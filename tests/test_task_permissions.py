"""Explicit task grants persist, reach workers, and release pending requests."""

import threading

import pytest

from lambda_harness.config import RoutedAgent, load_settings
from lambda_harness.project_settings import ProjectSettings

from .conftest import ScriptedAgent
from .test_webapp import browser_app as permissions_app  # noqa: F401
from .test_webapp import wait_for


def test_permission_preference_survives_restart_without_leaking_to_other_tasks_or_projects(
    tmp_path,
):
    with ProjectSettings(tmp_path / "first") as prefs:
        assert prefs.task_permissions("task") == "ask"
        prefs.save_task_permissions("task", "full")
        with pytest.raises(ValueError):
            prefs.save_task_permissions("task", "typo")
    with ProjectSettings(tmp_path / "first") as prefs:
        assert prefs.task_permissions("task") == "full"
        assert prefs.task_permissions("other") == "ask"
    with ProjectSettings(tmp_path / "second") as prefs:
        assert prefs.task_permissions("task") == "ask"


def test_each_worker_reads_latest_user_selected_policy():
    mode = "ask"
    agent = RoutedAgent(
        load_settings(),
        backend="codex",
        model="chosen-model",
        effort="high",
        permission_mode=lambda: mode,
    )
    assert agent.resolve("orchestrator").permission_mode == "ask"
    mode = "full"
    for role in (
        "orchestrator",
        "coding_worker",
        "check_worker",
        "research_worker",
        "review_worker",
    ):
        resolved = agent.resolve(role)
        assert resolved.permission_mode == "full"
        assert resolved.model == "chosen-model" and resolved.effort == "high"


def test_browser_permission_setting_is_authenticated_scoped_and_reversible(request):
    app, call, _, _ = request.getfixturevalue("permissions_app")
    h = app.harness(ScriptedAgent())
    try:
        h.start("first", "Implement")
        h.start("second", "Inspect")
    finally:
        h.close()
    body = {"task_id": "first", "mode": "full"}
    assert call("/api/task-permissions", body, authenticated=False)[0] == 403
    assert app.task_permissions("first") == "ask"
    assert call("/api/task-permissions", {**body, "mode": "bogus"})[0] == 400
    assert call("/api/task-permissions", {**body, "task_id": "missing"})[0] == 400
    assert call("/api/task-permissions", body)[0] == 200
    assert call("/api/state?task=first")[1]["task"]["permission_mode"] == "full"
    assert call("/api/state?task=second")[1]["task"]["permission_mode"] == "ask"
    assert call("/api/task-permissions", {**body, "mode": "ask"})[0] == 200
    assert app.task_permissions("first") == "ask"


def test_allow_everything_releases_current_request_and_covers_future_requests(request):
    from lambda_harness.cancellation import ModelCancellation

    app, call, _, _ = request.getfixturevalue("permissions_app")
    h = app.harness(ScriptedAgent())
    try:
        h.start("task", "Implement")
    finally:
        h.close()
    job = {"id": "job", "task_id": "task", "running": True, "log": []}
    app.jobs["task"] = job
    app.cancellations["task"] = ModelCancellation()
    cancel = threading.Event()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            app.request_permission(
                job,
                {
                    "method": "item/commandExecution/requestApproval",
                    "details": {"command": "nix build"},
                },
                cancel,
            )
        )
    )
    worker.start()
    pending = wait_for(lambda: job.get("permission"))
    body = {"task_id": "task", "mode": "full", "job_id": "job", "request_id": pending["id"]}
    assert call("/api/task-permissions", {**body, "request_id": "stale"})[0] == 400
    assert app.task_permissions("task") == "ask"
    assert call("/api/task-permissions", body)[0] == 200
    worker.join(5)
    assert not worker.is_alive() and results == [True]
    assert "permission" not in job
    assert app.request_permission(
        job, {"method": "item/permissions/requestApproval", "details": {}}, cancel
    )
    assert call("/api/task-permissions", {"task_id": "task", "mode": "ask"})[0] == 400
    cancel.set()
    assert not app.request_permission(
        job, {"method": "item/permissions/requestApproval", "details": {}}, cancel
    )
    job["running"] = False
    assert call("/api/task-permissions", {"task_id": "task", "mode": "ask"})[0] == 200


@pytest.mark.parametrize("mode", ["ask", "full"])
@pytest.mark.parametrize("role", ["orchestrator", "coding_worker", "check_worker"])
def test_backend_passes_effective_task_policy_to_native_session(monkeypatch, tmp_path, mode, role):
    import json

    from lambda_harness.backends import Backend, CommandAgent

    calls = []

    def session(*args, **kwargs):
        calls.append(kwargs)
        result = (
            {"answer": "Done", "action": "respond", "status": "complete"}
            if role == "orchestrator"
            else {"summary": "Done", "status": "complete"}
        )
        return json.dumps(result), "chosen-model"

    monkeypatch.setattr("lambda_harness.codex_session.run_session", session)
    agent = CommandAgent(
        Backend(command=["/bin/echo"], approval_command=["/bin/echo"], models_format="codex"),
        name="codex",
        model="chosen-model",
        effort="high",
        approval=lambda *_: False,
        permission_mode=mode,
    )
    agent(role, {}, tmp_path)
    assert calls[0]["sandbox"] == (
        "danger-full-access"
        if mode == "full"
        else "read-only"
        if role == "orchestrator"
        else "workspace-write"
    )
    assert calls[0]["approval_policy"] == ("never" if mode == "full" else "on-request")
    assert calls[0]["model"] == "chosen-model" and calls[0]["effort"] == "high"
