"""Partial feature drafts hand off the full scope without inventing test evidence."""

import copy
from pathlib import Path

import pytest

from lambda_harness.recovery import COVERAGE_ERROR, coverage_gap

from .conftest import FAILURE_ID, SPECIFICATION, TEST, TEST_CONTENT
from .test_task_instructions import approved


def partial_scope():
    scope = copy.deepcopy(SPECIFICATION)
    scope["acceptance_criteria"].append(
        {"id": "A2", "description": "Update the protocol model and its specification"}
    )
    return scope


def draft():
    return {
        "summary": "Draft behavior tests; protocol verification remains.",
        "changes": [{"path": TEST, "content": TEST_CONTENT}],
        "expected_failures": [FAILURE_ID],
        "coverage": {"A1": [FAILURE_ID], "A2": []},
        "commit_message": "test: initial feature coverage",
    }


def legacy_draft(h, agent):
    agent.responses["interviewer"].append({"specification": partial_scope(), "questions": []})
    approved(h)
    state = h._state("task")
    state["retry_node"] = "tests"
    state["rejected_test_proposal"] = draft()
    h._gate(
        state,
        "agent_error",
        {"error": COVERAGE_ERROR, "coverage_gap": coverage_gap(state["specification"], draft())},
        ["retry"],
    )
    h.graph.invoke(state, h._config("task"))
    return h.inspect("task")


def test_partial_feature_tests_keep_scope_through_implementation_and_reviews(rig):
    h, agent, execute = rig
    agent.responses["interviewer"].append({"specification": partial_scope(), "questions": []})
    approved(h)
    agent.responses["test_author"].append(draft())
    view = h.run_phase("task", "tests", automatic=True)
    assert view["stage"] == "test_review", view["prompt"]
    assert execute.calls
    assert view["prompt"]["validation"]["status"] == "expected_red"
    assert view["prompt"]["criteria_without_tests"] == [partial_scope()["acceptance_criteria"][1]]
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
    view = h.respond("task", "approve", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "ready", view["prompt"]
    for role, context, _ in agent.calls:
        if role in {"implementer", "refactorer", "haskell_reviewer", "security_reviewer"}:
            assert context["criteria_without_tests"] == [partial_scope()["acceptance_criteria"][1]]
            assert context["specification"] == partial_scope()
    assert "workflow_skips" not in h._state("task")
    report = (Path(view["report_directory"]) / "pr-description.md").read_text()
    assert "Requirements without a named test in the initial draft" in report
    assert "A2: Update the protocol model" in report


def test_legacy_coverage_failure_continues_saved_tests_without_another_model_call(rig):
    h, agent, _ = rig
    view = legacy_draft(h, agent)
    assert view["outcome"]["kind"] == "waiting"
    assert "continue_tests" in view["prompt"]["actions"]
    calls = len(agent.calls)
    view = h.respond(
        "task", "continue_tests", approval_token=view["prompt"]["approval_token"], automatic=True
    )
    assert view["stage"] == "test_review", view["prompt"]
    assert len(agent.calls) == calls
    assert h._state("task")["specification"] == partial_scope()
    assert not h._state("task").get("workflow_skips")


def test_continue_message_uses_saved_draft_not_research_or_regeneration(rig):
    h, agent, _ = rig
    legacy_draft(h, agent)
    agent.responses["test_author"].append(
        {"answer": "Continuing your saved draft.", "next_action": "continue_tests"}
    )
    view = h.respond(
        "task", "question", text="Continue with these tests and then implement", automatic=True
    )
    assert view["stage"] == "test_review", view["prompt"]
    assert [role for role, _, _ in agent.calls].count("test_author") == 1
    assert "continue_tests" in agent.calls[-1][1]["recovery_actions"]


def test_legacy_draft_recovery_rejects_stale_worktree(rig):
    h, agent, execute = rig
    view = legacy_draft(h, agent)
    (Path(view["worktree"]) / TEST).write_text("-- external edits\n")
    with pytest.raises(ValueError, match="stale"):
        h.respond("task", "continue_tests", approval_token=view["prompt"]["approval_token"])
    assert not execute.calls
    assert (Path(view["worktree"]) / TEST).read_text() == "-- external edits\n"


def test_legacy_proposals_recovered_from_cache_are_still_usable(rig):
    import json

    h, agent, _ = rig
    legacy_draft(h, agent)
    state = h._state("task")
    (Path(state["directory"]) / "proposals" / "9999-test_author.json").write_text(
        json.dumps(state.pop("rejected_test_proposal"))
    )
    state["prompt"].pop("coverage_gap")
    h.graph.update_state(h._config("task"), state)
    view = h.inspect("task")
    assert "continue_tests" in view["prompt"]["actions"]
    view = h.respond("task", "continue_tests", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "test_review", view["prompt"]


def test_standalone_test_tasks_still_require_the_requested_coverage(rig):
    h, agent, execute = rig
    agent.responses["test_author"].append(draft())
    view = h.start_tests(
        "tests",
        "Test both behaviors",
        base="main",
        packages=["example-app"],
        criteria=["Increment input", "Check boundary"],
        expectation="red",
    )
    assert view["stage"] == "agent_error"
    assert "continue_tests" not in view["prompt"]["actions"]
    assert not execute.calls
    assert (Path(view["worktree"]) / TEST).read_text() == "-- Existing tests\n"
