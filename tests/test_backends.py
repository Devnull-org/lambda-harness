"""Backends share schema validation, process controls, and prompt semantics."""

import json
import os

import pytest

from lambda_harness.backends import Backend, CommandAgent
from lambda_harness.config import load_settings

from .conftest import SPECIFICATION
from .helpers import executable

REPLY = {"specification": SPECIFICATION, "questions": []}


def profile(name, script):
    backend = load_settings().backends[name]
    return backend.model_copy(update={"command": [script, *backend.command[1:]]})


def test_local_profile_preserves_saved_login_and_read_only_mode(tmp_path, monkeypatch):
    capture = tmp_path / "invocation.json"
    backend = load_settings().backends["local"]
    for key in backend.unset_env:
        monkeypatch.setenv(key, "test-only-value")
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(REPLY),
    }
    script = executable(
        tmp_path,
        "import json, os, sys\nfrom pathlib import Path\n"
        f"Path({str(capture)!r}).write_text(json.dumps({{\n"
        "'argv': sys.argv[1:], 'prompt': sys.stdin.read(), 'cwd': os.getcwd(),\n"
        f"'has_credentials': any(key in os.environ for key in {backend.unset_env!r})\n"
        "}))\n"
        f"print({json.dumps(envelope)!r})\n",
    )
    assert (
        CommandAgent(profile("local", script))("interviewer", {"request": "Fix addOne"}, tmp_path)
        == REPLY
    )
    invocation = json.loads(capture.read_text())
    assert not invocation["has_credentials"]
    assert invocation["cwd"] == str(tmp_path)
    assert "--print" in invocation["argv"]
    assert invocation["argv"][invocation["argv"].index("--mode") + 1] == "ask"
    assert invocation["argv"][invocation["argv"].index("--output-format") + 1] == "json"
    assert "--force" not in invocation["argv"]
    assert "Fix addOne" in invocation["prompt"]


def test_claude_profile_supplies_read_tools_schema_and_model(tmp_path):
    capture = tmp_path / "invocation.json"
    script = executable(
        tmp_path,
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps({'is_error': False, 'structured_output': REPLY, 'result': ''})!r})\n",
    )
    agent = CommandAgent(profile("claude-code", script), model="opus")
    assert agent("interviewer", {"request": "Fix addOne"}, tmp_path) == REPLY
    args = json.loads(capture.read_text())
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--model") + 1] == "opus"
    assert json.loads(args[args.index("--json-schema") + 1])["type"] == "object"
    assert "--strict-mcp-config" in args
    assert "--dangerously-skip-permissions" not in args


@pytest.mark.parametrize("provider", ["codex", "claude-code", "local"])
def test_provider_reported_model_is_kept_separate_from_the_requested_alias(tmp_path, provider):
    payload = (
        REPLY
        if provider == "codex"
        else {
            "result": json.dumps(REPLY),
            "model": "actual-model",
            "modelUsage": {"actual-model": {"inputTokens": 1}},
        }
    )
    script = executable(
        tmp_path,
        "import sys\nprint('model: actual-model', file=sys.stderr)\n"
        f"print({json.dumps(payload)!r})\n",
    )
    agent = CommandAgent(profile(provider, script), model="alias")
    assert agent("interviewer", {}, tmp_path) == REPLY
    assert agent.model == "alias"
    assert agent.reported_model == "actual-model"


def test_json_bridge_receives_versioned_request_and_returns_direct_response(tmp_path):
    capture = tmp_path / "request.json"
    script = executable(
        tmp_path,
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(capture)!r}).write_text(sys.stdin.read())\n"
        f"print({json.dumps(REPLY)!r})\n",
    )
    agent = CommandAgent(Backend(command=[script], input_format="json"), model="hosted-model")
    assert agent("interviewer", {"request": "Fix addOne"}, tmp_path) == REPLY
    request = json.loads(capture.read_text())
    assert request["version"] == 1
    assert request["role"] == "interviewer"
    assert request["model"] == "hosted-model"
    assert request["context"] == {"request": "Fix addOne"}
    assert "specification" in request["schema"]["properties"]
    assert "Fix addOne" in request["prompt"]


@pytest.mark.parametrize(
    "body, message",
    [
        ("import sys\nprint('Not logged in', file=sys.stderr)\nsys.exit(1)\n", "logged in"),
        ("print('not json')\n", "JSON|json"),
        ("print('[]')\n", "JSON object"),
        ("print('{\"is_error\": true}')\n", "error|failed"),
        ('print(\'{"subtype": "error_max_turns", "is_error": false}\')\n', "error|failed"),
        ('print(\'{"result": "{\\"unexpected\\": 1}"}\')\n', "schema|valid|specification"),
    ],
)
def test_backend_errors_do_not_advance_the_workflow(tmp_path, body, message):
    agent = CommandAgent(Backend(command=[executable(tmp_path, body)], response_fields=["result"]))
    with pytest.raises((RuntimeError, ValueError), match=message):
        agent("interviewer", {"request": "Fix addOne"}, tmp_path)


def test_backend_timeout_stops_the_process(tmp_path):
    marker = tmp_path / "finished"
    script = executable(
        tmp_path,
        "import time\nfrom pathlib import Path\ntime.sleep(2)\n"
        f"Path({str(marker)!r}).write_text('unexpected completion')\n",
    )
    with pytest.raises(RuntimeError, match="timed out|timeout"):
        CommandAgent(Backend(command=[script]), timeout=0.05)("interviewer", {}, tmp_path)
    assert not marker.exists()


def test_missing_executable_reports_an_actionable_error(tmp_path):
    missing = str(tmp_path / "missing-agent")
    with pytest.raises(RuntimeError, match="executable"):
        CommandAgent(Backend(command=[missing]))("interviewer", {}, tmp_path)


def test_model_is_passed_as_an_argument_without_shell_evaluation(tmp_path):
    capture = tmp_path / "arguments.json"
    script = executable(
        tmp_path,
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(REPLY)!r})\n",
    )
    model = "$(touch unwanted-file)"
    agent = CommandAgent(Backend(command=[script], model_args=["--model", "{model}"]), model=model)
    assert agent("interviewer", {}, tmp_path) == REPLY
    assert json.loads(capture.read_text()) == ["--model", model]
    assert not (tmp_path / "unwanted-file").exists()


def test_plain_prompt_backend_cannot_silently_ignore_model_override(tmp_path):
    with pytest.raises(ValueError, match="model_args"):
        CommandAgent(Backend(command=["never-executed"]), model="selected")(
            "interviewer", {}, tmp_path
        )


def test_auth_probe_detects_failed_login_even_with_zero_exit_code(tmp_path):
    script = executable(tmp_path, "print('Not logged in')\n")
    passed, message = CommandAgent(profile("local", script)).doctor(tmp_path)
    assert not passed
    assert "Not logged in" in message


def test_relative_backend_path_uses_the_selected_checkout_for_diagnostics(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable(checkout, f"print({json.dumps(REPLY)!r})\n")
    monkeypatch.chdir(tmp_path)
    agent = CommandAgent(Backend(command=["./agent-double"]))
    assert agent.doctor(checkout)[0]
    assert agent("interviewer", {}, checkout) == REPLY


def test_backend_accepts_one_fenced_response_after_progress_commentary(tmp_path):
    content = (
        "I will inspect the code. Here are the questions.```json\n" + json.dumps(REPLY) + "\n```"
    )
    script = executable(tmp_path, f"print({json.dumps({'result': content})!r})\n")
    assert (
        CommandAgent(Backend(command=[script], response_fields=["result"]))(
            "interviewer", {}, tmp_path
        )
        == REPLY
    )


def test_fenced_response_can_contain_backticks_in_json_strings(tmp_path):
    reply = {"answer": "The example is ```haskell\\naddOne 0\\n``` in the docs."}
    content = "Explanation follows.```json\n" + json.dumps(reply) + "\n```"
    script = executable(tmp_path, f"print({json.dumps({'result': content})!r})\n")
    assert (
        CommandAgent(Backend(command=[script], response_fields=["result"]))(
            "interviewer", {"intent": "explain"}, tmp_path
        )
        == reply
    )


@pytest.mark.parametrize("prefix", ["I inspected the code.", "I inspected the code.\n\n"])
def test_backend_accepts_narration_followed_by_one_unfenced_response(tmp_path, prefix):
    reply = {"answer": 'The example uses {braces}, an escaped "quote", and ```haskell fences.'}
    content = prefix + json.dumps(reply) + "\n"
    script = executable(tmp_path, f"print({json.dumps({'result': content})!r})\n")
    assert (
        CommandAgent(Backend(command=[script], response_fields=["result"]))(
            "interviewer", {"intent": "explain"}, tmp_path
        )
        == reply
    )


def test_backend_detaches_desktop_session_hooks_without_changing_login(tmp_path, monkeypatch):
    routing = {
        "ORCA_PANE_KEY": "parent-pane",
        "ORCA_TAB_ID": "parent-tab",
        "ORCA_WORKTREE_ID": "parent-worktree",
        "ORCA_AGENT_HOOK_ENDPOINT": "/unused/endpoint",
        "ORCA_AGENT_HOOK_PORT": "1",
        "ORCA_AGENT_HOOK_TOKEN": "test-hook-token",
        "ORCA_AGENT_LAUNCH_TOKEN": "parent-launch",
        "ORCA_AGENT_PANE": "parent-pane-alias",
        "ORCA_AGENT_LAUNCH": "parent-launch-alias",
        "ORCA_AGENT_SESSION_ID": "parent-session",
        "ORCA_STRUCTURED_SESSION": "1",
    }
    for key, value in routing.items():
        monkeypatch.setenv(key, value)
    home = str(tmp_path / "existing-home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "existing-config"))
    script = executable(
        tmp_path,
        "import os\n"
        f"assert not set({list(routing)!r}) & os.environ.keys()\n"
        f"assert os.environ['HOME'] == {home!r}\n"
        f"assert os.environ['XDG_CONFIG_HOME'] == {str(tmp_path / 'existing-config')!r}\n"
        f"print({json.dumps(REPLY)!r})\n",
    )
    assert CommandAgent(Backend(command=[script]))("interviewer", {}, tmp_path) == REPLY
    for key, value in routing.items():
        assert os.environ[key] == value


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{}\n```\n```json\n" + json.dumps(REPLY) + "\n```",
        "Commentary.```json\nnot json\n```",
        "Commentary.```json\n{}\n```",
        "Commentary without a structured response",
        "Commentary.{}" + json.dumps(REPLY),
        "Commentary." + json.dumps(REPLY) + " trailing prose",
        'Commentary.{"broken": ' + json.dumps(REPLY),
        "Commentary.[" + json.dumps(REPLY) + "]",
        "Commentary.```json\n" + json.dumps(REPLY),
    ],
)
def test_response_recovery_still_rejects_ambiguous_or_invalid_handoffs(tmp_path, content):
    script = executable(tmp_path, f"print({json.dumps({'result': content})!r})\n")
    with pytest.raises(ValueError, match="JSON|schema"):
        CommandAgent(Backend(command=[script], response_fields=["result"]))(
            "interviewer", {}, tmp_path
        )


def test_invalid_backend_output_is_saved_with_the_task(rig, tmp_path):
    harness, _, _ = rig
    script = executable(
        tmp_path, "import sys\nprint('invalid response')\nprint('diagnostic', file=sys.stderr)\n"
    )
    harness.agent = CommandAgent(Backend(command=[script]))
    view = harness.start("invalid", "Fix it")
    assert view["stage"] == "backend_blocked"
    directory = harness.state_dir / "invalid" / "backend-responses" / "0001-interviewer"
    assert (directory / "stdout.txt").read_text() == "invalid response\n"
    assert (directory / "stderr.txt").read_text() == "diagnostic\n"
    assert view["prompt"]["diagnostics"] == str(directory)


@pytest.mark.parametrize("native", [False, True])
def test_effort_is_forwarded_alongside_exact_model_without_shell_evaluation(tmp_path, native):
    capture = tmp_path / "effort.json"
    script = executable(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(REPLY)!r})\n",
    )
    args = ["-c", "{effort_config}"] if native else ["--effort", "{effort}"]
    agent = CommandAgent(
        Backend(command=[script], model_args=["--model", "{model}"], effort_args=args),
        model="chosen-model",
        effort="high",
    )
    assert agent("interviewer", {}, tmp_path) == REPLY
    assert json.loads(capture.read_text()) == [
        "--model",
        "chosen-model",
        *(["-c", 'model_reasoning_effort="high"'] if native else ["--effort", "high"]),
    ]


def test_unsupported_effort_is_never_silently_ignored(tmp_path):
    with pytest.raises(ValueError, match="does not support effort"):
        CommandAgent(Backend(command=["never-executed"]), effort="high")(
            "interviewer", {}, tmp_path
        )
