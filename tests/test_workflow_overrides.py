"""Explicit chat instructions can replace the default phase ordering."""

from pathlib import Path

import pytest

from .conftest import IMPLEMENTED, SOURCE, SPECIFICATION, TEST, git
from .test_task_instructions import approved


def override(text, *, target="implement", skip=None, automatic=None, specification=None):
    return {
        "answer": "Following your requested workflow.",
        "workflow_action": {
            "action": "override_workflow",
            "target": target,
            "skip": skip or ["test_writing"],
            "user_instruction": text,
            "reason": "Developer requested this workflow.",
            "automatic": automatic,
            "specification": specification,
        },
    }


def test_skip_test_writing_from_requested_phase_runs_implementation_and_checks(rig):
    h, agent, execute = rig
    approved(h)
    state = h._state("task")
    h._phase_complete(state, "tests_requested", "Write tests next")
    h.graph.update_state(h._config("task"), state, as_node="finish")
    text = "Skip test writing and implement now"
    agent.responses["interviewer"].append(override(text))
    view = h.task_chat("task", text)
    assert view["stage"] == "implementation_ready", view["prompt"]
    assert (Path(view["worktree"]) / SOURCE).read_text() == IMPLEMENTED
    assert "test_author" not in [role for role, _, _ in agent.calls]
    assert execute.calls
    assert h._state("task")["validation"]["status"] == "green"
    assert h._state("task")["workflow_skips"] == ["test_writing"]
    assert any(
        "Skipping: test writing" in m.get("answer", "") for m in h._state("task")["messages"]
    )


def test_implementation_instruction_can_leave_task_check_planning(rig):
    from .test_check_runner import plan

    h, agent, execute = rig
    approved(h)
    state = h._state("task")
    state["approved_tests"] = h._workspace(state).test_manifest()
    h._phase_complete(state, "tests_requested", "Write tests next")
    h.graph.update_state(h._config("task"), state, as_node="finish")
    agent.responses["test_runner"].append(plan())
    view = h.run_task_checks("task")
    assert view["stage"] == "checks_plan"
    original_tests = h._state("task").get("approved_tests")
    text = "Leave check planning, skip test writing and implement now; run checks afterwards"
    agent.responses["test_runner"].append(override(text))
    view = h.task_chat("task", text)
    state = h._state("task")
    assert view["stage"] == "implementation_ready", view["prompt"]
    assert not state.get("checks_origin")
    assert state["check_plan"]["commands"] == plan()["commands"]
    assert "override_workflow" in agent.calls[-2][1]["workflow_actions"]
    assert state["workflow_skips"] == ["test_writing"]
    assert state["approved_tests"] == original_tests
    assert state["validation"]["status"] == "green" and execute.calls
    assert (Path(view["worktree"]) / SOURCE).read_text() == IMPLEMENTED


def test_rejected_handoff_is_saved_in_chat_and_keeps_check_session(rig):
    from lambda_harness.conversation import conversation_messages

    from .test_check_runner import plan

    h, agent, execute = rig
    approved(h)
    agent.responses["test_runner"].append(plan())
    view = h.run_task_checks("task")
    path = Path(view["worktree"]) / SOURCE
    path.write_text("-- edited outside the task\n")
    text = "Skip test writing and implement now"
    agent.responses["test_runner"].append(override(text))
    with pytest.raises(ValueError, match="Worktree changed"):
        h.task_chat("task", text)
    state = h._state("task")
    assert state["checks_origin"] and state["stage"] == "checks_plan"
    assert "workflow_skips" not in state
    assert not execute.calls
    assert "This request failed: Worktree changed" in conversation_messages(state)[-1]["answer"]


@pytest.mark.parametrize("channel", ["question", "task_chat"])
def test_bypass_pending_scope_gate_keeps_tests_and_records_unverified_work(rig, channel):
    h, agent, execute = rig
    original = h.start("task", "Fix addOne")
    text = "Skip requirements, new tests, validation and commits; implement now"
    agent.responses["interviewer"].append(
        override(text, skip=["requirements", "test_writing", "validation", "commits"])
    )
    view = (
        h.respond("task", "question", text=text)
        if channel == "question"
        else h.task_chat("task", text)
    )
    assert view["stage"] == "implementation_ready", view["prompt"]
    assert (Path(view["worktree"]) / TEST).read_text() == "-- Existing tests\n"
    assert not execute.calls and not agent.discovery_calls
    state = h._state("task")
    assert state["validation"]["status"] == "skipped"
    assert not state["commits"]
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == original["base"]
    assert "validated" not in view["outcome"]["detail"]


def test_skip_reviews_and_refactoring_automatically_finishes_without_claiming_reviews(rig):
    h, agent, execute = rig
    approved(h)
    text = (
        "Skip tests, validation, refactoring, reviews, walkthrough and commits; "
        "implement and finish automatically"
    )
    skip = [
        "test_writing",
        "validation",
        "refactor",
        "code_review",
        "security_review",
        "walkthrough",
        "commits",
    ]
    agent.responses["interviewer"].append(override(text, skip=skip, automatic=True))
    view = h.task_chat("task", text)
    assert view["stage"] == "ready", view["prompt"]
    assert not execute.calls
    roles = [role for role, _, _ in agent.calls]
    assert not set(roles) & {
        "test_author",
        "refactorer",
        "haskell_reviewer",
        "security_reviewer",
        "walkthrough",
    }
    report = (Path(view["report_directory"]) / "pr-description.md").read_text()
    assert "no review performed" in report
    assert "Skipped by developer instruction" in report


def test_initial_request_can_skip_formal_scope_approval(rig):
    h, agent, _ = rig
    text = "Fix addOne now; skip requirements and test writing"
    agent.responses["interviewer"].append(
        {
            "specification": SPECIFICATION,
            "questions": [],
            "workflow_action": override(text, skip=["requirements", "test_writing"])[
                "workflow_action"
            ],
        }
    )
    view = h.start("task", text)
    assert view["stage"] == "implementation_ready", view["prompt"]


def test_mismatched_instruction_cannot_waive_a_gate(rig):
    h, agent, execute = rig
    original = approved(h)
    agent.responses["interviewer"].append(override("Skip everything"))
    view = h.task_chat("task", "What would happen if I skipped tests?")
    assert view["stage"] == "agent_error"
    assert "current developer instruction" in view["prompt"]["error"]
    assert not execute.calls
    assert "workflow_skips" not in h._state("task")
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == original["base"]


def test_override_is_task_scoped_and_ordinary_question_keeps_default(rig):
    h, agent, _ = rig
    approved(h)
    h.start("other", "Fix addOne")
    text = "Skip tests and validation; implement now"
    agent.responses["interviewer"].append(override(text, skip=["test_writing", "validation"]))
    h.task_chat("task", text)
    view = h.respond("other", "question", text="Could I skip tests?")
    assert view["stage"] == "specification_review"
    assert "workflow_skips" not in h._state("other")


def test_skip_test_approval_preserves_red_then_green_flow(rig):
    h, agent, execute = rig
    approved(h)
    text = "Write tests; skip test approval and continue everything automatically"
    agent.responses["interviewer"].append(
        override(text, target="tests", skip=["test_approval"], automatic=True)
    )
    view = h.task_chat("task", text)
    assert view["stage"] == "ready", view["prompt"]
    assert execute.calls
    assert [v["status"] for v in h._state("task")["validations"]][:2] == ["expected_red", "green"]


def test_recovery_question_can_bypass_incomplete_test_coverage(rig):
    h, agent, _ = rig
    from .test_feature_drafts import legacy_draft

    legacy_draft(h, agent)
    text = "Skip test writing and implement now"
    agent.responses["test_author"].append(override(text))
    view = h.respond("task", "question", text=text)
    assert view["stage"] == "implementation_ready", view["prompt"]
    context = next(context for role, context, _ in agent.calls if role == "implementer")
    assert context["workflow_instructions"][-1]["user_instruction"] == text


def test_skipping_test_writing_does_not_authorize_test_edits(rig):
    h, agent, _ = rig
    approved(h)
    text = "Skip test writing and implement now"
    agent.responses["interviewer"].append(override(text))
    agent.responses["implementer"].append(
        {
            "summary": "Update tests",
            "changes": [{"path": TEST, "content": "-- edited\n"}],
            "commit_message": "test: update",
        }
    )
    view = h.task_chat("task", text)
    assert view["stage"] == "test_change_review"
    assert (Path(view["worktree"]) / TEST).read_text() == "-- Existing tests\n"
    text = "Skip patch approval and implement the required test changes"
    reply = override(text, skip=["patch_approval"])
    reply["proposal"] = None
    agent.responses["implementer"].append(reply)
    agent.responses["implementer"].append(
        {
            "summary": "Implement with tests",
            "changes": [
                {"path": TEST, "content": "-- edited\n"},
                {"path": SOURCE, "content": IMPLEMENTED},
            ],
            "commit_message": "fix: implement with tests",
        }
    )
    view = h.respond("task", "question", text=text)
    assert view["stage"] == "implementation_ready", view["prompt"]
    assert (Path(view["worktree"]) / TEST).read_text() == "-- edited\n"


def test_override_survives_restart_and_exports_with_its_instruction(rig):
    from lambda_harness.handoff import export_handoff
    from lambda_harness.workflow import Harness

    h, agent, execute = rig
    approved(h)
    text = "Skip tests and validation; implement now"
    agent.responses["interviewer"].append(override(text, skip=["test_writing", "validation"]))
    h.task_chat("task", text)
    reopened = Harness(h.repository, h.state_dir.parent, agent, execute)
    try:
        assert reopened._state("task")["workflow_skips"] == ["test_writing", "validation"]
        archive = export_handoff(reopened, ["task"])
        assert archive
    finally:
        reopened.close()


def test_phased_buttons_do_not_require_steps_the_user_skipped(rig):
    h, agent, _ = rig
    approved(h)
    text = "Skip tests, refactoring, reviews and walkthrough; implement now"
    agent.responses["interviewer"].append(
        override(
            text, skip=["test_writing", "refactor", "code_review", "security_review", "walkthrough"]
        )
    )
    view = h.task_chat("task", text)
    assert view["stage"] == "implementation_ready", view["prompt"]
    assert view["prompt"]["available_phases"] == ["handoff"]
    assert h.run_phase("task", "handoff")["stage"] == "ready"


def test_external_edits_cannot_be_blessed_by_an_override(rig):
    h, agent, _ = rig
    view = h.start("task", "Fix addOne")
    (Path(view["worktree"]) / SOURCE).write_text("-- unrelated external edit\n")
    text = "Skip requirements and tests and implement now"
    agent.responses["interviewer"].append(override(text, skip=["requirements", "test_writing"]))
    with pytest.raises(ValueError, match="changed outside"):
        h.task_chat("task", text)
    assert "workflow_skips" not in h._state("task")
    assert (Path(view["worktree"]) / SOURCE).read_text() == "-- unrelated external edit\n"


def test_coverage_mapping_can_be_waived_without_skipping_execution(rig):
    from .conftest import FAILURE_ID, TEST_CONTENT

    h, agent, execute = rig
    approved(h)
    text = "Skip full acceptance coverage mapping and write the first failing test"
    agent.responses["interviewer"].append(override(text, target="tests", skip=["coverage"]))
    agent.responses["test_author"].append(
        {
            "summary": "First failing test",
            "changes": [{"path": TEST, "content": TEST_CONTENT}],
            "coverage": {},
            "expected_failures": [FAILURE_ID],
            "commit_message": "test: first case",
        }
    )
    view = h.task_chat("task", text)
    assert view["stage"] == "test_review", view["prompt"]
    assert execute.calls
    assert h._state("task")["validation"]["status"] == "expected_red"


def test_user_can_end_after_red_tests_without_implementation(rig):
    from .conftest import BROKEN

    h, agent, _ = rig
    approved(h)
    text = (
        "Write failing tests, skip approval, implementation, refactoring and reviews, "
        "then hand off automatically"
    )
    agent.responses["interviewer"].append(
        override(
            text,
            target="tests",
            skip=[
                "test_approval",
                "implementation",
                "refactor",
                "code_review",
                "security_review",
                "walkthrough",
            ],
            automatic=True,
        )
    )
    view = h.task_chat("task", text)
    assert view["stage"] == "ready", view["prompt"]
    assert (Path(view["worktree"]) / SOURCE).read_text() == BROKEN
    assert "implementer" not in [role for role, _, _ in agent.calls]
    assert h._state("task")["validation"]["status"] == "expected_red"
