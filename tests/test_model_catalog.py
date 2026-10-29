"""Discover provider models without sending a generation request."""

import pytest

from lambda_harness.backends import Backend, CommandAgent
from lambda_harness.model_catalog import discover_models

from .helpers import executable


def candidate(tmp_path, script, format="json"):
    cli = executable(tmp_path, script)
    return CommandAgent(
        Backend(command=[cli], models_command=["{executable}"], models_format=format),
        name="test",
    )


def test_custom_catalog_preserves_identifiers_and_does_not_run_in_project(tmp_path):
    agent = candidate(
        tmp_path,
        "import json, os\n"
        f"assert os.getcwd() != {str(tmp_path)!r}\n"
        "print(json.dumps({'models':[{'id':'small','name':'Quick'},"
        "{'id':'large','name':'Careful'}],'current':'small'}))\n",
    )
    result = discover_models(agent, tmp_path)
    assert [m["id"] for m in result["models"]] == ["small", "large"]
    assert result["current"] == "small"


def test_local_list_reads_current_and_default_and_ignores_help(tmp_path):
    output = "Available models\n\nauto - Auto (current, default)\nfast - Fast\n\nTip: --model ID"
    result = discover_models(candidate(tmp_path, f"print({output!r})", "lines"), tmp_path)
    assert result["current"] == result["default"] == "auto"
    assert [(m["id"], m["name"]) for m in result["models"]] == [("auto", "Auto"), ("fast", "Fast")]


@pytest.mark.parametrize("ignore_config", [False, True])
def test_codex_uses_initialize_and_paginates_without_starting_a_thread(tmp_path, ignore_config):
    agent = candidate(
        tmp_path,
        """
import json, sys
for line in sys.stdin:
    r=json.loads(line)
    if r['method']=='initialize':
        print(json.dumps({'id':r['id'],'result':{}}),flush=True)
    elif r['method']=='initialized':
        continue
    else:
        assert r['method']=='model/list'
        second=r['params'].get('cursor')=='next'
        print(json.dumps({'method':'notification','params':{}}),flush=True)
        print(json.dumps({'id':r['id'],'result':{'data':[
            {'model':'careful' if second else 'quick','displayName':'Model',
             'isDefault':not second},
            {'model':'hidden','hidden':True}],
            'nextCursor':None if second else 'next'}}),flush=True)
""",
        "codex",
    )
    if ignore_config:
        agent.backend.command.append("--ignore-user-config")
    result = discover_models(agent, tmp_path)
    assert [m["id"] for m in result["models"]] == ["quick", "careful"]
    assert result["default"] == "quick"
    assert result["current"] == ("quick" if ignore_config else None)


def test_claude_reads_initialization_models_without_user_messages(tmp_path):
    agent = candidate(
        tmp_path,
        """
import json, sys, time
r=json.loads(sys.stdin.readline())
assert r['type']=='control_request' and r['request']['subtype']=='initialize'
print(json.dumps({'type':'control_response','response':{'subtype':'success',
    'request_id':r['request_id'],'response':{'models':[
      {'value':'default','displayName':'Default','resolvedModel':'current-version'},
      {'value':'fast','displayName':'Fast','resolvedModel':'fast-version'}]}}}),flush=True)
time.sleep(60)
""",
        "claude",
    )
    result = discover_models(agent, tmp_path)
    assert result["default"] == "default"
    assert result["models"][1]["resolved_model"] == "fast-version"


@pytest.mark.parametrize(
    "script,format",
    [
        ("print('not json')", "json"),
        ("import time; time.sleep(60)", "codex"),
        ("import sys; sys.exit(1)", "claude"),
        ("print('{}')", "json"),
    ],
)
def test_bad_or_slow_discovery_is_bounded_and_does_not_invent_options(tmp_path, script, format):
    with pytest.raises((RuntimeError, ValueError)):
        discover_models(candidate(tmp_path, script, format), tmp_path, timeout=0.2)


def test_backend_without_catalog_keeps_its_configured_model(tmp_path):
    agent = CommandAgent(Backend(command=["not-installed"], model="custom-model"))
    result = discover_models(agent, tmp_path)
    assert result["current"] == "custom-model"
    assert result["models"] == []
    assert result["notice"]


def test_codex_catalog_preserves_model_specific_effort_levels(tmp_path):
    agent = candidate(
        tmp_path,
        """
import json,sys
for line in sys.stdin:
    r=json.loads(line)
    if r['method']=='initialize':
        print(json.dumps({'id':r['id'],'result':{}}),flush=True)
    elif r['method']=='model/list':
        print(json.dumps({'id':r['id'],'result':{'data':[
            {'model':'quick','isDefault':True,'defaultReasoningEffort':'low',
             'supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}]},
            {'model':'deep','defaultReasoningEffort':'max',
             'supportedReasoningEfforts':[{'reasoningEffort':'max'}]}]}}),flush=True)
""",
        "codex",
    )
    agent.backend.effort_args = ["-c", "{effort_config}"]
    models = discover_models(agent, tmp_path)["models"]
    assert models[0]["efforts"] == ["low", "high"]
    assert models[0]["default_effort"] == "low"
    assert models[1]["efforts"] == ["max"]
