"""Native approvals are explicit, task-scoped, and unblock the same provider turn."""

import json
import os
import threading

import pytest

from lambda_harness.backends import Backend
from lambda_harness.codex_session import run_session
from lambda_harness.process import ProcessError

from .helpers import executable
from .test_webapp import browser_app as native_browser_app  # noqa: F401
from .test_webapp import wait_for


def session_script(tmp_path, method="item/commandExecution/requestApproval"):
    return executable(
        tmp_path,
        """import json,sys

def send(message):
    print(json.dumps(message),flush=True)

for line in sys.stdin:
    r=json.loads(line)
    if r.get('method')=='initialize':
        send({'id':r['id'],'result':{}})
    elif r.get('method')=='thread/start':
        p=r['params']
        assert p['sandbox']=='read-only' and p['approvalPolicy']=='on-request'
        assert p['approvalsReviewer']=='user' and p['ephemeral']
        assert p['allowProviderModelFallback'] is False
        send({'id':r['id'],'result':{'thread':{'id':'thread'},'model':p.get('model','default-model')}})
    elif r.get('method')=='turn/start':
        assert r['params']['outputSchema']['type']=='object'
        send({'id':r['id'],'result':{'turn':{'id':'turn'}}})
        send({'id':42,'method':METHOD,'params':{'threadId':'thread','turnId':'turn',
            'itemId':'item','reason':'Read the linked GitHub issue',
            'command':'gh issue view 2913 --repo cardano-scaling/hydra',
            'cwd':'/task/worktree','permissions':{'network':{'enabled':True}}}})
    elif r.get('id')==42:
        result=r.get('result',{})
        network=result.get('permissions',{}).get('network',{}).get('enabled')
        allowed=result.get('decision')=='accept' or network
        if 'permissions' in result: assert result['scope']=='turn'
        answer=json.dumps({'answer':'approved' if allowed else 'denied','references':[]})
        send({'method':'item/completed','params':{'threadId':'thread','turnId':'turn',
            'item':{'type':'agentMessage','phase':'final_answer','text':answer}}})
        send({'method':'turn/completed','params':{'threadId':'thread',
            'turn':{'id':'turn','status':'completed'}}})
""".replace("METHOD", repr(method)),
    )


@pytest.mark.parametrize(
    "method", ["item/commandExecution/requestApproval", "item/permissions/requestApproval"]
)
@pytest.mark.parametrize("allowed", [True, False])
def test_native_decision_reaches_same_turn_without_session_grants(tmp_path, method, allowed):
    requests = []

    def approve(request, cancel):
        requests.append(request)
        return allowed

    answer, model = run_session(
        [session_script(tmp_path, method)],
        tmp_path,
        prompt="Read issue",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        model="selected-model",
        env=os.environ.copy(),
        timeout=5,
        cancel=threading.Event(),
        approve=approve,
    )
    assert json.loads(answer)["answer"] == ("approved" if allowed else "denied")
    assert model == "selected-model"
    assert requests[0]["method"] == method


@pytest.mark.parametrize("decision", ["approve", "deny", "stop"])
def test_browser_runtime_permissions_are_scoped_and_cancellable(request, tmp_path, decision):
    app, call, _, _ = request.getfixturevalue("native_browser_app")
    script = session_script(tmp_path)
    app.settings.backends["test"] = Backend(
        command=[script], approval_command=[script], models_format="codex"
    )
    code, _ = call(
        "/api/command",
        {
            "command": "chat",
            "task_id": "approval",
            "request": "Read issue",
            "base": "main",
            "backend": "test",
            "model": "chosen-model",
        },
    )
    assert code == 202
    view = wait_for(
        lambda: (
            (v if v["job"].get("permission") else None)
            if (v := call("/api/state?task=approval")[1])
            else None
        )
    )
    job = view["job"]
    assert view["can_stop"]
    body = {
        "task_id": "approval",
        "job_id": job["id"],
        "request_id": job["permission"]["id"],
        "decision": "approve",
    }
    assert call("/api/approval", body, authenticated=False)[0] == 403
    assert call("/api/approval", {**body, "task_id": "other"})[0] == 400
    assert call("/api/approval", {**body, "request_id": "stale"})[0] == 400
    assert call("/api/state?task=other")[1]["job"] is None
    if decision == "stop":
        assert call("/api/stop", {"task_id": "approval", "job_id": job["id"]})[1]["stopping"]
    else:
        assert call("/api/approval", {**body, "decision": decision})[0] == 200
    wait_for(lambda: not call("/api/state?task=approval")[1]["job"]["running"])
    final = call("/api/state?task=approval")[1]
    assert not final["job"].get("permission")
    assert call("/api/approval", body)[0] == 400
    if decision == "stop":
        assert final["task"]["stage"] == "agent_paused"
    else:
        assert final["job"]["error"] is None
        assert final["task"]["messages"][-1]["answer"] == (
            "approved" if decision == "approve" else "denied"
        )
        assert final["task"]["messages"][-1]["generation"]["reported_model"] == "chosen-model"


@pytest.mark.parametrize(
    "method", ["item/fileChange/requestApproval", "item/tool/requestUserInput"]
)
def test_unsupported_actions_never_receive_an_access_grant(tmp_path, method):
    def unexpected(request, cancel):
        pytest.fail("Unsupported operation was sent to the approval UI")

    answer, _ = run_session(
        [session_script(tmp_path, method)],
        tmp_path,
        prompt="Inspect only",
        schema={"type": "object"},
        model=None,
        env=os.environ.copy(),
        timeout=5,
        cancel=None,
        approve=unexpected,
    )
    assert json.loads(answer)["answer"] == "denied"


def test_failed_turn_reports_provider_diagnostic(tmp_path):
    script = session_script(tmp_path)
    from pathlib import Path

    path = Path(script)
    path.write_text(
        path.read_text().replace(
            "'status':'completed'", "'status':'failed','error':{'message':'Provider unavailable'}"
        )
    )
    with pytest.raises(ProcessError, match="Provider unavailable") as failure:
        run_session(
            [script],
            tmp_path,
            prompt="Read issue",
            schema={"type": "object"},
            model=None,
            env=os.environ.copy(),
            timeout=5,
            cancel=None,
            approve=lambda request, cancel: False,
        )
    assert failure.value.kind == "provider"


@pytest.mark.parametrize("effort", [None, "high", "max"])
def test_native_turn_receives_the_exact_selected_model_and_effort(tmp_path, effort):
    from pathlib import Path

    script = session_script(tmp_path)
    path = Path(script)
    path.write_text(
        path.read_text().replace(
            "assert r['params']['outputSchema']['type']=='object'",
            "assert r['params']['model']=='chosen-model'\n"
            f"        assert r['params'].get('effort')=={effort!r}\n"
            "        assert r['params']['outputSchema']['type']=='object'",
        )
    )
    answer, model = run_session(
        [script],
        tmp_path,
        prompt="Review",
        schema={"type": "object"},
        model="chosen-model",
        effort=effort,
        env=os.environ.copy(),
        timeout=5,
        cancel=None,
        approve=lambda *_: False,
    )
    assert model == "chosen-model"
    assert json.loads(answer)["answer"] == "denied"


def test_native_provider_cannot_silently_replace_selected_model(tmp_path):
    from pathlib import Path

    script = session_script(tmp_path)
    path = Path(script)
    path.write_text(path.read_text().replace("p.get('model','default-model')", "'different-model'"))
    with pytest.raises(ProcessError, match="Requested model.*chosen-model.*different-model"):
        run_session(
            [script],
            tmp_path,
            prompt="Review",
            schema={"type": "object"},
            model="chosen-model",
            effort="high",
            env=os.environ.copy(),
            timeout=5,
            cancel=None,
            approve=lambda *_: False,
        )


def test_native_mid_turn_model_reroute_is_rejected(tmp_path):
    from pathlib import Path

    script = session_script(tmp_path)
    path = Path(script)
    path.write_text(
        path.read_text().replace(
            "send({'id':42,'method':METHOD".replace(
                "METHOD", repr("item/commandExecution/requestApproval")
            ),
            "send({'method':'model/rerouted','params':{'threadId':'thread','toModel':'other'}})\n"
            "        send({'id':42,'method':" + repr("item/commandExecution/requestApproval"),
        )
    )
    with pytest.raises(ProcessError, match="switched from requested model.*chosen-model.*other"):
        run_session(
            [script],
            tmp_path,
            prompt="Review",
            schema={"type": "object"},
            model="chosen-model",
            env=os.environ.copy(),
            timeout=5,
            cancel=None,
            approve=lambda *_: False,
        )
