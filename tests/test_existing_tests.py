"""Developers can reuse passing coverage without skipping validation or approvals."""

from pathlib import Path

import pytest

from lambda_harness.failures import BackendResponseError

from .conftest import BROKEN, IMPLEMENTED, REFACTORED, SOURCE, TEST, TEST_CONTENT, git
from .test_phases import specification


def passing_base(repository):
    (repository / SOURCE).write_text(IMPLEMENTED)
    (repository / TEST).write_text(TEST_CONTENT)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Existing behavior and coverage")


@pytest.mark.parametrize(
    "phase,role,stage",
    [
        ("implement", "implementer", "implementation_ready"),
        ("refactor", "refactorer", "refactor_ready"),
    ],
)
def test_approved_scope_can_use_existing_tests(rig, repository, phase, role, stage):
    harness, agent, executor = rig
    passing_base(repository)
    view = specification(harness)
    assert set(view["prompt"]["available_phases"]) == {
        "explore",
        "interview",
        "tests",
        "implement",
        "refactor",
    }
    agent.responses[role].append(
        {
            "summary": "Simplify using existing coverage",
            "changes": [{"path": SOURCE, "content": REFACTORED}],
            "commit_message": "refactor: simplify increment",
            "complete": True,
        }
    )
    view = harness.run_phase("task", phase)
    assert view["stage"] == stage
    assert [r for r, _, _ in agent.calls] == ["interviewer", role]
    context = agent.calls[-1][1]
    assert context["test_strategy"] == "existing"
    assert context["validation"]["status"] == "green"
    assert TEST in context["approved_tests"]
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
    assert len(executor.calls) == 8  # Baseline and the changed code each get all four checks.
    assert len(harness._state("task")["commits"]) == 1


def test_failed_baseline_never_calls_the_implementer_or_accepts_red(rig):
    harness, agent, _ = rig
    specification(harness)
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "validation_blocked"
    assert set(view["prompt"]["actions"]) == {"recheck", "choose_phase", "question"}
    assert [r for r, _, _ in agent.calls] == ["interviewer"]
    assert not harness._state("task")["commits"]
    view = harness.respond("task", "choose_phase")
    assert view["stage"] == "specification_ready"
    assert not harness._state("task").get("approved_tests")


def test_existing_test_path_never_commits_red_intermediate_patches(rig, repository):
    harness, agent, _ = rig
    passing_base(repository)
    specification(harness)
    for _ in range(2):
        agent.responses["implementer"].append(
            {
                "summary": "Incomplete rewrite",
                "changes": [{"path": SOURCE, "content": BROKEN}],
                "commit_message": "refactor: work in progress",
                "complete": False,
            }
        )
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "validation_blocked"
    assert view["prompt"]["validation"]["phase"] == "green"
    assert "accept_red" not in view["prompt"]["actions"]
    assert "choose_phase" not in view["prompt"]["actions"]
    assert not harness._state("task")["commits"]


def test_existing_tests_still_need_review_before_changes(rig, repository):
    harness, agent, _ = rig
    passing_base(repository)
    specification(harness)
    agent.responses["implementer"].append(
        {
            "summary": "Remove test",
            "changes": [{"path": TEST, "content": "-- gone\n"}],
            "commit_message": "Remove coverage",
            "complete": True,
        }
    )
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "test_change_review"
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT


def test_no_test_patch_is_a_scope_decision_not_a_schema_failure(rig):
    harness, agent, executor = rig
    specification(harness)
    agent.responses["test_author"].append(
        {
            "summary": "This is an assessment; no file changes are requested.",
            "changes": [],
            "expected_failures": [],
            "coverage": {},
            "commit_message": "No commit",
        }
    )
    view = harness.run_phase("task", "tests")
    assert view["stage"] == "test_scope_review"
    assert "assessment" in view["prompt"]["summary"]
    assert set(view["prompt"]["actions"]) == {"question", "revise", "choose_phase"}
    assert not executor.calls
    assert [r for r, _, _ in agent.calls].count("test_author") == 1
    harness.respond("task", "question", text="What scope is missing?")
    assert harness.respond("task", "choose_phase")["stage"] == "specification_ready"
    assert "implementer" not in [r for r, _, _ in agent.calls]


def test_preexisting_failed_test_generation_can_return_to_phase_selection(rig):
    harness, agent, _ = rig
    specification(harness)

    def failed(*args):
        raise BackendResponseError("Empty test changes", "{}", "")

    harness.agent = failed
    assert harness.run_phase("task", "tests")["stage"] == "backend_blocked"
    harness.agent = agent
    view = harness.inspect("task")
    assert "choose_phase" in view["prompt"]["actions"]
    before = len(agent.calls)
    view = harness.respond("task", "choose_phase")
    assert view["stage"] == "specification_ready"
    assert "interview" in view["prompt"]["available_phases"]
    assert len(agent.calls) == before


def test_changing_phase_does_not_discard_worktree_edits(rig):
    harness, _, _ = rig
    specification(harness)

    def failed(*args):
        raise BackendResponseError("Empty test changes", "{}", "")

    harness.agent = failed
    view = harness.run_phase("task", "tests")
    path = Path(view["worktree"]) / TEST
    path.write_text("-- Unfinished developer work\n")
    with pytest.raises(ValueError, match="changed|clean"):
        harness.respond("task", "choose_phase")
    assert path.read_text() == "-- Unfinished developer work\n"


CABAL = "example-app/example-app.cabal"
CABAL_CONTENT = """name: example-app
library
  exposed-modules: Example.Arithmetic
  build-depends: base

common shared
  ghc-options: -Wall

library testlib
  build-depends: base

test-suite tests
  type: exitcode-stdio-1.0
  build-depends: base
"""


def dependency_base(repository):
    (repository / CABAL).write_text(CABAL_CONTENT)
    passing_base(repository)


def dependency_patch(content):
    return {
        "summary": "Implement using a production dependency",
        "changes": [
            {"path": SOURCE, "content": REFACTORED},
            {"path": CABAL, "content": content},
        ],
        "commit_message": "feat: use production dependency",
        "complete": True,
    }


def test_main_library_dependency_patch_is_applied_and_validated(rig, repository):
    harness, agent, executor = rig
    dependency_base(repository)
    specification(harness)
    content = CABAL_CONTENT.replace("build-depends: base", "build-depends: base, servant", 1)
    agent.responses["implementer"].append(dependency_patch(content))
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "implementation_ready"
    assert (Path(view["worktree"]) / CABAL).read_text() == content
    assert (Path(view["worktree"]) / SOURCE).read_text() == REFACTORED
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
    assert len(executor.calls) == 8
    assert len(harness._state("task")["commits"]) == 1


@pytest.mark.parametrize(
    "content",
    [
        CABAL_CONTENT.replace("build-depends: base", "build-depends: base, servant"),
        CABAL_CONTENT.replace("-Wall", "-w"),
        CABAL_CONTENT.replace("test-suite tests", "test-suite disabled"),
        CABAL_CONTENT.replace("library testlib\n  build-depends: base", "library testlib"),
        None,
    ],
)
def test_cabal_test_and_shared_configuration_still_blocks_whole_patch(rig, repository, content):
    harness, agent, executor = rig
    dependency_base(repository)
    specification(harness)
    agent.responses["implementer"].append(dependency_patch(content))
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "test_change_review"
    assert view["prompt"]["blocked_paths"] == [CABAL]
    assert (Path(view["worktree"]) / CABAL).read_text() == CABAL_CONTENT
    assert (Path(view["worktree"]) / SOURCE).read_text() == IMPLEMENTED
    assert len(executor.calls) == 4


def test_saved_blocked_patch_can_continue_coding_after_explanation(rig, repository):
    harness, agent, executor = rig
    dependency_base(repository)
    specification(harness)
    agent.responses["implementer"].append(
        dependency_patch(
            CABAL_CONTENT.replace("build-depends: base", "build-depends: base, servant")
        )
    )
    view = harness.run_phase("task", "implement")
    assert view["stage"] == "test_change_review"
    view = harness.respond("task", "question", text="When will implementation continue?")
    assert agent.calls[-1][1]["intent"] == "discuss_patch"
    assert len(executor.calls) == 4
    assert "approve_changes" in harness.inspect("task")["prompt"]["actions"]
    with pytest.raises(ValueError, match="token"):
        harness.respond("task", "continue_implementation", approval_token="stale")
    content = CABAL_CONTENT.replace("build-depends: base", "build-depends: base, servant", 1)
    agent.responses["implementer"].append(dependency_patch(content))
    view = harness.respond(
        "task", "continue_implementation", approval_token=view["prompt"]["approval_token"]
    )
    assert view["stage"] == "implementation_ready"
    context = agent.calls[-1][1]
    assert context.get("intent") != "explain"
    assert context["messages"][-1]["action"] == "continue_implementation"
    assert "rejected_test_change" in context["messages"][-1]
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
    assert (Path(view["worktree"]) / CABAL).read_text() == content
    assert len(executor.calls) == 8


def test_incomplete_test_proposal_exposes_evidence_without_applying_it(rig):
    import copy

    from .conftest import FAILURE_ID, SPECIFICATION

    harness, agent, executor = rig
    scope = copy.deepcopy(SPECIFICATION)
    scope["acceptance_criteria"].append({"id": "A2", "description": "Handle the boundary case"})
    agent.responses["interviewer"].append({"specification": scope, "questions": []})
    approved = specification(harness)
    worktree = Path(approved["worktree"])
    original = (worktree / TEST).read_text()
    agent.responses["test_author"].append(
        {
            "summary": "Partial proposal; boundary coverage still missing.",
            "changes": [{"path": TEST, "content": TEST_CONTENT + "-- proposed only\n"}],
            "expected_failures": [FAILURE_ID],
            "coverage": {"A1": [FAILURE_ID], "A2": []},
            "commit_message": "test: partial coverage",
        }
    )
    view = harness.run_phase("task", "tests", automatic=True)
    assert view["stage"] == "agent_error"
    assert view["outcome"]["title"] == "Test proposal incomplete"
    assert "1 of 2" in view["outcome"]["detail"]
    gap = view["outcome"]["coverage_gap"]
    assert gap["missing"] == [scope["acceptance_criteria"][1]]
    assert gap["files"] == [TEST]
    assert gap["summary"] == "Partial proposal; boundary coverage still missing."
    assert view["outcome"]["step"] == "tests"
    assert (worktree / TEST).read_text() == original
    assert executor.calls == []
    assert "question" in view["prompt"]["actions"]
    assert "choose_phase" in view["prompt"]["actions"]

    # Previously saved failures recover the same evidence from their saved proposal.
    legacy = copy.deepcopy(harness._state("task"))
    del legacy["prompt"]["coverage_gap"]
    assert harness._view(legacy)["outcome"]["coverage_gap"] == gap
    assert "coverage_gap" not in legacy["prompt"]
