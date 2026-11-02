"""Error recovery is conversational, bounded to available operations, and resumable."""

from pathlib import Path

import pytest

from lambda_harness.failures import BackendError
from lambda_harness.handoff import export_handoff
from lambda_harness.workspace import Workspace, git

from .conftest import REFACTORED, SOURCE
from .test_phases import approved_tests


@pytest.mark.parametrize("via_chat", [False, True])
def test_timed_out_test_writing_continues_with_more_time_without_repeating_setup(rig, via_chat):
    h, agent, _ = rig
    view = h.start("timeout", "Fix addOne")
    h.respond("timeout", "approve", approval_token=view["prompt"]["approval_token"])

    def timeout(role, context, cwd):
        if role == "test_author":
            raise BackendError("Timed out after 600s", kind="timeout")
        return agent(role, context, cwd)

    h.agent = timeout
    view = h.run_phase("timeout", "tests")
    assert view["stage"] == "backend_blocked"
    assert view["outcome"]["title"] == "Test writing timed out"
    assert "retry_longer" in view["prompt"]["actions"]
    assert view["prompt"]["retry_timeout_seconds"] == 1800
    discoveries = len(agent.discovery_calls)
    h.agent = agent
    if via_chat:
        agent.responses["test_author"].append(
            {"answer": "Continuing with more time.", "next_action": "retry_longer"}
        )
        view = h.respond("timeout", "question", text="Please finish the implementation")
    else:
        view = h.respond("timeout", "retry_longer")
    assert view["stage"] == "test_review"
    assert len(agent.discovery_calls) == discoveries
    assert agent.calls[-1][1]["agent_timeout"] == 1800
    assert h._state("timeout")["agent_timeout"] == 1800
    assert "implementer" not in [role for role, _, _ in agent.calls]


SIGNING_ERROR = (
    "git commit failed: error: gpg failed to sign the data:\n"
    "[GNUPG:] KEY_CONSIDERED 64AA0918461E3C542FC5F5E4C574785FF89B8E25 2\n"
    "gpg: cannot open '/dev/tty': Device not configured"
)


def failed_commit(rig, monkeypatch):
    harness, agent, executor = rig
    approved_tests(harness)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError(SIGNING_ERROR)

    monkeypatch.setattr(Workspace, "commit", fail)
    view = harness.run_phase("task", "implement", automatic=True)
    assert view["stage"] == "agent_error"
    return view, calls


def test_commit_failure_offers_scoped_signing_command_and_alternatives(rig, monkeypatch):
    view, _ = failed_commit(rig, monkeypatch)
    commands = view["prompt"]["suggested_commands"]
    assert 'export GPG_TTY="$(tty)"' in commands[0]["command"]
    assert "--local-user 64AA0918461E3C542FC5F5E4C574785FF89B8E25" in commands[0]["command"]
    assert "git commit" not in commands[0]["command"]
    assert {"retry", "recheck", "question", "continue_without_commits"} <= set(
        view["prompt"]["actions"]
    )


def test_continue_without_commits_validates_reviews_and_exports_dirty_work(rig, monkeypatch):
    harness, agent, _ = rig
    view, commits = failed_commit(rig, monkeypatch)
    head = git(Path(view["worktree"]), "rev-parse", "HEAD")
    view = harness.respond(
        "task", "continue_without_commits", approval_token=view["prompt"]["approval_token"]
    )
    assert view["stage"] == "ready"
    assert view["commit_policy"] == "manual"
    assert "uncommitted" in view["prompt"]["summary"]
    assert len(commits) == 1
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == head
    assert (Path(view["worktree"]) / SOURCE).read_text() == REFACTORED
    state = harness._state("task")
    assert state["validation"]["status"] == "green"
    assert state["haskell_reviewer"]["fingerprint"] == state["review_fingerprint"]
    assert state["security_reviewer"]["fingerprint"] == state["review_fingerprint"]
    assert (
        "Changes remain uncommitted"
        in (Path(view["report_directory"]) / "pr-description.md").read_text()
    )
    assert export_handoff(harness, ["task"])
    (Path(view["worktree"]) / SOURCE).write_text("-- Changed after review\n")
    with pytest.raises(ValueError, match="changed"):
        export_handoff(harness, ["task"])


def test_conversation_can_select_no_commits_but_advice_does_not(rig, monkeypatch):
    harness, agent, executor = rig
    view, commits = failed_commit(rig, monkeypatch)
    before = len(executor.calls)
    agent.responses["implementer"].append(
        {"answer": "You can leave changes uncommitted.", "next_action": None}
    )
    view = harness.respond("task", "question", text="What are my options?")
    assert view["stage"] == "agent_error"
    assert harness._state("task").get("commit_policy") is None
    assert len(commits) == 1 and len(executor.calls) == before
    agent.responses["implementer"].append(
        {"answer": "I will continue without committing.", "next_action": "continue_without_commits"}
    )
    view = harness.respond("task", "question", text="Don't commit anything else; continue.")
    assert view["stage"] == "ready"
    assert len(commits) == 1


def test_skip_commit_cannot_bypass_changed_validation(rig, monkeypatch):
    harness, agent, _ = rig
    view, _ = failed_commit(rig, monkeypatch)
    (Path(view["worktree"]) / SOURCE).write_text("-- Changed after validation\n")
    with pytest.raises(ValueError, match="stale"):
        harness.respond(
            "task", "continue_without_commits", approval_token=view["prompt"]["approval_token"]
        )
    agent.responses["implementer"].append(
        {"answer": "Continue without commits.", "next_action": "continue_without_commits"}
    )
    view = harness.respond("task", "question", text="Don't commit; continue")
    assert view["stage"] == "agent_error"
    assert "rerun checks" in view["prompt"]["error"]
    assert harness._state("task").get("commit_policy") is None


@pytest.mark.parametrize(
    "failure", [RuntimeError("tool not found"), BackendError("provider error", kind="quota")]
)
def test_general_errors_support_advice_commands_and_retry_with_instructions(rig, failure):
    harness, agent, executor = rig
    approved_tests(harness)
    original = harness.agent

    def fail(*args):
        raise failure

    harness.agent = fail
    view = harness.run_phase("task", "implement")
    assert "question" in view["prompt"]["actions"]
    assert "continue_without_commits" not in view["prompt"]["actions"]
    harness.agent = original
    commands = [{"explanation": "Check the installed CLI.", "command": "tool --version"}]
    agent.responses["implementer"].append(
        {
            "answer": "Check the tool, then retry.",
            "suggested_commands": commands,
            "next_action": None,
        }
    )
    calls = len(executor.calls)
    view = harness.respond("task", "question", text="How can I fix this?")
    assert view["prompt"]["suggested_commands"] == commands
    assert len(executor.calls) == calls
    assert agent.calls[-1][1]["intent"] == "recover_operation"
    agent.responses["implementer"].append(
        {"answer": "Retrying with your instructions.", "next_action": "retry"}
    )
    view = harness.respond("task", "question", text="Retry and keep the change minimal.")
    assert view["stage"] == "implementation_ready"
    assert any(
        m.get("text") == "Retry and keep the change minimal."
        for m in agent.calls[-1][1]["messages"]
    )


def test_unsupported_recovery_cannot_disable_commits(rig):
    harness, agent, _ = rig
    approved_tests(harness)
    original = harness.agent
    harness.agent = lambda *a: (_ for _ in ()).throw(RuntimeError("tool missing"))
    harness.run_phase("task", "implement")
    harness.agent = original
    agent.responses["implementer"].append(
        {"answer": "Skipping commits.", "next_action": "continue_without_commits"}
    )
    view = harness.respond("task", "question", text="What happened?")
    assert view["stage"] == "agent_error"
    assert "unavailable" in view["prompt"]["error"]
    assert harness._state("task").get("commit_policy") is None


def test_manual_phase_completion_reports_success_without_a_commit_activity(rig, monkeypatch):
    harness, _, _ = rig
    view, commits = failed_commit(rig, monkeypatch)
    progress = []
    harness.progress = progress.append
    view = harness.respond(
        "task",
        "continue_without_commits",
        approval_token=view["prompt"]["approval_token"],
        automatic=False,
    )
    assert view["stage"] == "implementation_ready"
    assert view["outcome"]["kind"] == "success"
    assert "No commit was created" in view["outcome"]["detail"]
    assert "one-step mode" in view["outcome"]["guidance"]
    assert "task: commit" not in progress
    assert any("keeping validated changes uncommitted" in item for item in progress)
    assert "No commit was created" in harness._state("task")["messages"][-1]["answer"]
    view = harness.run_phase("task", "refactor")
    assert view["stage"] == "refactor_ready"
    assert len(commits) == 1
    assert "Refactoring complete and validated" in progress[-1]
    assert "one-step mode" in progress[-1]
