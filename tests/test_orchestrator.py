"""Conversational delegation executes in one task and survives partial work."""

import copy
import json
from collections import deque
from pathlib import Path

import pytest
from pydantic import ValidationError

from lambda_harness.failures import AgentInterrupted
from lambda_harness.schemas import OrchestratorDecision

from .conftest import BROKEN, IMPLEMENTED, SOURCE, TEST, git


def delegate(worker, instruction):
    return dict(
        answer=instruction,
        action="delegate",
        status="working",
        worker=worker,
        instruction=instruction,
    )


def answer(text="Done"):
    return dict(answer=text, action="respond", status="complete")


class NativeAgent:
    def __init__(self, decisions, worker=None):
        self.decisions = deque(decisions)
        self.worker = worker
        self.calls = []
        self.last_commands = []

    def __call__(self, role, context, cwd):
        self.last_commands = []
        self.calls.append((role, copy.deepcopy(context), Path(cwd)))
        if role == "orchestrator":
            return self.decisions.popleft()
        return self.worker(self, role, context, Path(cwd))


def test_question_answers_without_workers_or_phase_prerequisites(rig):
    h, _, execute = rig
    agent = NativeAgent([answer("Use: fix: increment the input")])
    h.agent = agent
    view = h.start("question", "Suggest a commit message", phase="orchestrator")
    assert view["stage"] == "orchestrator_ready", view["prompt"]
    assert h._state("question")["messages"][-1]["answer"] == "Use: fix: increment the input"
    assert view["orchestrator_enabled"]
    assert [role for role, _, _ in agent.calls] == ["orchestrator"]
    assert not execute.calls
    assert not git(Path(view["worktree"]), "status", "--porcelain")


def test_delegation_repairs_failed_checks_and_finishes_in_same_turn(rig):
    h, _, _ = rig
    attempts = []

    def worker(agent, role, context, cwd):
        attempts.append(role)
        assert "Keep existing tests" in context["latest_user_message"]
        if role == "coding_worker":
            (cwd / SOURCE).write_text(IMPLEMENTED)
        if role == "check_worker":
            failed = attempts.count(role) == 1
            agent.last_commands = [{"command": "check", "exit_code": 1 if failed else 0}]
            return dict(
                summary="Failed" if failed else "Passed", status="blocked" if failed else "complete"
            )
        return dict(summary="Edited source", status="complete")

    agent = NativeAgent(
        [
            delegate("coding_worker", "Implement and preserve tests"),
            delegate("check_worker", "Run checks"),
            delegate("coding_worker", "Repair the reported failure"),
            delegate("check_worker", "Recheck"),
            answer("Implemented; checks pass."),
        ],
        worker,
    )
    h.agent = agent
    view = h.start("work", "Implement. Keep existing tests", phase="orchestrator")
    assert view["stage"] == "orchestrator_ready", view["prompt"]
    assert attempts == ["coding_worker", "check_worker", "coding_worker", "check_worker"]
    root = Path(view["worktree"])
    assert (root / SOURCE).read_text() == IMPLEMENTED
    assert (root / TEST).read_text() == "-- Existing tests\n"
    assert (h.repository / SOURCE).read_text() == BROKEN
    assert git(root, "rev-parse", "HEAD") == view["base"]  # no unsolicited commit
    assert all(cwd == root for _, _, cwd in agent.calls)
    controllers = [ctx for role, ctx, _ in agent.calls if role == "orchestrator"]
    assert controllers[2]["worker_results"][-1]["commands"][0]["exit_code"] == 1
    assert controllers[-1]["worker_results"][-1]["commands"][0]["exit_code"] == 0
    for result in controllers[-1]["worker_results"]:
        assert json.loads(Path(result["receipt"]).read_text())["summary"] == result["summary"]


def test_existing_blocked_task_adopted_without_recreating_worktree_or_other_task(rig):
    h, _, _ = rig
    original = h.start("old", "Implement the arithmetic change")
    other = h.start("other", "Unrelated task")
    state = h._state("old")
    state["checks_origin"] = {"stage": "tests_requested"}
    h._phase_complete(state, "tests_requested", "Tests must be written")
    h.graph.update_state(h._config("old"), state, as_node="finish")
    (Path(original["worktree"]) / "notes.txt").write_text("Preserve me")
    h.agent = NativeAgent([answer("I can continue here.")])
    view = h.orchestrate("old", "Skip that process and use my current work")
    assert view["stage"] == "orchestrator_ready", view["prompt"]
    assert view["worktree"] == original["worktree"]
    assert (Path(view["worktree"]) / "notes.txt").read_text() == "Preserve me"
    assert "checks_origin" not in h._state("old")
    assert h.inspect("other") == other
    assert "Unrelated task" not in json.dumps(h.agent.calls, default=str)


def test_stopped_worker_preserves_edits_and_next_message_gets_partial_result(rig):
    h, _, _ = rig

    def stopped(agent, role, context, cwd):
        (cwd / SOURCE).write_text(IMPLEMENTED)
        raise AgentInterrupted()

    h.agent = NativeAgent([delegate("coding_worker", "Implement")], stopped)
    view = h.start("stop", "Implement", phase="orchestrator")
    assert view["stage"] == "agent_paused", view["prompt"]
    assert (Path(view["worktree"]) / SOURCE).read_text() == IMPLEMENTED
    h.agent = NativeAgent(
        [answer("Recovered partial work")], lambda *_: pytest.fail("Worker replayed")
    )
    # A retry resumes the interrupted node but must not repeat the native write.
    view = h.respond("stop", "retry")
    assert view["stage"] == "orchestrator_ready", view["prompt"]
    result = h.agent.calls[0][1]["worker_results"][-1]
    assert "interrupted" in result["summary"]
    assert result["files_changed"]
    assert Path(result["receipt"]).exists()


def test_controller_cannot_end_turn_with_promise_of_future_work():
    with pytest.raises(ValidationError):
        OrchestratorDecision.model_validate(
            dict(answer="I will implement", action="respond", status="working")
        )


def test_readonly_worker_edits_are_reported_as_blocked(rig):
    h, _, _ = rig

    def wrong_worker(agent, role, context, cwd):
        (cwd / SOURCE).write_text(IMPLEMENTED)
        return dict(summary="Inspected", status="complete")

    h.agent = NativeAgent([delegate("research_worker", "Inspect only"), answer()], wrong_worker)
    h.start("readonly", "Investigate", phase="orchestrator")
    result = h.agent.calls[-1][1]["worker_results"][-1]
    assert result["status"] == "blocked"
    assert "changed files" in result["summary"]


def test_controller_context_does_not_copy_old_source_patches_or_unbounded_logs(rig):
    from lambda_harness.orchestrator import controller_results

    h, _, _ = rig
    h.start("large", "Fix the issue")
    state = h._state("large")
    state["messages"].append({"role": "implementer", "changes": [{"content": "x" * 600_000}]})
    h.graph.update_state(h._config("large"), state, as_node="finish")
    h.agent = NativeAgent([answer()])
    h.orchestrate("large", "Continue")
    assert len(json.dumps(h.agent.calls[0][1])) < 100_000
    results = [
        {
            "summary": "Done",
            "commands": [{"command": "check", "output_tail": "x" * 8000} for _ in range(1000)],
        }
    ]
    assert len(json.dumps(controller_results(results))) < 40_000
    assert len(results[0]["commands"]) == 1000  # full receipt stays intact
