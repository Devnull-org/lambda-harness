"""Exploration delivers analysis and can end without starting a coding workflow."""

import copy
import json
from pathlib import Path

import pytest

from lambda_harness.failures import BackendError
from lambda_harness.workflow import Harness

from .conftest import SOURCE, SPECIFICATION, git

ANALYSIS = {
    "summary": "A pure policy and an explicit effect/result boundary are viable alternatives.",
    "analysis": (
        "The current function returns its input unchanged.\n\n"
        "A pure policy can reject an input before the state transition. An external policy "
        "needs an effect and a matching result input; performing IO inside the pure "
        "transition is not an option. Approval must still match the current state."
    ),
    "references": [f"{SOURCE}:3 — the current pure function"],
    "open_questions": ["Would a pure policy have enough information?"],
}


def research(harness, agent):
    agent.responses["explorer"].append(ANALYSIS)
    return harness.start(
        "research", "Explore the alternatives without implementing.", phase="explore"
    )


def test_exploration_delivers_analysis_and_saves_it_without_interview_or_code(rig, repository):
    harness, agent, executor = rig
    view = research(harness, agent)
    assert view["stage"] == "exploration"
    assert view["prompt"]["exploration"]["analysis"] == ANALYSIS["analysis"]
    assert set(view["prompt"]["actions"]) == {"question", "start_implementation", "done"}
    assert "specification" not in view["prompt"]
    assert [call[0] for call in agent.calls] == ["explorer"]
    assert not executor.calls
    assert git(Path(view["worktree"]), "status", "--porcelain") == ""
    assert git(Path(view["worktree"]), "rev-parse", "HEAD") == git(repository, "rev-parse", "HEAD")
    report = Path(view["prompt"]["report"])
    assert ANALYSIS["analysis"] in report.read_text()
    assert SOURCE in report.read_text()
    assert json.loads(report.with_suffix(".json").read_text())["analysis"] == ANALYSIS["analysis"]
    with pytest.raises(ValueError, match="not available"):
        harness.respond("research", "approve", approval_token=view["prompt"]["approval_token"])
    completed = harness.respond("research", "done")
    assert completed["stage"] == "exploration_ready"
    assert completed["prompt"]["available_phases"] == ["explore", "interview", "implement"]
    assert harness.resume("research") == completed
    assert len(agent.calls) == 1
    with pytest.raises(ValueError, match="not available"):
        harness.run_phase("research", "tests")


def test_existing_approved_interview_can_run_exploration_with_its_scope_and_context(
    rig, repository
):
    harness, agent, executor = rig
    snapshot = harness.context.refresh(repository, sources=["CLAUDE.md"])
    view = harness.start("research", "Read this issue and explore our options.")
    view = harness.respond("research", "revise", text="Only explore validation before application.")
    approved = harness.respond(
        "research", "approve", approval_token=view["prompt"]["approval_token"]
    )
    # Reproduce a saved task from before exploration was added.
    state = copy.deepcopy(harness._state("research"))
    state.pop("initial_phase", None)
    state["prompt"]["available_phases"] = ["tests"]
    harness.graph.update_state(harness._config("research"), state, as_node="finish")
    assert harness.inspect("research")["prompt"]["available_phases"] == [
        "explore",
        "interview",
        "tests",
        "implement",
        "refactor",
    ]
    agent.responses["explorer"].append(ANALYSIS)
    view = harness.run_phase("research", "explore")
    assert view["stage"] == "exploration"
    assert view["worktree"] == approved["worktree"]
    assert view["project_context_id"] == snapshot["id"]
    role, context, cwd = agent.calls[-1]
    assert role == "explorer"
    assert context["specification"] == SPECIFICATION
    assert context["messages"] == []
    assert context["project_context"]
    assert cwd == Path(approved["worktree"])
    assert not executor.calls


def test_exploration_questions_and_results_survive_new_process_and_done(rig, repository, tmp_path):
    harness, agent, executor = rig
    view = research(harness, agent)
    reopened = Harness(repository, tmp_path / "state", agent, executor)
    try:
        assert reopened.resume("research") == view
        reply = {**ANALYSIS, "analysis": "The pure policy uses candidate data and configuration."}
        agent.responses["explorer"].append(reply)
        follow_up = reopened.respond("research", "question", text="Focus only on a pure policy.")
        context = agent.calls[-1][1]
        assert context["exploration"]["analysis"] == ANALYSIS["analysis"]
        assert context["messages"][-1]["text"] == "Focus only on a pure policy."
        assert follow_up["prompt"]["exploration"]["analysis"] == reply["analysis"]
        assert reply["analysis"] in Path(follow_up["prompt"]["report"]).read_text()
        assert len(agent.calls) == 2
        completed = reopened.respond("research", "done")
        assert completed["stage"] == "exploration_ready"
        assert len(agent.calls) == 2
        assert not executor.calls
    finally:
        reopened.close()


def test_coding_after_research_requires_new_interview_and_test_approval(rig):
    harness, agent, executor = rig
    research(harness, agent)
    harness.respond("research", "done")
    view = harness.run_phase("research", "interview")
    assert view["stage"] == "specification_review"
    assert agent.calls[-1][1]["intent"] == "implementation_interview"
    assert "exploration" not in agent.calls[-1][1]
    assert agent.calls[-1][1]["messages"] == []
    assert not executor.calls
    with pytest.raises(ValueError, match="pending"):
        harness.run_phase("research", "tests")
    harness.respond("research", "approve", approval_token=view["prompt"]["approval_token"])
    assert harness.run_phase("research", "tests")["stage"] == "test_review"
    assert "implementer" not in [call[0] for call in agent.calls]


def test_restart_repeats_exploration_without_changing_previous_discussion(rig):
    harness, agent, _ = rig
    original = research(harness, agent)
    agent.responses["explorer"].append(ANALYSIS)
    fresh = harness.restart("research")
    assert fresh["stage"] == "exploration"
    assert fresh["worktree"] != original["worktree"]
    assert harness.inspect("research") == original
    assert [call[0] for call in agent.calls] == ["explorer", "explorer"]


def test_exploration_quota_failure_preserves_question_and_previous_report(rig):
    harness, agent, executor = rig
    view = research(harness, agent)
    report = Path(view["prompt"]["report"])
    before = report.read_text()
    calls = []

    def unavailable(role, context, cwd):
        calls.append(role)
        raise BackendError("Usage limit reached", kind="quota")

    harness.agent = unavailable
    blocked = harness.respond("research", "question", text="What about state changes?")
    assert blocked["stage"] == "backend_blocked"
    assert blocked["prompt"]["backend_failure"]["kind"] == "quota"
    assert calls == ["explorer"]
    assert report.read_text() == before
    harness.agent = agent
    agent.responses["explorer"].append(ANALYSIS)
    resumed = harness.resume("research", retry_backend=True)
    assert resumed["stage"] == "exploration"
    assert agent.calls[-1][1]["messages"][-1]["text"] == "What about state changes?"
    assert agent.calls[-1][1]["exploration"]["analysis"] == ANALYSIS["analysis"]
    assert not executor.calls


def test_explorer_cannot_return_only_a_specification(rig):
    harness, agent, executor = rig
    agent.responses["explorer"].extend(
        [{"specification": SPECIFICATION, "questions": []}, ANALYSIS]
    )
    view = harness.start("research", "Explore the options", phase="explore")
    assert view["stage"] == "exploration"
    assert view["prompt"]["exploration"]["analysis"] == ANALYSIS["analysis"]
    repair = agent.calls[-1][1]["response_repair"]
    assert "analysis" in repair["validation_error"]
    assert len(agent.calls) == 2
    assert not executor.calls


def test_explorer_direct_edits_are_rejected_before_saving_analysis(rig, repository):
    harness, _, executor = rig
    original = (repository / SOURCE).read_text()

    def edits(role, context, cwd):
        (cwd / SOURCE).write_text("-- An unauthorized change\n")
        return ANALYSIS

    harness.agent = edits
    view = harness.start("research", "Explore the options", phase="explore")
    assert view["stage"] == "agent_error"
    assert "changed the checkout" in view["prompt"]["error"]
    assert not (Path(view["report_directory"]) / "exploration.md").exists()
    assert (repository / SOURCE).read_text() == original
    assert not executor.calls


def test_continue_coding_in_conversation_runs_test_first_flow_after_approvals(rig):
    harness, agent, executor = rig
    original = research(harness, agent)
    agent.responses["explorer"].append({**ANALYSIS, "next_action": "start_implementation"})
    view = harness.respond(
        "research", "question", text="Implement the pure policy now", automatic=True
    )
    assert view["stage"] == "specification_review"
    assert view["outcome"]["title"] == "Approve scope to start coding"
    assert view["worktree"] == original["worktree"]
    context = agent.calls[-1][1]
    assert context["intent"] == "implementation_interview"
    handoff = context["implementation_handoff"]
    assert handoff["instruction"] == "Implement the pure policy now"
    assert handoff["exploration"]["analysis"] == ANALYSIS["analysis"]
    assert any(m.get("text") == handoff["instruction"] for m in handoff["messages"])
    assert not executor.calls
    tests = harness.respond("research", "approve", approval_token=view["prompt"]["approval_token"])
    assert tests["stage"] == "test_review"
    assert "implementer" not in [role for role, _, _ in agent.calls]
    assert agent.calls[-1][1]["implementation_handoff"] == handoff
    complete = harness.respond(
        "research", "approve", approval_token=tests["prompt"]["approval_token"]
    )
    assert complete["stage"] == "ready"
    assert "implementer" in [role for role, _, _ in agent.calls]
    assert complete["worktree"] == original["worktree"]


def test_legacy_exploration_offers_start_implementation_with_stale_token_guard(rig):
    harness, agent, executor = rig
    view = research(harness, agent)
    state = copy.deepcopy(harness._state("research"))
    state["prompt"]["actions"] = ["question", "done"]
    harness.graph.update_state(harness._config("research"), state)
    assert "start_implementation" in harness.inspect("research")["prompt"]["actions"]
    with pytest.raises(ValueError, match="Stale"):
        harness.respond("research", "start_implementation", approval_token="old")
    view = harness.respond(
        "research", "start_implementation", approval_token=view["prompt"]["approval_token"]
    )
    assert view["stage"] == "specification_review"
    assert agent.calls[-1][1]["implementation_handoff"]["exploration"]["analysis"]
    assert not executor.calls


@pytest.mark.parametrize("via_chat", [False, True])
def test_completed_exploration_can_start_implementation_without_restart(rig, via_chat):
    harness, agent, _ = rig
    original = research(harness, agent)
    harness.respond("research", "done")
    if via_chat:
        agent.responses["explorer"].append({**ANALYSIS, "next_action": "start_implementation"})
        view = harness.task_chat("research", "Continue coding this yourself")
    else:
        view = harness.run_phase("research", "implement")
    assert view["stage"] == "specification_review"
    assert view["worktree"] == original["worktree"]
    assert agent.calls[-1][1]["implementation_handoff"]


def test_initial_research_cannot_start_coding_from_model_suggestion(rig):
    harness, agent, executor = rig
    agent.responses["explorer"].append({**ANALYSIS, "next_action": "start_implementation"})
    view = harness.start("research", "Explore only", phase="explore")
    assert view["stage"] == "exploration"
    assert agent.calls[-1][1]["can_start_implementation"] is False
    assert not executor.calls


def test_completed_exploration_question_stays_research(rig):
    harness, agent, executor = rig
    research(harness, agent)
    harness.respond("research", "done")
    agent.responses["explorer"].append({**ANALYSIS, "next_action": None})
    view = harness.task_chat("research", "What would implementing the alternative involve?")
    assert view["stage"] == "exploration"
    assert agent.calls[-1][0] == "explorer"
    assert "implementation_handoff" not in harness._state("research")
    assert not executor.calls


def test_reexploring_does_not_reuse_an_old_coding_request(rig):
    harness, agent, _ = rig
    research(harness, agent)
    agent.responses["explorer"].append({**ANALYSIS, "next_action": "start_implementation"})
    view = harness.respond("research", "question", text="Please implement the pure policy")
    harness.respond("research", "approve", approval_token=view["prompt"]["approval_token"])
    agent.responses["explorer"].append({**ANALYSIS, "next_action": "start_implementation"})
    view = harness.run_phase("research", "explore")
    assert view["stage"] == "exploration"
    assert agent.calls[-1][1]["can_start_implementation"] is False
