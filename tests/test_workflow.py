"""Acceptance criteria for the developer's local TDD workflow."""

import copy
import json
from pathlib import Path

import pytest

from lambda_harness.contracts import Harness

from .conftest import (
    BROKEN,
    IMPLEMENTED,
    REFACTORED,
    SOURCE,
    SPECIFICATION,
    TEST,
    TEST_CONTENT,
    approve_tests,
    git,
    reach_test_review,
)


def test_interview_repeats_until_decisions_are_resolved(rig):
    harness, agent, _ = rig
    spec = copy.deepcopy(SPECIFICATION)
    spec["open_questions"] = ["Should negative inputs be supported?"]
    agent.responses["interviewer"].append(
        {"specification": spec, "questions": spec["open_questions"]}
    )

    view = harness.start("increment", "Fix addOne")
    assert view.get("stage") == "interview", view
    assert "negative" in json.dumps(view["prompt"])
    assert [role for role, _, _ in agent.calls] == ["interviewer"]

    view = harness.respond("increment", "answer", text="Yes, support negative integers.")
    assert view["stage"] == "specification_review"
    assert "negative integers" in json.dumps(agent.calls[-1][1])


def test_answering_a_specification_question_is_not_approval(rig):
    harness, agent, _ = rig
    view = harness.start("increment", "Fix addOne")
    assert view.get("stage") == "specification_review", view
    view = harness.respond("increment", "question", text="How does A1 establish the behavior?")
    assert view["stage"] == "specification_review"
    assert "test_author" not in [role for role, _, _ in agent.calls]


def test_test_review_contains_diff_coverage_and_actual_red_evidence(rig, repository):
    harness, agent, executor = rig
    view = reach_test_review(harness)
    assert view["stage"] == "test_review"
    packet = view["prompt"]
    assert "adds one" in packet["diff"]
    assert packet["coverage"]["A1"]
    assert packet["validation"]["status"] == "expected_red"
    assert ["hlint", "."] in [argv for argv, _, _ in executor.calls]
    assert ["nix", "flake", "check"] in [argv for argv, _, _ in executor.calls]
    assert ["cabal", "test", "example-app", "--test-show-details=direct"] in [
        argv for argv, _, _ in executor.calls
    ]
    assert "implementer" not in [role for role, _, _ in agent.calls]
    assert (repository / TEST).read_text() == "-- Existing tests\n"
    assert (Path(view["worktree"]) / SOURCE).read_text() == BROKEN


def test_developer_can_question_tests_without_starting_implementation(rig):
    harness, agent, _ = rig
    view = reach_test_review(harness)
    assert view.get("stage") == "test_review", view
    view = harness.respond("increment", "question", text="Why use zero as the input?")
    assert view["stage"] == "test_review"
    assert "answer" in view["prompt"]
    assert "implementer" not in [role for role, _, _ in agent.calls]


def test_test_revisions_require_fresh_validation_and_approval(rig):
    harness, agent, executor = rig
    view = reach_test_review(harness)
    assert view.get("stage") == "test_review", view
    old_token = view["prompt"]["approval_token"]
    agent.responses["test_author"].append(
        {
            "summary": "Clarify the assertion",
            "changes": [{"path": TEST, "content": TEST_CONTENT + "-- Also document negatives.\n"}],
            "expected_failures": ["Example.Arithmetic.adds one"],
            "coverage": {"A1": ["Example.Arithmetic.adds one"]},
            "commit_message": "test: specify increment behavior",
        }
    )
    previous_runs = len(executor.calls)
    view = harness.respond("increment", "revise", text="Clarify negative input behavior.")
    assert view["stage"] == "test_review"
    assert view["prompt"]["approval_token"] != old_token
    assert len(executor.calls) > previous_runs
    with pytest.raises(ValueError, match="stale|approval"):
        harness.respond("increment", "approve", approval_token=old_token)
    assert "implementer" not in [role for role, _, _ in agent.calls]


def test_external_edits_invalidate_test_approval(rig):
    harness, agent, _ = rig
    view = reach_test_review(harness)
    assert view.get("stage") == "test_review", view
    (Path(view["worktree"]) / TEST).write_text("-- A different test\n")
    with pytest.raises(ValueError, match="changed|stale|approval"):
        approve_tests(harness, view)
    assert "implementer" not in [role for role, _, _ in agent.calls]


def test_restart_preserves_pending_review_without_repeating_agent_work(rig, tmp_path, repository):
    harness, agent, executor = rig
    view = reach_test_review(harness)
    assert view.get("stage") == "test_review", view
    call_count = len(agent.calls)
    harness.close()

    resumed = Harness(repository, tmp_path / "state", agent, executor)
    try:
        restored = resumed.inspect("increment")
        assert restored["prompt"]["approval_token"] == view["prompt"]["approval_token"]
        assert len(agent.calls) == call_count
        assert approve_tests(resumed, restored)["stage"] == "ready"
        assert [role for role, _, _ in agent.calls].count("test_author") == 1
    finally:
        resumed.close()


def test_implementation_refactoring_and_reviews_use_separate_agents(rig):
    harness, agent, _ = rig
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "ready"
    roles = [role for role, _, _ in agent.calls]
    assert roles.index("test_author") < roles.index("implementer") < roles.index("refactorer")
    assert roles.index("refactorer") < roles.index("haskell_reviewer")
    assert roles.index("refactorer") < roles.index("security_reviewer")
    reviews = [context for role, context, _ in agent.calls if role.endswith("reviewer")]
    assert reviews[0]["revision"] == reviews[1]["revision"] == view["revision"]
    assert all("other_review" not in context for context in reviews)
    assert all(context["specification"] == SPECIFICATION for context in reviews)


def test_each_patch_is_validated_and_committed_in_order(rig, repository):
    harness, _, executor = rig
    base = git(repository, "rev-parse", "HEAD")
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "ready"
    worktree = Path(view["worktree"])
    subjects = git(worktree, "log", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()
    assert subjects == [
        "test: specify increment behavior",
        "fix: increment the input",
        "refactor: simplify increment",
    ]
    for source in (BROKEN, IMPLEMENTED, REFACTORED):
        commands = [argv for argv, _, contents in executor.calls if contents == source]
        assert ["hlint", "."] in commands
        assert ["cabal", "build", "all", "--enable-tests", "--ghc-options=-Werror"] in commands
        assert ["nix", "flake", "check"] in commands
        assert ["cabal", "test", "example-app", "--test-show-details=direct"] in commands
    assert git(repository, "rev-parse", "HEAD") == base
    assert git(repository, "status", "--porcelain") == ""
    assert git(worktree, "status", "--porcelain") == ""
    assert (worktree / TEST).read_text() == TEST_CONTENT


def test_lint_or_werror_failure_blocks_test_approval(rig):
    harness, agent, executor = rig
    executor.lint_error = True
    view = reach_test_review(harness)
    assert view["stage"] == "validation_blocked"
    assert "implementer" not in [role for role, _, _ in agent.calls]


def test_failed_nix_check_is_not_automatically_excused_as_tdd_red(rig):
    harness, agent, executor = rig
    executor.nix_error = True
    view = reach_test_review(harness)
    assert view["stage"] == "red_failure_review"
    assert "nix" in json.dumps(view["prompt"]).lower()
    assert "implementer" not in [role for role, _, _ in agent.calls]


def test_implementer_cannot_silently_replace_approved_tests(rig):
    harness, agent, _ = rig
    agent.responses["implementer"].append(
        {
            "summary": "Propose changing an approved test",
            "changes": [{"path": TEST, "content": "-- Removed the assertion\n"}],
            "commit_message": "fix: change tests",
            "complete": True,
        }
    )
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "test_change_review"
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
    assert "refactorer" not in [role for role, _, _ in agent.calls]


def test_security_findings_are_local_and_stop_the_ready_transition(rig):
    harness, agent, _ = rig
    agent.responses["security_reviewer"].append(
        {
            "summary": "A transition may stop progress",
            "findings": [
                {
                    "id": "SEC-1",
                    "severity": "high",
                    "location": f"{SOURCE}:3",
                    "title": "A participant can prevent recovery",
                    "conditions": "A participant sends a duplicate message",
                    "sequence": ["Accept message", "Restart", "Replay message"],
                    "impact": "Recovery remains blocked",
                    "evidence": "Illustrative fixture finding, not a real vulnerability",
                    "confidence": "suspected",
                    "recommendation": "Add a regression test and inspect the transition",
                    "needs_test": True,
                }
            ],
        }
    )
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "review_findings"
    assert "SEC-1" in json.dumps(view["prompt"])
    reports = Path(view["report_directory"])
    assert reports.is_dir()
    assert "SEC-1" in (reports / "security-review.json").read_text()


def test_optional_walkthrough_can_be_skipped(rig):
    harness, agent, _ = rig
    view = approve_tests(harness, reach_test_review(harness, walkthrough=True))
    assert view["stage"] == "walkthrough"
    assert "question" in view["prompt"]
    assert "answer" not in view["prompt"]
    view = harness.respond("increment", "skip")
    assert view["stage"] == "ready"
    assert "walkthrough" in [role for role, _, _ in agent.calls]


def test_walkthrough_gives_feedback_after_the_developer_answers(rig):
    harness, agent, _ = rig
    view = approve_tests(harness, reach_test_review(harness, walkthrough=True))
    assert view["stage"] == "walkthrough"
    first_question = view["prompt"]["question"]
    view = harness.respond("increment", "answer", text="It increments the input by one.")
    assert view["stage"] == "walkthrough"
    assert view["prompt"]["feedback"]
    assert view["prompt"]["question"] != first_question
    assert "walkthrough_feedback" in [role for role, _, _ in agent.calls]


def test_interview_only_mode_does_not_write_tests_or_code(rig, repository):
    harness, agent, executor = rig
    view = harness.start("increment", "Fix addOne", mode="interview")
    assert view.get("stage") == "specification_review", view
    view = harness.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "specification_ready"
    assert [role for role, _, _ in agent.calls] == ["interviewer"]
    assert executor.calls == []
    assert git(repository, "status", "--porcelain") == ""


def test_ready_handoff_stays_local(rig, repository, tmp_path):
    harness, agent, executor = rig
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repository, "remote", "add", "origin", str(remote))

    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "ready"
    assert git(remote, "for-each-ref", "--format=%(refname)") == ""
    assert all(argv[0] != "gh" and "push" not in argv for argv, _, _ in executor.calls)
    assert "walkthrough" not in [role for role, _, _ in agent.calls]
    reports = Path(view["report_directory"])
    assert (reports / "pr-description.md").is_file()
    assert (reports / "validation.json").is_file()


def test_review_bug_returns_to_test_author_and_developer_approval(rig):
    harness, agent, executor = rig
    finding = {
        "id": "HASKELL-1",
        "severity": "medium",
        "location": f"{SOURCE}:3",
        "title": "A boundary case needs coverage",
        "conditions": "A boundary input",
        "sequence": ["Call addOne with the boundary input"],
        "impact": "Unexpected behavior",
        "evidence": "Illustrative fixture finding",
        "confidence": "suspected",
        "recommendation": "Write a regression test before changing code",
        "needs_test": True,
    }
    agent.responses["haskell_reviewer"].append({"findings": [finding], "summary": "Found a gap"})
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "review_findings"
    implementation_calls = [role for role, _, _ in agent.calls].count("implementer")
    agent.responses["test_author"].append(
        {
            "summary": "Cover the reviewed boundary case",
            "changes": [{"path": TEST, "content": TEST_CONTENT + "-- Boundary case regression\n"}],
            "expected_failures": ["Example.Arithmetic.adds one"],
            "coverage": {"A1": ["Example.Arithmetic.adds one"]},
            "commit_message": "test: cover reviewed boundary case",
        }
    )
    executor.force_red = True
    view = harness.respond("increment", "fix", finding_ids=["HASKELL-1"])
    assert view["stage"] == "test_review"
    assert "boundary" in view["prompt"]["diff"].lower()
    assert [role for role, _, _ in agent.calls].count("implementer") == implementation_calls


def test_repeated_failed_repairs_return_control_to_the_developer(rig):
    harness, agent, _ = rig
    for _ in range(3):
        agent.responses["implementer"].append(
            {
                "summary": "An ineffective attempted repair",
                "changes": [{"path": SOURCE, "content": BROKEN}],
                "commit_message": "fix: attempt increment repair",
                "complete": True,
            }
        )
    view = approve_tests(harness, reach_test_review(harness))
    assert view["stage"] == "validation_blocked"
    roles = [role for role, _, _ in agent.calls]
    assert roles.count("implementer") <= 2
    assert "refactorer" not in roles


def test_tasks_have_independent_state_and_approval_tokens(rig):
    harness, _, _ = rig
    first = harness.start("first", "Fix addOne")
    second = harness.start("second", "Explain addOne", mode="interview")
    assert first.get("stage") == second.get("stage") == "specification_review"
    assert first["prompt"]["approval_token"] != second["prompt"]["approval_token"]
    assert harness.inspect("first")["task_id"] == "first"
    assert harness.inspect("second")["task_id"] == "second"


@pytest.mark.parametrize("path", ["../outside.hs", "/tmp/outside.hs", ".git/config"])
def test_agent_edits_cannot_escape_the_worktree_or_change_git_metadata(rig, path):
    harness, agent, _ = rig
    agent.responses["test_author"].append(
        {
            "summary": "Invalid file proposal",
            "changes": [{"path": path, "content": "invalid"}],
            "expected_failures": ["Example.Arithmetic.adds one"],
            "coverage": {"A1": ["Example.Arithmetic.adds one"]},
            "commit_message": "test: invalid proposal",
        }
    )
    view = reach_test_review(harness)
    assert view["stage"] == "agent_error"
    assert "path" in json.dumps(view["prompt"]).lower()


@pytest.mark.parametrize("declared,executed", [(False, True), (True, True), (True, False)])
def test_missing_production_api_can_be_reviewed_before_implementation(rig, declared, executed):
    from .conftest import FAILURE_ID, command_result, junit

    harness, agent, executor = rig
    proposal = {
        "summary": "Specify the new API before implementation",
        "changes": [{"path": TEST, "content": TEST_CONTENT}],
        "expected_failures": [FAILURE_ID],
        "coverage": {"A1": [FAILURE_ID]},
        "commit_message": "test: specify new API",
    }
    if declared:
        proposal["missing_implementation"] = ["Example.Arithmetic.addOne :: Int -> Int"]
    agent.responses["test_author"].append(proposal)
    original_execute = harness.execute

    def missing_api(argv, cwd, **kwargs):
        if argv[:2] == ["cabal", "build"] and (cwd / SOURCE).read_text() == BROKEN:
            return command_result(argv, exit_code=1, stderr="Not in scope: addOne")
        if argv[:2] == ["cabal", "test"] and not executed:
            return command_result(
                argv, junit_xml=[junit(failing=False).replace("adds one", "other test")]
            )
        return original_execute(argv, cwd, **kwargs)

    harness.execute = missing_api
    view = reach_test_review(harness)
    if not declared:
        assert view["stage"] == "validation_blocked"
        assert "approve" not in view["prompt"]["actions"]
        return
    assert view["stage"] == "test_review"
    assert view["outcome"]["title"] == "Tests written · implementation missing"
    validation = view["prompt"]["validation"]
    assert validation["status"] == "unverified_red"
    assert validation["test_evidence"]["executed"] == 0
    assert "did not compile or execute" in validation["evidence_error"]
    assert validation["commands"]["build"]["stderr_tail"] == "Not in scope: addOne"
    assert not any(argv[0:2] == ["cabal", "test"] for argv, _, _ in executor.calls)
    assert not any(role == "implementer" for role, _, _ in agent.calls)
    with pytest.raises(ValueError, match="Stale|missing"):
        harness.respond("increment", "approve", approval_token="stale")
    view = harness.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    if not executed:
        assert view["stage"] == "validation_blocked"
        assert "did not execute" in view["prompt"]["validation"]["evidence_error"]
        assert not any(role == "refactorer" for role, _, _ in agent.calls)
        return
    assert view["stage"] == "ready"
    implementation_context = next(c for r, c, _ in agent.calls if r == "implementer")
    assert implementation_context["missing_implementation"] == proposal["missing_implementation"]
    assert implementation_context["validation"]["status"] == "unverified_red"
    assert all(
        patch["validation"]["status"] == "green"
        for patch in harness._state("increment")["commits"]
        if patch["role"] != "test_author"
    )
    assert (Path(view["worktree"]) / TEST).read_text() == TEST_CONTENT
