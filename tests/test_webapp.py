"""The browser uses the existing gates and can stop and revise a real child process."""

import http.client
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from lambda_harness.__main__ import parser
from lambda_harness.discovery import SelectionRequired
from lambda_harness.webapp import LocalApp, create_server

from .conftest import IMPLEMENTED, SOURCE, SPECIFICATION, ScriptedAgent, ScriptedExecutor, git
from .helpers import executable
from .test_exploration import ANALYSIS


@pytest.fixture
def browser_app(repository, tmp_path, request):
    calls, ready = tmp_path / "calls.jsonl", tmp_path / "ready"
    script = executable(
        tmp_path,
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        f"path = Path({str(calls)!r})\n"
        "first = not path.exists()\n"
        "with path.open('a') as out: out.write(json.dumps(request) + '\\n')\n"
        "if first:\n"
        f"    Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        f"print({json.dumps({'specification': SPECIFICATION, 'questions': []})!r})\n",
    )
    config = tmp_path / "backend.toml"
    config.write_text(
        'default_backend = "test"\n[backends.test]\ninput_format = "json"\n'
        f"command = [{json.dumps(script)}]\n"
    )
    args = parser().parse_args(
        [
            "--repo",
            str(repository),
            "--state-dir",
            str(tmp_path / "state"),
            "--config",
            str(config),
            "--port",
            "0",
        ]
    )
    app = LocalApp(args)
    phone = None
    if getattr(request, "param", None) == "phone":
        from lambda_harness.phone import PhoneAccess

        phone = PhoneAccess("tailscale", "laptop.example.ts.net", "owner@example.com", 443)
    server = create_server(app, 0, phone=phone)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, headers=None, authenticated=True):
        request_headers = {"X-Harness-Token": app.token} if authenticated else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            connection.request(
                "GET" if body is None else "POST",
                path,
                body=None if body is None else json.dumps(body),
                headers=request_headers,
            )
            response = connection.getresponse()
            text = response.read().decode()
            return response.status, (
                json.loads(text)
                if response.getheader("Content-Type") == "application/json"
                else text
            )
        finally:
            connection.close()

    yield app, call, calls, ready
    server.hub.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def wait_for(test):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = test()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Operation did not finish")


def test_saved_interview_history_is_recovered_in_the_browser_api(browser_app):
    from .test_conversation import legacy_interview

    app, call, _, _ = browser_app
    agent = ScriptedAgent()
    harness = app.harness(agent)
    try:
        original = legacy_interview(harness, agent)
        status, response = call("/api/state?task=legacy")
        assert status == 200
        messages = response["task"]["messages"]
        assert [m.get("text") or m["questions"][0] for m in messages] == [
            "First saved question",
            "First developer reply",
            "Second saved question",
        ]
        assert call("/api/state?task=legacy")[1]["task"]["messages"] == messages
        assert harness._state("legacy") == original
        assert len(agent.calls) == 2
    finally:
        harness.close()


def test_chat_selects_models_per_reply_and_keeps_provenance_after_reopening(browser_app, tmp_path):
    from lambda_harness.backends import Backend

    app, call, _, _ = browser_app
    calls = tmp_path / "models-used.jsonl"
    script = executable(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        "if len(sys.argv)>1:\n"
        " print(json.dumps({'models':[{'id':'quick','name':'Quick'},"
        "{'id':'careful','name':'Careful'}], 'current':'quick'}))\n"
        "else:\n"
        " request=json.load(sys.stdin)\n"
        f" with Path({str(calls)!r}).open('a') as f: f.write(json.dumps(request['model'])+'\\n')\n"
        " print(json.dumps({'answer':'A response', 'references':[]}))\n",
    )
    app.settings.backends["test"] = Backend(
        command=[script], input_format="json", models_command=["{executable}", "models"]
    )
    app.selected_backend = "test"
    app.args.model = "launch-default"
    status, catalogs = call("/api/models")
    assert status == 200 and catalogs["backend"] == "test"
    assert catalogs["catalogs"][0]["current"] == "launch-default"
    assert not calls.exists()
    for command in [
        {
            "command": "chat",
            "task_id": "model chat",
            "request": "Explain this",
            "base": "HEAD",
            "model": "quick",
        },
        {
            "command": "respond",
            "task_id": "model chat",
            "action": "answer",
            "text": "More detail",
            "model": "careful",
        },
        {
            "command": "respond",
            "task_id": "model chat",
            "action": "answer",
            "text": "Use default",
            "model": None,
        },
    ]:
        assert call("/api/command", command)[0] == 202
        wait_for(lambda: app.job and not app.job["running"])
        assert app.job["error"] is None
        assert app.job["result"]["stage"] == "chat", app.job
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        "quick",
        "careful",
        None,
    ]
    view = call("/api/state?task=model%20chat")[1]["task"]
    generations = [m["generation"] for m in view["messages"] if m.get("role") == "chat"]
    assert [g["model"] for g in generations] == ["quick", "careful", None]
    assert all(g["backend"] == "test" for g in generations)
    reopened = app.harness()
    try:
        assert reopened._state("model chat")["messages"] == view["messages"]
    finally:
        reopened.close()


def test_exploration_follow_up_uses_the_selected_model(browser_app, tmp_path):
    from lambda_harness.backends import Backend

    app, call, calls, _ = browser_app
    script = executable(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        f"with Path({str(calls)!r}).open('a') as f: f.write(json.dumps(request)+'\\n')\n"
        f"print({json.dumps(ANALYSIS)!r})\n",
    )
    app.settings.backends["test"] = Backend(command=[script], input_format="json")
    for command in [
        {"command": "explore", "request": "Explore the parser", "base": "HEAD", "model": "quick"},
        {
            "command": "respond",
            "action": "question",
            "text": "What are the tradeoffs?",
            "model": "careful",
        },
    ]:
        assert call("/api/command", {"task_id": "research", "backend": "test", **command})[0] == 202
        wait_for(lambda: app.job and not app.job["running"])
        assert app.job["error"] is None
        assert app.job["result"]["stage"] == "exploration"
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [(r["role"], r["model"]) for r in requests] == [
        ("explorer", "quick"),
        ("explorer", "careful"),
    ]
    view = call("/api/state?task=research")[1]["task"]
    assert view["model_selection"] == {"backend": "test", "model": "careful"}
    assert [m["generation"]["model"] for m in view["messages"] if m.get("role") == "explorer"] == [
        "quick",
        "careful",
    ]
    # A fresh request without model fields keeps the saved choice, even if defaults change.
    app.args.model = "different-default"
    assert (
        call(
            "/api/command",
            {
                "command": "respond",
                "task_id": "research",
                "action": "question",
                "text": "Continue the analysis",
            },
        )[0]
        == 202
    )
    wait_for(lambda: app.job and not app.job["running"])
    assert app.job["error"] is None
    latest = json.loads(calls.read_text().splitlines()[-1])
    assert (latest["role"], latest["model"]) == ("explorer", "careful")
    from lambda_harness.project_context import state_directory
    from lambda_harness.project_settings import ProjectSettings

    with ProjectSettings(state_directory(app.args.repo, app.args.state_dir)) as saved:
        assert saved.task_model("research") == {"backend": "test", "model": "careful"}


def test_model_discovery_does_not_lock_state_or_stop_requests(browser_app, monkeypatch):
    app, call, _, _ = browser_app
    app.selected_backend = "test"
    entered, release = threading.Event(), threading.Event()

    def discover(*args):
        entered.set()
        assert release.wait(timeout=10)
        return {"models": [], "current": None, "default": None}

    monkeypatch.setattr("lambda_harness.webapp.discover_models", discover)
    result = []
    worker = threading.Thread(target=lambda: result.append(call("/api/models")))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert call("/api/state")[0] == 200
        assert call("/api/stop", {})[0] == 200
    finally:
        release.set()
        worker.join(timeout=5)
    assert result[0][0] == 200


def test_models_api_is_authenticated_and_model_errors_are_recoverable(browser_app, monkeypatch):
    app, call, _, _ = browser_app
    app.selected_backend = "test"
    assert call("/api/models", authenticated=False)[0] == 403

    def broken(*args):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr("lambda_harness.webapp.discover_models", broken)
    status, catalog = call("/api/models")
    assert status == 200 and catalog["catalogs"][0]["error"] == "Provider unavailable"
    assert catalog["catalogs"][0]["models"] == []
    for model in ("", "bad\x00model", 123):
        assert (
            call("/api/command", {"command": "chat", "task_id": "invalid", "model": model})[0]
            == 400
        )
    assert app.job is None


def test_requested_review_tools_use_http_scope_without_model_selection(browser_app, monkeypatch):
    app, call, calls, _ = browser_app
    harness = app.harness(ScriptedAgent())
    try:
        repository = harness.repository
        base = git(repository, "rev-parse", "HEAD")
        (repository / "README.md").write_text("# Guide\n")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "Docs")
        view = harness.start_review("links", "Check docs", base=base, head="HEAD")
    finally:
        harness.close()
    monkeypatch.setattr(
        "lambda_harness.webapp.select_backend", lambda *a: pytest.fail("Model selected")
    )
    monkeypatch.setattr(
        "lambda_harness.workflow.check_documentation_links",
        lambda *a, **kw: {"summary": "Checked links", "issues": [], "limitations": "No build"},
    )
    body = {
        "command": "review_action",
        "task_id": "links",
        "action": "check_links",
        "token": view["review_actions"]["token"],
    }
    assert call("/api/command", body, authenticated=False)[0] == 403
    assert call("/api/command", body)[0] == 202
    wait_for(lambda: not app.state("links")["job"]["running"])
    state = app.state("links")
    assert state["job"]["error"] is None
    assert state["task"]["messages"][-1]["action"] == "check_links"
    assert state["task"]["stage"] == "standalone_review"
    assert not calls.exists()
    assert call("/api/command", {**body, "token": "stale"})[0] == 202
    wait_for(lambda: not app.state("links")["job"]["running"])
    assert "review changed" in app.state("links")["job"]["error"]


def test_handoff_http_preview_import_and_takeover_do_not_call_a_backend(browser_app, monkeypatch):
    app, call, calls, _ = browser_app
    harness = app.harness(ScriptedAgent())
    try:
        harness.start("share", "Explain this change")
    finally:
        harness.close()
    status, archive = call("/api/handoff/export", {"tasks": ["share"]})
    assert status == 200, archive
    assert call("/api/handoff/export", {"tasks": ["share"]}, authenticated=False)[0] == 403
    assert (
        call(
            "/api/handoff/export",
            {"tasks": ["share"]},
            headers={"Origin": "https://elsewhere.invalid"},
        )[0]
        == 403
    )
    status, preview = call("/api/handoff/preview", {"archive": archive})
    assert status == 200, preview
    assert preview["tasks"][0]["task_id"] == "share-import-1"
    status, result = call(
        "/api/handoff/import", {"archive": archive, "confirmation": preview["confirmation"]}
    )
    assert status == 200, result
    view = call("/api/state?task=share-import-1")[1]["task"]
    assert view["stage"] == "handoff_review"
    monkeypatch.setattr(
        "lambda_harness.webapp.select_backend", lambda *a: pytest.fail("Unexpected backend call")
    )
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": "share-import-1",
            "action": "continue_handoff",
            "token": view["prompt"]["approval_token"],
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=share-import-1")[1]
    assert state["task"]["stage"] == "specification_review", state["job"]
    assert not calls.exists()


def test_handoff_status_remains_responsive_and_blocks_mutations(browser_app, monkeypatch):
    app, call, _, _ = browser_app
    entered, release = threading.Event(), threading.Event()

    def paused_export(harness, tasks):
        entered.set()
        assert release.wait(5)
        return {"exported": True}

    monkeypatch.setattr("lambda_harness.webapp.export_handoff", paused_export)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(call("/api/handoff/export", {"tasks": []}))
    )
    thread.start()
    try:
        assert entered.wait(5)
        assert call("/api/state")[1]["job"]["running"]
        assert (
            call(
                "/api/command",
                {"command": "start", "task_id": "new", "request": "new", "base": "main"},
            )[0]
            == 400
        )
        assert call("/api/handoff/export", {"tasks": []})[0] == 400
        assert (
            call(
                "/api/projects/remove",
                {"project_id": app.identity["id"], "confirmation": app.identity["id"]},
            )[0]
            == 400
        )
        assert call("/api/backend", {"backend": "auto"})[0] == 400
    finally:
        release.set()
        thread.join(5)
    assert result == [(200, {"exported": True})]
    assert not call("/api/state")[1]["job"]["running"]


def test_invalid_handoff_returns_a_useful_error_and_releases_busy_state(browser_app):
    _, call, _, _ = browser_app
    status, result = call("/api/handoff/preview", {"archive": {"version": 99}})
    assert status == 400 and result["error"]
    job = call("/api/state")[1]["job"]
    assert not job["running"]
    assert job["error"] == result["error"]


@pytest.mark.parametrize(
    "kind,reviewers,roles",
    [
        ("tests", None, ["test_author"]),
        ("review", "haskell", ["haskell_reviewer"]),
        ("review", "documentation", ["documentation_reviewer"]),
        ("review", "all", ["haskell_reviewer", "security_reviewer", "documentation_reviewer"]),
    ],
)
def test_browser_standalone_tasks_start_directly_and_finish_locally(
    browser_app, monkeypatch, kind, reviewers, roles
):
    app, call, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    monkeypatch.setattr("lambda_harness.webapp.LocalExecutor", lambda **kw: executor)
    command = {"command": kind, "task_id": kind, "base": "main", "request": "Check increment"}
    if kind == "review":
        git(app.args.repo, "checkout", "-b", "feature")
        (app.args.repo / SOURCE).write_text(IMPLEMENTED)
        git(app.args.repo, "commit", "-am", "Change")
        command.update(head="feature", reviewers=reviewers)
    else:
        command.update(packages=["example-app"], criteria=["Increment by one"], expectation="red")
    assert call("/api/command", command)[0] == 202
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call(f"/api/state?task={kind}")[1]
    assert state["job"]["error"] is None, state["job"]
    task = state["task"]
    assert task["task_kind"] == kind
    assert task["stage"] == ("standalone_review" if kind == "review" else "test_review")
    assert [role for role, _, _ in agent.calls] == roles
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": kind,
            "action": "done" if kind == "review" else "approve",
            "token": task["prompt"]["approval_token"],
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    task = call(f"/api/state?task={kind}")[1]["task"]
    assert task["stage"] == f"{kind}_complete", task
    assert task["prompt"]["available_phases"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("head", []),
        ("reviewers", {}),
        ("expectation", False),
        ("criteria", "not a list"),
        ("packages", [7]),
    ],
)
def test_standalone_form_rejects_invalid_field_types(browser_app, field, value):
    _, call, calls, _ = browser_app
    code, result = call("/api/command", {"command": "tests", "task_id": "bad", field: value})
    assert code == 400 and field in result["error"]
    assert not calls.exists()


def test_browser_chat_can_stop_revise_and_continue_without_workflow_gates(browser_app):
    app, call, calls, ready = browser_app
    assert (
        call(
            "/api/command",
            {
                "command": "chat",
                "task_id": "chat",
                "base": "main",
                "request": "Explain pure functions",
            },
        )[0]
        == 202
    )
    wait_for(ready.exists)
    assert call("/api/stop", {})[1]["stopping"]
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state?task=chat")[1]["task"]["stage"] == "agent_paused"
    executable(
        calls.parent,
        "import json, sys\nrequest = json.load(sys.stdin)\n"
        "print(json.dumps({'answer': 'We can discuss that.', 'references': []}))\n",
    )
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": "chat",
            "action": "revise",
            "text": "Actually, explain recursion",
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    task = call("/api/state?task=chat")[1]["task"]
    assert task["stage"] == "chat", task
    assert task["prompt"]["actions"] == ["answer"]
    assert task["messages"][-1]["answer"] == "We can discuss that."
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": "chat",
            "action": "answer",
            "text": "And tail recursion?",
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    task = call("/api/state?task=chat")[1]["task"]
    assert task["stage"] == "chat"
    assert task["messages"][-2]["text"] == "And tail recursion?"
    assert len([message for message in task["messages"] if message.get("role") == "chat"]) == 2


def test_browser_stop_edit_resume_and_approval_use_existing_workflow(browser_app):
    app, call, calls, ready = browser_app
    assert call("/api/state")[1]["tasks"] == []
    code, _ = call(
        "/api/command",
        {"command": "start", "task_id": "web", "request": "Original idea", "base": "main"},
    )
    assert code == 202
    wait_for(ready.exists)
    assert call("/api/state?task=web")[1]["can_stop"]
    assert call("/api/command", {"command": "resume", "task_id": "web"})[0] == 400
    assert call("/api/stop", {})[1]["stopping"]
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    view = call("/api/state?task=web")[1]["task"]
    assert view["stage"] == "agent_paused"
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)
    assert app.state("web")["task"]["prompt"]["draft"] == "Original idea"
    call("/api/command", {"command": "resume", "task_id": "web"})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert len(calls.read_text().splitlines()) == 1
    call(
        "/api/command",
        {"command": "respond", "task_id": "web", "action": "revise", "text": "Corrected\nscope"},
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=web")[1]
    assert state["task"]["stage"] == "specification_review"
    last = json.loads(calls.read_text().splitlines()[-1])
    assert last["context"]["messages"][-1]["text"] == "Corrected\nscope"
    call("/api/command", {"command": "respond", "task_id": "web", "action": "approve"})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert "token" in call("/api/state")[1]["job"]["error"]
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": "web",
            "action": "approve",
            "token": state["task"]["prompt"]["approval_token"],
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state?task=web")[1]["task"]["stage"] == "specification_ready"
    assert len(calls.read_text().splitlines()) == 2


def test_browser_can_stop_validation_keep_changes_and_recheck(browser_app, monkeypatch):
    from lambda_harness.process import run_process

    from .conftest import reach_test_review

    app, call, calls, ready = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    harness = app.harness(agent)
    harness.execute = executor
    try:
        original = reach_test_review(harness)
        baseline = git(Path(original["worktree"]), "rev-parse", "HEAD")
    finally:
        harness.close()
    before_calls = list(agent.calls)
    script = executable(
        calls.parent,
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "print('partial check output', flush=True)\ntime.sleep(60)\n",
    )
    commands = []

    def check(argv, cwd, **kwargs):
        commands.append(argv)
        return run_process([script], cwd, **kwargs)

    monkeypatch.setattr("lambda_harness.validation.run_process", check)
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    assert (
        call("/api/command", {"command": "respond", "task_id": "increment", "action": "recheck"})[0]
        == 202
    )
    wait_for(ready.exists)
    running = call("/api/state?task=increment")[1]
    assert running["can_stop"] and running["activity_kind"] == "validation"
    assert call("/api/stop", {})[1]["stopping"]
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    stopped = call("/api/state?task=increment")[1]
    assert stopped["task"]["stage"] == "validation_blocked"
    assert "recheck" in stopped["task"]["prompt"]["actions"]
    assert "Checks stopped" in stopped["task"]["prompt"]["summary"]
    assert stopped["task"]["prompt"]["validation"]["status"] == "stopped"
    assert not stopped["can_stop"] and not stopped["stopping"]
    assert stopped["activity_kind"] is None
    assert len(commands) == 1  # No further checks or repair calls after Stop.
    assert agent.calls == before_calls
    assert git(Path(original["worktree"]), "rev-parse", "HEAD") == baseline
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)
    logs = Path(stopped["task"]["prompt"]["validation"]["log_directory"])
    interrupted = [json.loads(path.read_text()) for path in logs.glob("*.json")]
    interrupted = [result for result in interrupted if result.get("cancelled")]
    assert len(interrupted) == 1
    assert interrupted[0]["exit_code"] is None
    assert "partial check output" in interrupted[0]["stdout"]
    harness = app.harness(agent)
    harness.execute = executor
    try:
        assert (
            "validated_fingerprint"
            not in harness.graph.get_state(harness._config("increment")).values
        )
        assert harness.resume("increment")["stage"] == "validation_blocked"
        rechecked = harness.respond("increment", "recheck")
        assert rechecked["stage"] == "test_review"
        assert rechecked["prompt"]["diff"] == original["prompt"]["diff"]
        assert agent.calls == before_calls
    finally:
        harness.close()


def test_browser_rejects_foreign_sites_and_unauthenticated_api_access(browser_app):
    _, call, _, _ = browser_app
    assert call("/api/state", authenticated=False)[0] == 403
    assert call("/api/stop", {}, authenticated=False)[0] == 403
    assert call("/", headers={"Host": "attacker.example"})[0] == 403
    assert call("/api/state", headers={"Origin": "https://attacker.example"})[0] == 403
    assert call("/")[0] == 200
    assert "HARNESS_SESSION_TOKEN" not in call("/")[1]
    assert call("/app.js")[0] == 200
    assert call("/vendor/marked.js")[0] == 200
    assert call("/../../README.md")[0] == 404
    assert call("/api/state?task=not-created-yet")[1]["task"] is None


@pytest.mark.parametrize("browser_app", ["phone"], indirect=True)
def test_phone_requests_require_owner_identity_origin_and_session_token(browser_app):
    app, call, _, _ = browser_app
    headers = {
        "Host": "laptop.example.ts.net",
        "X-Forwarded-Host": "laptop.example.ts.net",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "100.64.0.2",
        "Tailscale-User-Login": "owner@example.com",
    }
    assert app.token in call("/", headers=headers, authenticated=False)[1]
    qr_status, qr = call("/phone-qr.svg", headers=headers, authenticated=False)
    assert qr_status == 200 and "<svg" in qr
    assert app.token not in qr
    assert call("/phone-qr.svg", headers={**headers, "Tailscale-User-Login": "other"})[0] == 403
    assert call("/api/state", headers=headers)[0] == 200
    assert call("/api/state", headers=headers, authenticated=False)[0] == 403
    same_origin = {**headers, "Origin": "https://laptop.example.ts.net"}
    assert call("/api/stop", {}, headers=same_origin)[0] == 200
    for changed in (
        {"Tailscale-User-Login": "someone-else@example.com"},
        {"Tailscale-User-Login": ""},
        {"Tailscale-Funnel-Request": "?1"},
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Host": "attacker.example"},
        {"X-Forwarded-Host": "attacker.example"},
        {"X-Forwarded-Proto": "http"},
    ):
        for path, body in (("/", None), ("/api/state", None), ("/api/stop", {})):
            status, content = call(path, body, headers={**headers, **changed})
            assert status == 403, changed
            assert app.token not in str(content)
    assert call("/")[0] == 200


@pytest.mark.parametrize("browser_app", [None, "phone"], indirect=True)
def test_forwarded_requests_cannot_bypass_auth_using_loopback_host(browser_app):
    _, call, _, _ = browser_app
    for headers in (
        {"X-Forwarded-For": "100.64.0.2"},
        {"Forwarded": "for=100.64.0.2"},
        {"Tailscale-User-Login": "someone-else@example.com"},
        {"Tailscale-Funnel-Request": "?1"},
        {"X-Forwarded-Proto": "https", "Tailscale-User-Login": "owner@example.com"},
    ):
        assert call("/", headers=headers)[0] == 403
        assert call("/api/stop", {}, headers=headers)[0] == 403


def test_closing_server_stops_generation_and_persists_pause(browser_app):
    app, call, _, ready = browser_app
    call(
        "/api/command",
        {"command": "start", "task_id": "closing", "request": "Original", "base": "main"},
    )
    wait_for(ready.exists)
    app.close()
    assert not app.worker.is_alive()
    assert app.state("closing")["task"]["stage"] == "agent_paused"


def test_browser_asks_for_backend_without_losing_the_pending_request(browser_app, monkeypatch):
    _, call, calls, ready = browser_app

    def choose(agent, cwd):
        if agent.backend is None:
            raise SelectionRequired(
                [
                    {"name": "test", "available": True, "message": "Ready"},
                    {"name": "codex", "available": True, "message": "Ready"},
                ]
            )
        return agent.backend

    monkeypatch.setattr("lambda_harness.webapp.select_backend", choose)
    request = {"command": "start", "task_id": "choice", "request": "Keep my idea", "base": "main"}
    call("/api/command", request)
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state")[1]
    assert state["job"]["selection"]["available_backends"] == ["test", "codex"]
    assert state["job"]["pending_command"] == request
    assert not calls.exists()
    call("/api/command", {**state["job"]["pending_command"], "backend": "test"})
    wait_for(ready.exists)
    call("/api/stop", {})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state?task=choice")[1]["task"]["request"] == "Keep my idea"


def test_browser_creates_and_deletes_tasks_with_spaces(browser_app, monkeypatch):
    _, call, _, _ = browser_app
    agent = ScriptedAgent()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    name = "Review documentation"
    code, _ = call(
        "/api/command",
        {"command": "start", "task_id": name, "request": "Check docs", "base": "main"},
    )
    assert code == 202
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=" + quote(name))[1]
    assert state["job"]["error"] is None, state["job"]
    assert state["task"]["task_id"] == name
    assert state["tasks"] == [
        {"task_id": name, "stage": "specification_review", "outcome": state["task"]["outcome"]}
    ]
    assert state["task"]["outcome"]["kind"] == "waiting"
    code, error = call(
        "/api/command", {"command": "start", "task_id": "../Escape", "request": "Invalid"}
    )
    assert code == 400 and "Task name" in error["error"]
    plan = call("/api/abandon-plan?task=" + quote(name))[1]
    assert (
        call(
            "/api/command",
            {
                "command": "abandon",
                "task_id": name,
                "confirmation": name,
                "token": plan["confirmation_token"],
            },
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state")[1]["tasks"] == []


def test_browser_abandon_requires_task_name_and_current_confirmation_token(browser_app):
    _, call, _, ready = browser_app
    call(
        "/api/command",
        {"command": "start", "task_id": "delete", "request": "Discard this idea", "base": "main"},
    )
    wait_for(ready.exists)
    call("/api/stop", {})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    plan = call("/api/abandon-plan?task=delete")[1]
    request = {
        "command": "abandon",
        "task_id": "delete",
        "token": plan["confirmation_token"],
        "confirmation": "wrong-name",
    }
    call("/api/command", request)
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state?task=delete")[1]["task"]["stage"] == "agent_paused"
    call("/api/command", {**request, "confirmation": "delete"})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/state")[1]["tasks"] == []


@pytest.mark.parametrize("finished", [False, True])
def test_browser_deletes_saved_reviews_without_a_model(browser_app, monkeypatch, finished):
    app, call, calls, _ = browser_app
    harness = app.harness(ScriptedAgent())
    try:
        repository = harness.repository
        base = git(repository, "rev-parse", "HEAD")
        (repository / "README.md").write_text("# Documentation\n")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "Docs to review")
        head = git(repository, "rev-parse", "HEAD")
        harness.start("keep", "Keep this conversation")
        view = harness.start_review("discard-review", "Check docs", base=base, head="HEAD")
        if finished:
            view = harness.respond("discard-review", "done")
            assert view["stage"] == "review_complete"
        worktree = Path(view["worktree"])
    finally:
        harness.close()
    monkeypatch.setattr(
        "lambda_harness.webapp.select_backend", lambda *a: pytest.fail("Model selected")
    )
    plan = call("/api/abandon-plan?task=discard-review")[1]
    code, _ = call(
        "/api/command",
        {
            "command": "abandon",
            "task_id": "discard-review",
            "confirmation": "discard-review",
            "token": plan["confirmation_token"],
        },
    )
    assert code == 202
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=discard-review")[1]
    assert not state["job"].get("error")
    assert state["task"] is None
    assert [task["task_id"] for task in state["tasks"]] == ["keep"]
    assert not worktree.parent.exists()
    assert not git(repository, "branch", "--list", plan["branch"])
    assert git(repository, "rev-parse", "HEAD") == head
    assert not calls.exists()


def test_browser_manages_project_context_without_a_model(browser_app):
    app, call, calls, _ = browser_app
    assert call("/api/project")[1]["context"]["history"] == []
    assert call("/api/context", {"sources": []})[0] == 400
    code, saved = call("/api/context", {"sources": ["CLAUDE.md"]})
    assert code == 200
    assert saved["documents"][0]["path"] == "CLAUDE.md"
    assert call("/api/context?snapshot=" + saved["id"])[1] == saved
    assert call("/api/project")[1]["context"]["latest"] == saved["id"]
    assert not calls.exists()
    assert call("/api/context", {"sources": ["../outside"]})[0] == 400
    assert app.state()["tasks"] == []


def test_context_selection_keeps_the_workflow_gate(browser_app):
    app, call, _, ready = browser_app
    saved = call("/api/context", {"sources": ["CLAUDE.md"]})[1]
    call(
        "/api/command",
        {"command": "start", "task_id": "context", "request": "Original", "base": "main"},
    )
    wait_for(ready.exists)
    assert call("/api/context", {"sources": ["CLAUDE.md"]})[0] == 400
    assert (
        call("/api/context/save", {"notes": "New", "sources": [], "expected_current": saved["id"]})[
            0
        ]
        == 400
    )
    assert (
        call("/api/context/restore", {"snapshot": saved["id"], "expected_current": saved["id"]})[0]
        == 400
    )
    assert call("/api/context/use", {"task_id": "context", "snapshot": saved["id"]})[0] == 400
    call("/api/stop", {})
    wait_for(lambda: not app.state()["job"]["running"])
    assert call("/api/context/use", {"task_id": "context", "snapshot": saved["id"]})[0] == 400
    call(
        "/api/command",
        {"command": "respond", "task_id": "context", "action": "retry"},
    )
    wait_for(lambda: not app.state()["job"]["running"])
    token = app.state("context")["task"]["prompt"]["approval_token"]
    call(
        "/api/command",
        {"command": "respond", "task_id": "context", "action": "approve", "token": token},
    )
    wait_for(lambda: not app.state()["job"]["running"])
    code, view = call("/api/context/use", {"task_id": "context", "snapshot": saved["id"]})
    assert code == 200
    assert view["project_context_id"] == saved["id"]
    assert view["stage"] == "specification_ready"


def test_browser_backend_settings_are_explicit_and_do_not_invoke_models(browser_app):
    app, call, calls, _ = browser_app
    assert call("/api/backend", {"backend": "unknown"})[0] == 400
    assert call("/api/backend", {"backend": "test"})[0] == 200
    assert app.selected_backend == "test"
    assert not calls.exists()
    assert call("/api/backend", {"backend": "auto"})[0] == 200
    assert app.selected_backend == "auto"


def test_project_settings_resolve_backend_paths_against_the_project(browser_app):
    app, call, calls, _ = browser_app
    executable(app.args.repo, "raise AssertionError('Status must not execute a backend')\n")
    app.settings.backends["test"].command = ["./agent-double"]
    profiles = call("/api/project")[1]["backends"]
    assert next(item for item in profiles if item["name"] == "test")["available"]
    assert not calls.exists()


def test_project_controls_require_same_origin_and_session_token(browser_app):
    _, call, _, _ = browser_app
    assert call("/guide", authenticated=False)[0] == 200
    for path, body in (
        ("/api/project", None),
        ("/api/context/preview", None),
        ("/api/context/save", {"notes": "", "sources": [], "expected_current": None}),
        ("/api/context/restore", {"snapshot": "invalid", "expected_current": None}),
        ("/api/context", {"sources": ["CLAUDE.md"]}),
        ("/api/context/use", {"task_id": "example", "snapshot": "invalid"}),
        ("/api/backend", {"backend": "test"}),
    ):
        assert call(path, body, authenticated=False)[0] == 403
        assert call(path, body, headers={"Origin": "https://attacker.example"})[0] == 403


def test_browser_exploration_can_finish_and_reopen_without_coding(browser_app):
    app, call, calls, _ = browser_app
    executable(
        calls.parent,
        "import json, sys\nfrom pathlib import Path\nrequest = json.load(sys.stdin)\n"
        f"with Path({str(calls)!r}).open('a') as out: out.write(json.dumps(request) + '\\n')\n"
        f"print({json.dumps(ANALYSIS)!r})\n",
    )
    call(
        "/api/command",
        {"command": "explore", "task_id": "research", "request": "Compare options", "base": "main"},
    )
    wait_for(lambda: not app.state()["job"]["running"])
    original = call("/api/state?task=research")[1]["task"]
    assert original["stage"] == "exploration"
    call(
        "/api/command",
        {"command": "respond", "task_id": "research", "action": "question", "text": "done\nWhy?"},
    )
    wait_for(lambda: not app.state()["job"]["running"])
    assert app.state("research")["task"]["stage"] == "exploration"
    call("/api/command", {"command": "respond", "task_id": "research", "action": "done"})
    wait_for(lambda: not app.state()["job"]["running"])
    reopened = LocalApp(app.args)
    try:
        task = reopened.state("research")["task"]
        assert task["stage"] == "exploration_ready"
        assert task["prompt"]["available_phases"] == ["explore", "interview"]
        assert task["worktree"] == original["worktree"]
        assert any(item.get("text") == "done\nWhy?" for item in task["messages"])
        assert Path(task["prompt"]["report"]).is_file()
        assert [json.loads(line)["role"] for line in calls.read_text().splitlines()] == [
            "explorer",
            "explorer",
        ]
    finally:
        reopened.close()


def test_browser_resume_retries_a_provider_failure_once_in_the_same_task(browser_app):
    app, call, calls, _ = browser_app
    executable(
        calls.parent,
        "import sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as out: out.write('failure\\n')\n"
        "print('Usage limit reached', file=sys.stderr)\nsys.exit(1)\n",
    )
    call(
        "/api/command",
        {"command": "start", "task_id": "quota", "request": "Keep this", "base": "main"},
    )
    wait_for(lambda: not app.state()["job"]["running"])
    original = app.state("quota")["task"]
    assert original["stage"] == "backend_blocked"
    assert original["prompt"]["backend_failure"]["kind"] == "quota"
    assert calls.read_text() == "failure\n"
    executable(
        calls.parent,
        "import json, sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
        f"with Path({str(calls)!r}).open('a') as out: out.write('success\\n')\n"
        f"print({json.dumps({'specification': SPECIFICATION, 'questions': []})!r})\n",
    )
    call("/api/command", {"command": "resume", "task_id": "quota"})
    wait_for(lambda: not app.state()["job"]["running"])
    task = app.state("quota")["task"]
    assert task["stage"] == "specification_review"
    assert task["worktree"] == original["worktree"]
    assert task["request"] == "Keep this"
    assert calls.read_text() == "failure\nsuccess\n"


def test_browser_can_require_a_walkthrough_and_restart_without_losing_the_old_task(browser_app):
    app, call, _, ready = browser_app
    call(
        "/api/command",
        {
            "command": "start",
            "task_id": "tour",
            "request": "Original",
            "base": "main",
            "walkthrough": True,
        },
    )
    wait_for(ready.exists)
    call("/api/stop", {})
    wait_for(lambda: not app.state()["job"]["running"])
    original = app.state("tour")["task"]
    call("/api/command", {"command": "restart", "task_id": "tour"})
    wait_for(lambda: not app.state()["job"]["running"])
    fresh = app.state()["job"]["result"]
    assert fresh["task_id"] != original["task_id"]
    assert app.state("tour")["task"] == original
    harness = app.harness()
    try:
        assert harness._state(fresh["task_id"])["walkthrough_enabled"] is True
    finally:
        harness.close()


def test_memory_editor_saves_previews_restores_and_protects_stale_drafts(browser_app):
    app, call, calls, _ = browser_app
    draft = {
        "notes": "# Rules\nJobs are idempotent.",
        "sources": ["CLAUDE.md"],
        "expected_current": None,
    }
    code, first = call("/api/context/save", draft)
    assert code == 200
    assert first["notes"] == draft["notes"]
    assert call("/api/context/preview")[1]["context"]["notes"] == draft["notes"]
    code, second = call(
        "/api/context/save", {**draft, "notes": "Updated rules", "expected_current": first["id"]}
    )
    assert code == 200
    assert call("/api/context/save", draft)[0] == 400
    assert (
        call("/api/context/restore", {"snapshot": first["id"], "expected_current": first["id"]})[0]
        == 400
    )
    assert call("/api/context/save", {"notes": "", "sources": []})[0] == 400
    assert (
        call(
            "/api/context/save",
            {**draft, "expected_current": second["id"], "refresh_sources": "yes"},
        )[0]
        == 400
    )
    assert call(
        "/api/context/restore", {"snapshot": first["id"], "expected_current": second["id"]}
    ) == (200, first)
    assert call("/api/context?snapshot=" + second["id"])[1] == second
    assert len(call("/api/project")[1]["context"]["history"]) == 2
    (app.args.repo / "CLAUDE.md").write_text("Changed local reference")
    preview = call("/api/context/preview?snapshot=" + first["id"])[1]["context"]
    assert preview["changed_sources"] == ["CLAUDE.md"]
    assert "excerpt" not in preview["documents"][0]
    assert not calls.exists()
    assert app.state()["tasks"] == []


def test_task_memory_preview_and_explicit_update_use_the_tasks_pinned_checkout(
    browser_app, monkeypatch
):
    app, call, _, _ = browser_app
    agent = ScriptedAgent()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    agent.responses["chat"].append({"answer": "Initial answer", "references": []})
    call(
        "/api/command",
        {"command": "chat", "task_id": "memory", "base": "main", "request": "Discuss rules"},
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert call("/api/context/preview?task=memory")[1]["context"] is None
    saved = call(
        "/api/context/save",
        {"notes": "Saved rules", "sources": ["CLAUDE.md"], "expected_current": None},
    )[1]
    assert call("/api/context/preview?task=memory")[1]["context"] is None
    assert call("/api/state?task=memory")[1]["task"]["can_use_context"]
    assert call("/api/context/use", {"task_id": "memory", "snapshot": saved["id"]})[0] == 200
    # Changing the main checkout does not change a task's isolated source references.
    (app.args.repo / "CLAUDE.md").write_text("Changed in the main checkout")
    preview = call("/api/context/preview?task=memory")[1]
    assert preview["context"]["changed_sources"] == []
    assert preview["context"]["notes"] == "Saved rules"
    assert call("/api/context/preview")[1]["context"]["changed_sources"] == ["CLAUDE.md"]
    agent.responses["chat"].append({"answer": "Updated answer", "references": []})
    call(
        "/api/command",
        {
            "command": "respond",
            "task_id": "memory",
            "action": "answer",
            "text": "Use the updated rules",
        },
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    assert agent.calls[-1][1]["project_context"] == preview["context"]
    assert len(agent.calls) == 2


def test_chat_memory_save_is_visible_in_history_and_project_editor_after_reopening(
    browser_app, monkeypatch
):
    app, call, _, _ = browser_app
    agent = ScriptedAgent()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    note = "Network effects stay outside the pure core."
    agent.responses["chat"].append(
        {"answer": "Here is the note to remember.", "references": [], "memory_note": note}
    )
    assert (
        call(
            "/api/command",
            {
                "command": "chat",
                "task_id": "remember",
                "base": "main",
                "request": "Save this to project memory: " + note,
            },
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    task = call("/api/state?task=remember")[1]["task"]
    saved = call("/api/context")[1]
    assert saved["notes"] == note
    assert task["project_context_id"] == saved["id"]
    assert task["messages"][-1]["memory_save"] == {
        "status": "saved",
        "note": note,
        "snapshot": saved["id"],
    }
    assert task["prompt"]["actions"] == ["answer"]
    assert agent.calls[0][1]["latest_user_message"] == "Save this to project memory: " + note
    reopened = LocalApp(app.args)
    try:
        assert reopened.state("remember")["task"] == task
        assert reopened.context_snapshot()["notes"] == note
    finally:
        reopened.close()


def test_agents_page_builds_reviews_and_saves_to_git_without_another_model_call(
    browser_app, tmp_path, monkeypatch
):
    from .test_agent_builder import DEFINITION, DRAFT, agent_repo

    app, call, calls, _ = browser_app
    root = agent_repo(tmp_path)
    assert call("/agents")[0] == 200
    assert 'id="agent-grid"' in call("/agents")[1]
    assert call("/api/agents", authenticated=False)[0] == 403
    assert call("/api/agents/source", {"repository": str(root)})[0] == 200
    assert call("/api/agents")[1]["profiles"] == []
    agent = ScriptedAgent()
    agent.responses["agent_builder"].append(DRAFT)
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    assert (
        call(
            "/api/command",
            {"command": "agent_builder", "task_id": "rust-agent", "request": "A Rust reviewer"},
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    current = call("/api/state?task=rust-agent")[1]
    assert current["job"]["error"] is None
    assert current["tasks"][0]["task_kind"] == "agent_builder"
    assert call("/api/agents")[1]["profiles"] == []

    def no_model(*args):
        raise AssertionError("Saving must not probe or invoke a backend")

    monkeypatch.setattr("lambda_harness.webapp.select_backend", no_model)
    assert (
        call(
            "/api/command",
            {
                "command": "respond",
                "task_id": "rust-agent",
                "action": "save_agent",
                "token": current["task"]["prompt"]["approval_token"],
            },
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    saved = call("/api/state?task=rust-agent")[1]
    assert saved["job"]["error"] is None
    assert saved["task"]["stage"] == "agent_ready"
    profile = call("/api/agents")[1]["profiles"][0]
    assert json.loads((root / profile["path"]).read_text()) == DEFINITION
    assert len(agent.calls) == 1 and not calls.exists()
    assert call("/api/agents/assign", {"role": "reviewer", "agent_id": profile["id"]})[0] == 200
    assert call("/api/agents")[1]["selected"] == {"reviewer": profile["id"]}
    file = root / profile["path"]
    content = file.read_text()
    file.unlink()
    assert call("/api/agents")[1]["selected"] == {"reviewer": profile["id"]}
    file.write_text(content)
    assert call("/api/agents/assign", {"role": "implementer", "agent_id": profile["id"]})[0] == 400
    # Restarting the website remembers the repository, not a copied local catalog.
    reopened = LocalApp(app.args)
    assert reopened.agents()["repository"] == str(root)
    assert reopened.agents()["profiles"] == [profile]
    reopened.close()


def test_agent_repository_and_assignments_cannot_change_during_a_model_call(browser_app, tmp_path):
    _, call, _, ready = browser_app
    call("/api/command", {"command": "start", "task_id": "busy", "request": "Test", "base": "main"})
    wait_for(ready.exists)
    assert call("/api/agents/assign", {"role": "chat", "agent_id": None})[0] == 400
    assert call("/api/agents/source", {"repository": str(tmp_path)})[0] == 400
    assert call("/api/agents")[0] == 200
    call("/api/stop", {})
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])


def test_missing_agent_checkout_does_not_break_status_or_repository_selection(
    browser_app, tmp_path
):
    from .test_agent_builder import agent_repo

    _, call, _, _ = browser_app
    root = agent_repo(tmp_path)
    assert call("/api/agents/source", {"repository": str(root)})[0] == 200
    root.rename(tmp_path / "moved-catalog")
    assert call("/api/state")[0] == 200
    code, catalog = call("/api/agents")
    assert code == 200 and catalog["error"] and not catalog["writable"]
    assert call("/api/agents/source", {"repository": str(tmp_path / "moved-catalog")})[0] == 200
    assert call("/api/agents")[1]["error"] is None


@pytest.mark.parametrize("path", ["/agents", "/project-memory", "/project-settings"])
def test_project_pages_are_available_by_direct_url(browser_app, path):
    app, call, calls, _ = browser_app
    status, content = call(path)
    assert status == 200
    assert app.token in content
    assert 'id="agents-page"' in content
    assert 'id="memory-panel"' in content
    assert 'id="project-panel"' in content
    assert not calls.exists()


def test_project_identity_memory_and_backend_preferences_are_isolated(browser_app, tmp_path):
    import copy

    app, call, calls, _ = browser_app
    first = call("/api/project")[1]["project"]
    assert first["name"] == app.args.repo.name
    assert first["repository"] == str(app.args.repo)
    assert call("/api/state")[1]["project"] == first
    assert call("/api/agents")[1]["project"] == first
    assert call("/api/backend", {"backend": "codex"})[0] == 200
    assert (
        call(
            "/api/context/save",
            {"notes": "Only for the first project", "sources": [], "expected_current": None},
        )[0]
        == 200
    )
    other_path = tmp_path / "other" / app.args.repo.name
    other_path.parent.mkdir()
    git(tmp_path, "clone", str(app.args.repo), str(other_path))
    args = copy.copy(app.args)
    args.repo = other_path
    other = LocalApp(args)
    reopened = LocalApp(app.args)
    try:
        second = other.project()["project"]
        assert second["name"] == first["name"] and second["id"] != first["id"]
        assert other.project()["context"]["latest"] is None
        assert other.project()["backend"] == "test"
        assert reopened.project()["backend"] == "codex"
        assert reopened.project()["backend_origin"] == "project"
        assert reopened.context_snapshot()["notes"] == "Only for the first project"
        other.project_change("backend", {"backend": "auto"})
        assert reopened.project()["backend"] == "codex"
        reopened.selected_backend = "test"
        assert reopened.project()["backend"] == "codex"
        assert not calls.exists()
    finally:
        other.close()
        reopened.close()


def test_project_urls_cannot_open_another_projects_memory_or_settings(browser_app):
    _, call, _, _ = browser_app
    project = call("/api/project")[1]["project"]
    for page in ["memory", "settings"]:
        assert call(f"/projects/{project['id']}/{page}")[0] == 200
        assert call(f"/projects/not-this-project/{page}")[0] == 404


def test_automatic_backend_choice_is_restored_after_restarting(browser_app):
    app, call, _, _ = browser_app
    assert call("/api/backend", {"backend": "auto"})[0] == 200
    app.selected_backend = "codex"
    reopened = LocalApp(app.args)
    try:
        assert app.state()["backend"] == "auto"  # Another task cannot change the project default.
        assert reopened.project()["backend"] == "auto"
        assert reopened.project()["backend_origin"] == "project"
        assert reopened.state()["backend"] == "auto"
    finally:
        reopened.close()


def test_unavailable_saved_backend_allows_the_project_to_open_and_be_reconfigured(browser_app):
    from lambda_harness.project_context import state_directory
    from lambda_harness.project_settings import ProjectSettings

    app, _, _, _ = browser_app
    with ProjectSettings(state_directory(app.args.repo, app.args.state_dir)) as settings:
        settings.save_backend("removed-profile")
    reopened = LocalApp(app.args)
    try:
        assert reopened.project()["backend"] == "test"
        assert reopened.project()["unavailable_backend"] == "removed-profile"
        reopened.project_change("backend", {"backend": "auto"})
        assert reopened.project()["unavailable_backend"] is None
    finally:
        reopened.close()


def test_projects_keep_requests_and_saved_work_isolated_across_tabs(
    browser_app, tmp_path, monkeypatch
):
    app, call, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    agent.responses["explorer"].extend([ANALYSIS, ANALYSIS])
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    monkeypatch.setattr("lambda_harness.webapp.LocalExecutor", lambda **kw: executor)
    first = call("/api/project")[1]["project"]
    second_path = tmp_path / "second"
    git(tmp_path, "clone", str(app.args.repo), str(second_path))
    code, second = call("/api/projects", {"repository": str(second_path)})
    assert code == 200
    assert call("/api/projects", {"repository": str(second_path)})[1] == second
    assert len(call("/api/projects")[1]["projects"]) == 2
    for project in [first, second]:
        header = {"X-Harness-Project": project["id"]}
        assert call(f"/projects/{project['id']}")[0] == 200
        assert call(f"/projects/{project['id']}/agents")[0] == 200
        assert call("/api/state", headers=header)[1]["project"] == project
        assert (
            call(
                "/api/command",
                {
                    "command": "explore",
                    "task_id": "same-name",
                    "request": project["name"],
                    "base": "main",
                },
                headers=header,
            )[0]
            == 202
        )
        wait_for(lambda header=header: not call("/api/state", headers=header)[1]["job"]["running"])
        assert (
            call(
                "/api/context/save",
                {
                    "notes": project["name"],
                    "sources": [],
                    "expected_current": None,
                },
                headers=header,
            )[0]
            == 200
        )
    for project in [first, second]:
        header = {"X-Harness-Project": project["id"]}
        assert (
            call("/api/state?task=same-name", headers=header)[1]["task"]["request"]
            == project["name"]
        )
        assert call("/api/context", headers=header)[1]["notes"] == project["name"]
    assert call("/api/state")[1]["project"] == first
    assert (
        call("/api/backend", {"backend": "codex"}, headers={"X-Harness-Project": second["id"]})[0]
        == 200
    )
    assert call("/api/project")[1]["backend"] == "test"
    assert (
        call("/api/projects/remove", {"project_id": first["id"], "confirmation": first["id"]})[0]
        == 200
    )
    assert call("/api/state")[1]["project"] is None
    assert call("/api/state", headers={"X-Harness-Project": first["id"]})[0] == 400
    assert (
        call("/api/backend", {"backend": "auto"}, headers={"X-Harness-Project": first["id"]})[0]
        == 400
    )
    assert call(f"/projects/{first['id']}/memory")[0] == 404
    assert (app.args.repo / ".git").exists()
    assert call("/api/projects", {"repository": str(app.args.repo)})[1] == first
    header = {"X-Harness-Project": first["id"]}
    assert call("/api/context", headers=header)[1]["notes"] == first["name"]
    assert call("/api/state?task=same-name", headers=header)[1]["task"]["request"] == first["name"]


def test_empty_project_list_can_add_again_and_requires_authentication(browser_app):
    app, call, calls, _ = browser_app
    project_id = app.identity["id"]
    assert call("/api/projects", authenticated=False)[0] == 403
    assert call("/api/projects", {"repository": str(app.args.repo)}, authenticated=False)[0] == 403
    assert (
        call("/api/projects/remove", {"project_id": project_id, "confirmation": project_id})[0]
        == 200
    )
    assert call("/api/projects")[1]["projects"] == []
    assert call("/projects")[0] == 200
    assert call("/api/state")[1]["tasks"] == []
    assert (
        call("/api/command", {"command": "chat", "task_id": "no-project", "request": "hello"})[0]
        == 400
    )
    assert call("/api/projects", {"repository": str(app.args.repo)})[0] == 200
    assert not calls.exists()


def test_permission_notifications_include_unselected_tasks_and_ignore_question_tokens(
    browser_app, repository
):
    from .test_change_permissions import permission_gate

    app, _, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    harness = app.harness()
    try:
        harness.agent, harness.execute = agent, executor
        view = permission_gate((harness, agent, executor), repository)
        pending = app.state()["tasks"][0]["permission_request"]
        updated = harness.respond("task", "question", text="Why this dependency?")
        assert updated["prompt"]["approval_token"] != view["prompt"]["approval_token"]
        assert app.state()["tasks"][0]["permission_request"] == pending
        state = harness._state("task")
        state["next"] = "validate"
        harness.graph.update_state(harness._config("task"), state, as_node="reply")
        assert "permission_request" not in app.state()["tasks"][0]
    finally:
        harness.close()


def test_blocked_validation_emits_stable_attention_notification(browser_app, repository):
    from .test_implementation_conversation import blocked_implementation

    app, _, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    harness = app.harness()
    try:
        harness.agent, harness.execute = agent, executor
        blocked_implementation((harness, agent, executor), repository)
        attention = app.state()["tasks"][0]["attention_request"]
        harness.respond("task", "question", text="Why did it stop?")
        assert app.state()["tasks"][0]["attention_request"] == attention
    finally:
        harness.close()


def test_browser_automatic_execution_stops_on_error_and_accepts_selected_repair(
    browser_app, monkeypatch
):
    from .test_phases import approved_tests

    app, call, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    monkeypatch.setattr("lambda_harness.webapp.LocalExecutor", lambda **kw: executor)
    harness = app.harness(agent)
    try:
        approved_tests(harness)
    finally:
        harness.close()
    executor.lint_error = True
    assert (
        call(
            "/api/command",
            {"command": "run", "task_id": "task", "phase": "implement", "automatic": True},
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=task")[1]
    assert state["job"]["error"] is None
    assert state["task"]["outcome"]["kind"] == "error"
    assert state["task"]["mode"] == "full"
    assert [r for r, _, _ in agent.calls].count("implementer") == 1
    executor.lint_error = False
    assert (
        call(
            "/api/command",
            {"command": "respond", "task_id": "task", "action": "repair", "automatic": False},
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=task")[1]
    assert state["task"]["stage"] == "implementation_ready"
    assert state["task"]["mode"] == "phased"
    assert state["task"]["outcome"]["kind"] == "success"
    assert "refactorer" not in [r for r, _, _ in agent.calls]


def test_browser_rejects_invalid_automatic_choice(browser_app):
    _, call, _, _ = browser_app
    code, response = call(
        "/api/command",
        {"command": "run", "task_id": "task", "phase": "implement", "automatic": "yes"},
    )
    assert code == 400
    assert "Automatic" in response["error"]


def test_browser_discovers_then_executes_checks_without_another_model_request(
    browser_app, monkeypatch
):
    from .test_check_runner import CheckExecutor, plan

    app, call, _, _ = browser_app
    agent, executor = ScriptedAgent(), CheckExecutor()
    agent.responses["test_runner"].append(plan())
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    monkeypatch.setattr("lambda_harness.webapp.LocalExecutor", lambda **kw: executor)
    assert (
        call("/api/command", {"command": "checks", "task_id": "all-checks", "base": "main"})[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    view = call("/api/state?task=all-checks")[1]["task"]
    assert view["stage"] == "checks_plan" and not executor.calls
    monkeypatch.setattr(
        "lambda_harness.webapp.select_backend", lambda *a: pytest.fail("Unexpected backend check")
    )
    assert (
        call(
            "/api/command",
            {
                "command": "respond",
                "task_id": "all-checks",
                "action": "run_checks",
                "token": view["prompt"]["approval_token"],
            },
        )[0]
        == 202
    )
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    view = call("/api/state?task=all-checks")[1]["task"]
    assert view["stage"] == "checks_complete"
    assert len(executor.calls) == 4
    assert len(agent.calls) == 1


def test_task_chat_endpoint_keeps_saved_phase_and_returns_conversation(browser_app, monkeypatch):
    app, call, _, _ = browser_app
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    monkeypatch.setattr("lambda_harness.webapp.RoutedAgent", lambda *a, **kw: agent)
    monkeypatch.setattr("lambda_harness.webapp.select_backend", lambda *a: "test")
    monkeypatch.setattr("lambda_harness.webapp.LocalExecutor", lambda **kw: executor)
    harness = app.harness(agent)
    try:
        before = harness.start("task", "Fix addOne")
    finally:
        harness.close()
    agent.responses["interviewer"].append({"answer": "Suggested commit subject"})
    status, _ = call(
        "/api/command",
        {"command": "task_chat", "task_id": "task", "text": "Suggest a commit message"},
    )
    assert status == 202
    wait_for(lambda: not call("/api/state")[1]["job"]["running"])
    state = call("/api/state?task=task")[1]
    assert state["job"]["error"] is None
    assert state["task"]["stage"] == before["stage"]
    assert state["task"]["prompt"] == before["prompt"]
    assert state["task"]["messages"][-1]["answer"] == "Suggested commit subject"
    assert state["task"]["can_task_chat"]
    assert not executor.calls


def test_tasks_run_concurrently_with_isolated_models_worktrees_logs_and_stop(browser_app, tmp_path):
    from lambda_harness.config import Backend

    app, call, _, _ = browser_app
    script = executable(
        tmp_path,
        "import json, sys, time\nfrom pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "name = request['context']['request']\n"
        f"root = Path({str(tmp_path)!r})\n"
        "(root / (name + '.request')).write_text(json.dumps(request))\n"
        "while not (root / (name + '.release')).exists(): time.sleep(.01)\n"
        "print(json.dumps({'answer': name + ' completed', 'references': []}))\n",
    )
    app.settings.backends["test"] = Backend(command=[script], input_format="json")
    for name in ("alpha", "beta"):
        code, _ = call(
            "/api/command",
            {
                "command": "chat",
                "task_id": name,
                "request": name,
                "base": "main",
                "backend": "test",
                "model": name + "-model",
            },
        )
        assert code == 202
        wait_for((tmp_path / (name + ".request")).exists)
    alpha = call("/api/state?task=alpha")[1]
    beta = call("/api/state?task=beta")[1]
    assert alpha["job"]["task_id"] == "alpha" and beta["job"]["task_id"] == "beta"
    assert alpha["job"]["running"] and beta["job"]["running"]
    assert alpha["can_stop"] and beta["can_stop"]
    assert alpha["task"]["worktree"] != beta["task"]["worktree"]
    for name, state, other in [("alpha", alpha, "beta"), ("beta", beta, "alpha")]:
        assert state["task"]["model_selection"]["model"] == name + "-model"
        assert name + "-model" in "\n".join(state["job"]["log"])
        assert other + "-model" not in "\n".join(state["job"]["log"])
        request = json.loads((tmp_path / (name + ".request")).read_text())
        assert request["model"] == name + "-model"
        assert request["context"]["request"] == name
    assert call("/api/command", {"command": "resume", "task_id": "alpha"})[0] == 400
    assert call("/api/stop", {})[0] == 400  # Ambiguous stop cannot cancel a random task.
    assert not call("/api/stop", {"task_id": "alpha", "job_id": "stale"})[1]["stopping"]
    assert call("/api/stop", {"task_id": "beta", "job_id": beta["job"]["id"]})[1]["stopping"]
    wait_for(lambda: not call("/api/state?task=beta")[1]["job"]["running"])
    assert call("/api/state?task=beta")[1]["task"]["stage"] == "agent_paused"
    alpha = call("/api/state?task=alpha")[1]
    assert alpha["job"]["running"] and not alpha["stopping"]
    assert call("/api/state?task=unstarted")[1]["job"] is None
    assert call("/api/state?task=unstarted")[1]["project_running"]
    # Project-wide mutations stay blocked even though the latest job (beta) has finished.
    assert call("/api/backend", {"backend": "test"})[0] == 400
    with pytest.raises(ValueError, match="current operation"):
        app.reserve_handoff("export")
    original_beta = call("/api/state?task=beta")[1]["task"]
    (tmp_path / "beta.release").touch()
    assert call("/api/command", {"command": "restart", "task_id": "beta"})[0] == 202
    wait_for(lambda: not call("/api/state?task=beta")[1]["job"]["running"])
    restarted = call("/api/state?task=beta")[1]["job"]
    assert restarted["error"] is None
    assert restarted["result"]["task_id"] == "beta-restart-1"
    assert restarted["result"]["worktree"] != original_beta["worktree"]
    assert call("/api/state?task=beta")[1]["task"] == original_beta
    assert call("/api/state?task=alpha")[1]["job"]["running"]
    (tmp_path / "alpha.release").touch()
    wait_for(lambda: not call("/api/state?task=alpha")[1]["job"]["running"])
    alpha = call("/api/state?task=alpha")[1]
    assert alpha["job"]["error"] is None
    assert alpha["task"]["messages"][-1]["answer"] == "alpha completed"
    assert not alpha["project_running"]
    assert "alpha completed" not in json.dumps(call("/api/state?task=beta")[1]["task"]["messages"])


def test_review_model_and_effort_reach_both_reviewers_and_survive_replies(browser_app, tmp_path):
    from lambda_harness.backends import Backend

    app, call, calls, _ = browser_app
    script = executable(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        "if '--models' in sys.argv:\n"
        " print(json.dumps({'models':[{'id':'chosen-model','efforts':['low','high']}]}))\n"
        " sys.exit(0)\n"
        "r=json.load(sys.stdin)\n"
        f"with Path({str(calls)!r}).open('a') as f: f.write(json.dumps(r)+'\\n')\n"
        "print(json.dumps({'answer':'Explanation'} if r['context'].get('intent')=='explain' "
        "else {'summary':'Reviewed','findings':[]}))\n",
    )
    app.settings.backends["test"] = Backend(
        command=[script], input_format="json", models_command=["{executable}", "--models"]
    )
    git(app.args.repo, "checkout", "-b", "feature")
    (app.args.repo / SOURCE).write_text(IMPLEMENTED)
    git(app.args.repo, "commit", "-am", "Change")
    assert (
        call(
            "/api/command",
            {
                "command": "review",
                "task_id": "effort-review",
                "request": "Review",
                "base": "main",
                "head": "feature",
                "backend": "test",
                "model": "chosen-model",
                "effort": "high",
                "reviewers": "both",
            },
        )[0]
        == 202
    )
    wait_for(lambda: app.job and not app.job["running"])
    assert app.job["error"] is None, app.job
    view = call("/api/state?task=effort-review")[1]["task"]
    assert view["model_selection"] == {"backend": "test", "model": "chosen-model", "effort": "high"}
    assert set(view["messages"][-1]["generations"]) == {"haskell_reviewer", "security_reviewer"}
    assert all(g["effort"] == "high" for g in view["messages"][-1]["generations"].values())
    app.args.model = "different-default"
    for effort in ("saved", None, "low"):
        command = {
            "command": "respond",
            "task_id": "effort-review",
            "action": "question",
            "text": "Explain",
        }
        if effort != "saved":
            command["effort"] = effort
        assert call("/api/command", command)[0] == 202
        wait_for(lambda: not app.job["running"])
        assert app.job["error"] is None, app.job
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [r["model"] for r in requests] == ["chosen-model"] * 8
    assert [r.get("effort") for r in requests] == ["high"] * 4 + [None] * 2 + ["low"] * 2
    assert [r["role"] for r in requests] == ["haskell_reviewer", "security_reviewer"] * 4
    # Reject an unsupported setting before invoking either reviewer or changing preferences.
    count = len(requests)
    assert call("/api/command", {**command, "effort": "ultra"})[0] == 202
    wait_for(lambda: not app.job["running"])
    assert "not supported" in app.job["error"]
    assert len(calls.read_text().splitlines()) == count
    assert call("/api/state?task=effort-review")[1]["task"]["model_selection"]["effort"] == "low"
