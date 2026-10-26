"""Task discussion reads current work without changing workflow checkpoints."""

import copy

import pytest

from lambda_harness.conversation import conversation_messages
from lambda_harness.failures import AgentInterrupted
from lambda_harness.workflow import Harness
from lambda_harness.workspace import Workspace

from .conftest import SOURCE
from .test_phases import specification


def test_task_chat_preserves_pending_approval_and_uses_changes_and_history(rig):
    harness, agent, executor = rig
    view = harness.start("task", "Fix addOne")
    checkpoint = harness.graph.get_state(harness._config("task"))
    before = copy.deepcopy(checkpoint.values)
    workspace = Workspace(view["worktree"])
    source = workspace.path / SOURCE
    source.write_text(source.read_text() + "\n-- Explain this change\n")
    fingerprint = workspace.fingerprint()
    agent.responses["interviewer"].append({"answer": "docs: explain addOne"})
    result = harness.task_chat("task", "Suggest a commit message")
    context = agent.calls[-1][1]
    assert "Explain this change" in context["diff"]
    assert context["specification"] == before["specification"]
    assert context["task_stage"] == view["stage"]
    assert any(m.get("role") == "interviewer" for m in context["messages"])
    assert context["latest_user_message"] == "Suggest a commit message"
    assert conversation_messages(harness._state("task"))[-1]["answer"] == "docs: explain addOne"
    after = harness.graph.get_state(harness._config("task"))
    assert after.values == before
    assert after.next == checkpoint.next
    assert after.interrupts == checkpoint.interrupts
    assert result["prompt"] == view["prompt"]
    assert workspace.fingerprint() == fingerprint
    assert not executor.calls


def test_chat_survives_reopening_and_does_not_repeat_replies_or_block_next_step(
    rig, repository, tmp_path
):
    harness, agent, executor = rig
    specification(harness)
    for answer, question in [
        ("First suggestion", "Commit message?"),
        ("Second suggestion", "Shorter?"),
    ]:
        agent.responses["interviewer"].append({"answer": answer})
        harness.task_chat("task", question)
    assert any(m.get("answer") == "First suggestion" for m in agent.calls[-1][1]["messages"])
    reopened = Harness(repository, tmp_path / "state", agent, executor)
    try:
        assert conversation_messages(reopened._state("task"))[-1]["answer"] == "Second suggestion"
        view = reopened.run_phase("task", "tests")
        assert view["stage"] == "test_review"
        assert (
            sum(
                m.get("answer") == "First suggestion"
                for m in conversation_messages(reopened._state("task"))
            )
            == 1
        )
        assert (
            sum(
                m.get("answer") == "Second suggestion"
                for m in conversation_messages(reopened._state("task"))
            )
            == 1
        )
        approved = reopened.respond(
            "task", "approve", approval_token=view["prompt"]["approval_token"]
        )
        assert approved["stage"] == "tests_ready"
    finally:
        reopened.close()


def test_failed_task_chat_keeps_workflow_and_can_be_retried(rig):
    harness, agent, _ = rig
    view = specification(harness)
    original = copy.deepcopy(harness._state("task"))
    normal_agent = harness.agent
    harness.agent = lambda *args: (_ for _ in ()).throw(AgentInterrupted())
    with pytest.raises(AgentInterrupted):
        harness.task_chat("task", "Explain the task")
    assert harness._state("task") == original
    assert "did not complete" in conversation_messages(harness._state("task"))[-1]["answer"]
    harness.agent = normal_agent
    agent.responses["interviewer"].append({"answer": "Here is the explanation"})
    result = harness.task_chat("task", "Explain the task")
    assert result["stage"] == view["stage"]
    assert conversation_messages(harness._state("task"))[-1]["answer"] == "Here is the explanation"
    with pytest.raises(ValueError, match="Enter a question"):
        harness.task_chat("task", "  ")
