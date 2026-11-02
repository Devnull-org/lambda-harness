"""Conversation instructions execute workflow actions through existing approval gates."""

from pathlib import Path

import pytest

from lambda_harness.baseline import apply_baseline, plan_baseline
from lambda_harness.workspace import Workspace

from .conftest import SOURCE, SPECIFICATION, git


def approved(harness):
    view = harness.start("task", "Fix addOne")
    return harness.respond("task", "approve", approval_token=view["prompt"]["approval_token"])


def target_commit(repository):
    (repository / "policy.txt").write_text("Existing policy API and tests\n")
    git(repository, "add", "policy.txt")
    git(repository, "commit", "-m", "Add existing policy")
    return git(repository, "rev-parse", "HEAD")


def baseline_request(repository, revision):
    return {
        "action": "retarget_baseline",
        "checkout": str(repository),
        "revision": revision,
        "reason": "Use the existing policy API from the intended checkout.",
    }


def test_chat_baseline_request_preserves_scope_and_resumes_tests_after_confirmation(
    rig, repository
):
    h, agent, executor = rig
    original = approved(h)
    target = target_commit(repository)
    agent.responses["interviewer"].append(
        {
            "answer": "I will update the task baseline after confirmation.",
            "workflow_action": baseline_request(repository, target),
        }
    )
    view = h.task_chat("task", f"Use {repository} at {target} and continue", automatic=True)
    assert view["stage"] == "baseline_review"
    assert view["prompt"]["baseline"]["to"] == target
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == original["base"]
    assert not executor.calls
    with pytest.raises(ValueError, match="Stale"):
        h.respond("task", "approve_baseline", approval_token="old")
    view = h.respond("task", "approve_baseline", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "test_review"
    assert view["base"] == target and view["mode"] == "full"
    assert h._state("task")["specification"] == SPECIFICATION
    assert agent.calls[-1][0] == "test_author"
    assert agent.calls[-1][1]["specification_status"] == "approved"
    assert agent.calls[-1][1]["base_revision"] == target
    assert "implementer" not in [role for role, _, _ in agent.calls]
    assert git(repository, "rev-parse", "HEAD") == target
    assert not git(repository, "status", "--porcelain")
    assert list(Path(view["worktree"]).parent.joinpath("baseline-changes").glob("*.json"))


def test_test_author_can_surface_baseline_correction_instead_of_dead_end(rig, repository):
    h, agent, _ = rig
    original = approved(h)
    target = target_commit(repository)
    agent.responses["test_author"].append(
        {
            "summary": "Wrong baseline",
            "changes": [],
            "expected_failures": [],
            "coverage": {},
            "commit_message": "No changes",
            "workflow_action": baseline_request(repository, target),
        }
    )
    view = h.run_phase("task", "tests")
    assert view["stage"] == "baseline_review"
    assert "retarget_baseline" in agent.calls[-1][1]["workflow_actions"]
    view = h.respond("task", "cancel_baseline")
    assert view["base"] == original["base"]
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == original["base"]


def test_scope_question_can_correct_target_without_reapproving_specification(rig, repository):
    h, agent, _ = rig
    approved(h)
    agent.responses["test_author"].append(
        {
            "summary": "Need intended checkout",
            "changes": [],
            "expected_failures": [],
            "coverage": {},
            "commit_message": "No patch",
        }
    )
    assert h.run_phase("task", "tests")["stage"] == "test_scope_review"
    target = target_commit(repository)
    agent.responses["test_author"].append(
        {
            "answer": "Here is the baseline correction.",
            "workflow_action": baseline_request(repository, target),
        }
    )
    view = h.respond("task", "question", text=f"Use {repository} and continue")
    assert view["stage"] == "baseline_review"
    view = h.respond("task", "approve_baseline", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "test_review"


@pytest.mark.parametrize("change", ["source", "task"])
def test_changed_checkout_invalidates_baseline_confirmation(rig, repository, change):
    h, agent, _ = rig
    approved(h)
    target = target_commit(repository)
    agent.responses["interviewer"].append(
        {"answer": "Confirm the baseline.", "workflow_action": baseline_request(repository, target)}
    )
    view = h.task_chat("task", "Use the intended checkout")
    path = repository if change == "source" else Path(view["worktree"])
    (path / SOURCE).write_text("-- unsaved work\n")
    if change == "task":
        with pytest.raises(ValueError, match="stale"):
            h.respond("task", "approve_baseline", approval_token=view["prompt"]["approval_token"])
    else:
        view = h.respond(
            "task", "approve_baseline", approval_token=view["prompt"]["approval_token"]
        )
        assert view["stage"] == "agent_error"
        assert "uncommitted" in view["prompt"]["error"]
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == view["base"]
    assert (path / SOURCE).read_text() == "-- unsaved work\n"


def test_baseline_fast_forward_replay_is_idempotent_only_with_authorization(rig, repository):
    h, _, _ = rig
    view = approved(h)
    target = target_commit(repository)
    workspace = Workspace(view["worktree"])
    plan = plan_baseline(repository, workspace, view["base"], str(repository), target)
    apply_baseline(repository, workspace, plan)
    with pytest.raises(ValueError, match="task has changes"):
        apply_baseline(repository, workspace, plan)
    apply_baseline(repository, workspace, plan, already_authorized=True)
    assert workspace.revision == target


def test_chat_starts_tests_but_an_ordinary_question_does_not(rig):
    h, agent, executor = rig
    original = approved(h)
    h.task_chat("task", "What will writing tests involve?")
    assert h.inspect("task")["stage"] == "specification_ready"
    assert not executor.calls
    agent.responses["interviewer"].append(
        {
            "answer": "Starting test writing.",
            "workflow_action": {"action": "write_tests", "reason": "Write the tests now."},
        }
    )
    view = h.task_chat("task", "Write the tests now")
    assert view["stage"] == "test_review" and view["worktree"] == original["worktree"]


def test_baseline_request_cannot_approve_a_pending_specification(rig, repository):
    h, agent, executor = rig
    h.start("task", "Fix addOne")
    target = target_commit(repository)
    agent.responses["interviewer"].append(
        {
            "answer": "Confirm the corrected baseline.",
            "workflow_action": baseline_request(repository, target),
        }
    )
    view = h.respond("task", "question", text="Use the intended checkout")
    view = h.respond("task", "approve_baseline", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "specification_review"
    assert not executor.calls


def test_bad_checkout_request_can_be_corrected_in_the_same_conversation(rig, repository, tmp_path):
    h, agent, executor = rig
    pending = h.start("task", "Fix addOne")
    target = target_commit(repository)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    agent.responses["interviewer"].append(
        {
            "answer": "Proposing the requested checkout.",
            "workflow_action": baseline_request(other, target),
        }
    )
    view = h.respond("task", "question", text="Use the other checkout")
    assert view["stage"] == "agent_error"
    assert "another repository" in view["prompt"]["error"]
    assert view["base"] == pending["base"]
    agent.responses["interviewer"].append(
        {
            "answer": "Using the corrected path.",
            "workflow_action": baseline_request(repository, target),
        }
    )
    view = h.respond("task", "question", text=f"I meant {repository}")
    assert view["stage"] == "baseline_review"
    view = h.respond("task", "approve_baseline", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "specification_review"
    assert not executor.calls
