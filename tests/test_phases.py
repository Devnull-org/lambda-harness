"""A phase only runs when chosen, with approvals and evidence carried across restarts."""

from pathlib import Path

import pytest

from lambda_harness.contracts import Harness
from lambda_harness.workspace import Workspace

from .conftest import SOURCE, TEST, TEST_CONTENT


def approve(harness, view):
    return harness.respond("task", "approve", approval_token=view["prompt"]["approval_token"])


def specification(harness, **options):
    return approve(harness, harness.start("task", "Fix addOne", **options))


def approved_tests(harness):
    specification(harness)
    return approve(harness, harness.run_phase("task", "tests"))


def refactored(harness):
    approved_tests(harness)
    harness.run_phase("task", "implement")
    return harness.run_phase("task", "refactor")


def roles(agent):
    return [role for role, _, _ in agent.calls]


def test_default_interview_stops_and_can_continue_after_restart(rig, repository, tmp_path):
    harness, agent, executor = rig
    completed = specification(harness)
    assert completed["stage"] == "specification_ready"
    assert completed["prompt"]["available_phases"] == [
        "explore",
        "interview",
        "tests",
        "implement",
        "refactor",
    ]
    assert Path(completed["worktree"]).is_dir()
    assert (Path(completed["report_directory"]) / "specification.json").is_file()
    assert roles(agent) == ["interviewer"]
    assert executor.calls == []
    harness.close()

    resumed = Harness(repository, tmp_path / "state", agent, executor)
    try:
        assert resumed.resume("task") == completed
        assert roles(agent) == ["interviewer"]
        view = resumed.run_phase("task", "tests")
        assert view["stage"] == "test_review"
        assert roles(agent) == ["interviewer", "test_author"]
    finally:
        resumed.close()


@pytest.mark.parametrize("first", ["review-haskell", "review-security"])
def test_each_phase_stops_before_the_next_agent(rig, first):
    harness, agent, _ = rig
    view = approved_tests(harness)
    assert view["stage"] == "tests_ready"
    assert roles(agent) == ["interviewer", "test_author"]
    assert harness.resume("task") == view

    view = harness.run_phase("task", "implement")
    assert view["prompt"]["available_phases"] == ["refactor"]
    assert roles(agent)[-1] == "implementer"
    view = harness.run_phase("task", "refactor")
    assert view["prompt"]["available_phases"] == ["review-haskell", "review-security"]
    assert roles(agent)[-1] == "refactorer"

    second = "review-security" if first == "review-haskell" else "review-haskell"
    view = harness.run_phase("task", first)
    assert view["prompt"]["available_phases"] == [second]
    assert len([r for r in roles(agent) if r.endswith("reviewer")]) == 1
    view = harness.run_phase("task", second)
    assert view["prompt"]["available_phases"] == ["walkthrough", "handoff"]
    assert not (Path(view["report_directory"]) / "pr-description.md").exists()
    count = len(agent.calls)
    view = harness.run_phase("task", "handoff")
    assert view["stage"] == "ready"
    assert len(agent.calls) == count
    assert (Path(view["report_directory"]) / "pr-description.md").is_file()


def test_phase_selection_cannot_skip_test_approval(rig):
    harness, agent, _ = rig
    pending = harness.start("task", "Fix addOne")
    with pytest.raises(ValueError, match="pending"):
        harness.run_phase("task", "implement")
    approve(harness, pending)
    view = harness.run_phase("task", "tests")
    assert view["stage"] == "test_review"
    with pytest.raises(ValueError, match="resume|pending"):
        harness.run_phase("task", "implement")
    view = harness.respond("task", "question", text="Why this assertion?")
    assert view["stage"] == "test_review"
    assert "implementer" not in roles(agent)


@pytest.mark.parametrize("file", [SOURCE, TEST])
def test_changed_files_between_phases_block_execution(rig, file):
    harness, agent, _ = rig
    view = approved_tests(harness)
    (Path(view["worktree"]) / file).write_text("-- Changed since approval\n")
    with pytest.raises(ValueError, match="changed"):
        harness.run_phase("task", "implement")
    assert "implementer" not in roles(agent)


def test_changed_source_checkout_does_not_invalidate_saved_specification(rig, repository):
    harness, agent, _ = rig
    specification(harness)
    (repository / SOURCE).write_text("-- Changed since interview\n")
    assert harness.run_phase("task", "tests")["stage"] == "test_review"
    assert roles(agent) == ["interviewer", "test_author"]


def test_legacy_interview_task_can_continue_with_tests(rig):
    harness, agent, _ = rig
    completed = specification(harness, mode="interview")
    assert completed["stage"] == "specification_ready"
    assert harness.run_phase("task", "tests")["stage"] == "test_review"
    assert roles(agent) == ["interviewer", "test_author"]


def test_recovery_after_test_commit_still_stops_before_implementation(rig, monkeypatch):
    harness, agent, _ = rig
    specification(harness)
    view = harness.run_phase("task", "tests")
    original = Workspace.commit
    interrupted = False

    def commit_then_interrupt(self, *args, **kwargs):
        nonlocal interrupted
        revision = original(self, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return revision

    monkeypatch.setattr(Workspace, "commit", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        approve(harness, view)
    assert harness.resume("task")["stage"] == "tests_ready"
    assert "implementer" not in roles(agent)


def test_walkthrough_is_explicit_and_stops_before_handoff(rig):
    harness, agent, _ = rig
    refactored(harness)
    harness.run_phase("task", "review-haskell")
    harness.run_phase("task", "review-security")
    assert "walkthrough" not in roles(agent)
    view = harness.run_phase("task", "walkthrough")
    assert view["stage"] == "walkthrough"
    view = harness.respond("task", "skip")
    assert view["prompt"]["available_phases"] == ["handoff"]
    assert not (Path(view["report_directory"]) / "pr-description.md").exists()
    assert harness.run_phase("task", "handoff")["stage"] == "ready"


def test_test_change_request_waits_for_explicit_test_phase(rig):
    harness, agent, _ = rig
    approved_tests(harness)
    agent.responses["implementer"].append(
        {
            "summary": "Request another test",
            "changes": [{"path": TEST, "content": TEST_CONTENT + "-- Another assertion\n"}],
            "commit_message": "test: extend coverage",
            "complete": True,
        }
    )
    assert harness.run_phase("task", "implement")["stage"] == "test_change_review"
    view = harness.respond("task", "revise", text="Discuss the extra assertion")
    assert view["prompt"]["available_phases"] == ["tests"]
    assert roles(agent).count("test_author") == 1
    assert harness.run_phase("task", "tests")["stage"] == "test_review"


def test_review_fix_waits_for_explicit_phase_and_invalidates_reviews(rig):
    harness, agent, _ = rig
    refactored(harness)
    harness.run_phase("task", "review-haskell")
    agent.responses["security_reviewer"].append(
        {
            "summary": "Comment needs clarification",
            "findings": [
                {
                    "id": "SEC-1",
                    "severity": "low",
                    "location": SOURCE,
                    "title": "Unclear comment",
                    "conditions": "Reading the implementation",
                    "sequence": ["Read the comment"],
                    "impact": "Misunderstanding",
                    "evidence": "Fixture only",
                    "confidence": "suspected",
                    "recommendation": "Clarify the comment",
                    "needs_test": False,
                }
            ],
        }
    )
    assert harness.run_phase("task", "review-security")["stage"] == "review_findings"
    count = len(agent.calls)
    view = harness.respond("task", "fix", finding_ids=["SEC-1"])
    assert view["prompt"]["available_phases"] == ["refactor"]
    assert len(agent.calls) == count
    view = harness.run_phase("task", "refactor")
    assert view["prompt"]["available_phases"] == ["review-haskell", "review-security"]
    with pytest.raises(ValueError, match="not available"):
        harness.run_phase("task", "handoff")


def test_failed_validation_cannot_be_skipped_by_selecting_another_phase(rig):
    harness, agent, executor = rig
    specification(harness)
    executor.lint_error = True
    assert harness.run_phase("task", "tests")["stage"] == "validation_blocked"
    with pytest.raises(ValueError, match="pending"):
        harness.run_phase("task", "implement")
    assert "implementer" not in roles(agent)


def test_requested_walkthrough_remains_explicit_and_is_required_before_handoff(rig):
    harness, agent, _ = rig
    specification(harness, walkthrough=True)
    approve(harness, harness.run_phase("task", "tests"))
    harness.run_phase("task", "implement")
    harness.run_phase("task", "refactor")
    harness.run_phase("task", "review-haskell")
    view = harness.run_phase("task", "review-security")
    assert view["prompt"]["available_phases"] == ["walkthrough"]
    assert "walkthrough" not in roles(agent)
    with pytest.raises(ValueError, match="not available"):
        harness.run_phase("task", "handoff")


@pytest.mark.parametrize("automatic", [False, True])
def test_test_author_receives_approval_despite_stale_draft_prose(rig, automatic):
    import copy

    from .conftest import SPECIFICATION

    harness, agent, _ = rig
    draft = copy.deepcopy(SPECIFICATION)
    draft["expected_behavior"] += " This specification is not approved."
    draft["constraints"].append("This revised specification requires fresh developer approval.")
    agent.responses["interviewer"].append({"specification": draft, "questions": []})
    pending = harness.start("task", "Fix addOne")
    with pytest.raises(ValueError, match="pending"):
        harness.run_phase("task", "tests", automatic=automatic)
    assert "specification_status" not in agent.calls[-1][1]
    approve(harness, pending)
    view = harness.run_phase("task", "tests", automatic=automatic)
    role, context, _ = agent.calls[-1]
    assert role == "test_author"
    assert context["specification"] == draft
    assert context["specification_status"] == "approved"
    assert view["stage"] == "test_review"
    assert "implementer" not in roles(agent)
