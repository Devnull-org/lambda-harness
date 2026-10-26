"""Handoffs move paused work between independent checkouts without running a model."""

import base64
import copy
import json
from pathlib import Path

import pytest

from lambda_harness.handoff import export_handoff, import_handoff, preview_handoff
from lambda_harness.workflow import Harness
from lambda_harness.workspace import Workspace

from .conftest import SOURCE, TEST, ScriptedAgent, ScriptedExecutor, git, reach_test_review
from .test_exploration import ANALYSIS
from .test_review_context import large_change


@pytest.fixture
def recipient(repository, tmp_path):
    checkout = tmp_path / "teammate"
    git(repository, "clone", "--no-local", str(repository), str(checkout))
    git(checkout, "config", "user.name", "Second developer")
    git(checkout, "config", "user.email", "second@example.invalid")
    git(checkout, "config", "commit.gpgsign", "false")
    git(checkout, "config", "core.hooksPath", "/dev/null")
    agent, executor = ScriptedAgent(), ScriptedExecutor()
    harness = Harness(checkout, tmp_path / "recipient-state", agent, executor)
    yield harness, agent, executor
    harness.close()


def transfer(source, target, tasks):
    archive = export_handoff(source, tasks)
    preview = preview_handoff(target, archive)
    result = import_handoff(target, archive, confirmation=preview["confirmation"])
    return archive, result


def accept(harness, task):
    view = harness.inspect(task)
    assert view["stage"] == "handoff_review"
    return harness.respond(
        task, "continue_handoff", approval_token=view["prompt"]["approval_token"]
    )


def test_recovered_interview_turns_travel_with_a_handoff(rig, recipient):
    from lambda_harness.conversation import conversation_messages

    from .test_conversation import legacy_interview

    source, agent, _ = rig
    target, target_agent, _ = recipient
    original = legacy_interview(source, agent)
    expected = conversation_messages(original)
    transfer(source, target, ["legacy"])
    accept(target, "legacy")
    assert target._state("legacy")["messages"] == expected
    assert source._state("legacy") == original
    assert not target_agent.calls


def test_existing_test_baseline_can_be_shared_and_rechecked(rig, recipient):
    from .test_phases import specification

    source, _, _ = rig
    target, agent, _ = recipient
    specification(source)
    assert source.run_phase("task", "implement")["stage"] == "validation_blocked"
    transfer(source, target, ["task"])
    view = accept(target, "task")
    assert view["test_strategy"] == "existing"
    assert target.respond("task", "recheck")["stage"] == "validation_blocked"
    assert not agent.calls
    assert len(agent.discovery_calls) == 1
    assert target.respond("task", "choose_phase")["stage"] == "specification_ready"


def test_unresolved_test_setup_can_be_shared_without_executing_checks(rig, recipient):
    from .test_phases import specification
    from .test_validation_discovery import discovered

    source, agent, executor = rig
    target, target_agent, target_executor = recipient
    specification(source)
    agent.responses["validation_discovery"].append(
        discovered(questions=["Which service does the integration suite need?"])
    )
    assert source.run_phase("task", "tests")["stage"] == "validation_setup"
    transfer(source, target, ["task"])
    view = accept(target, "task")
    assert view["stage"] == "validation_setup"
    assert "integration suite" in view["prompt"]["questions"][0]
    assert not target._state("task").get("validation_knowledge")
    assert not executor.calls and not target_executor.calls
    assert not target_agent.discovery_calls
    assert (
        target.respond("task", "answer", text="The fixture starts its service.")["stage"]
        == "test_review"
    )
    assert len(target_agent.discovery_calls) == 1


def test_test_scope_decision_can_be_shared_without_starting_code(rig, recipient):
    from .test_phases import specification

    source, agent, _ = rig
    target, target_agent, _ = recipient
    specification(source)
    agent.responses["test_author"].append(
        {
            "summary": "Assessment only",
            "changes": [],
            "expected_failures": [],
            "coverage": {},
            "commit_message": "No changes",
        }
    )
    assert source.run_phase("task", "tests")["stage"] == "test_scope_review"
    transfer(source, target, ["task"])
    assert accept(target, "task")["stage"] == "test_scope_review"
    assert target.respond("task", "choose_phase")["stage"] == "specification_ready"
    assert not target_agent.calls


def test_review_tool_receipts_survive_handoff(rig, repository, recipient, monkeypatch):
    from .test_github_actions import github, review

    source, view = review(rig, repository)
    comments, _ = github(monkeypatch, source)
    harness, target = rig[0], recipient[0]
    view = harness.review_action(
        "docs",
        "post_review_comment",
        token=view["review_actions"]["token"],
        role="documentation_reviewer",
        finding_id="DOC-002",
    )
    transfer(harness, target, ["docs"])
    restored = accept(target, "docs")
    assert restored["review_actions"]["posts"] == view["review_actions"]["posts"]
    assert len(comments) == 1


@pytest.mark.parametrize("reviewers", ["documentation", "all"])
def test_documentation_reviews_can_be_handed_off_and_resumed(rig, recipient, reviewers):
    source, _, _ = rig
    target, agent, executor = recipient
    git(source.repository, "checkout", "-b", "docs")
    (source.repository / "README.md").write_text("# Setup\nRead the guide.\n")
    git(source.repository, "add", ".")
    git(source.repository, "commit", "-m", "Write docs")
    original = source.start_review(
        "docs-review", "Check examples", base="main", head="docs", reviewers=reviewers
    )
    transfer(source, target, ["docs-review"])
    view = accept(target, "docs-review")
    assert view["stage"] == "standalone_review"
    assert view["agent_profiles"] == original["agent_profiles"]
    assert not agent.calls and not executor.calls
    view = target.respond("docs-review", "question", text="What assumptions do the examples make?")
    assert view["stage"] == "standalone_review"
    assert agent.calls[-1][0] == "documentation_reviewer"
    assert not executor.calls


def test_large_review_artifacts_follow_the_imported_task(rig, recipient):
    source, _, _ = rig
    target, agent, _ = recipient
    # The recipient needs this test's added comparison commit.
    large_change(source.repository)
    git(target.repository, "fetch", str(source.repository), "review-base")
    source.start_review(
        "large-review",
        "Review docs",
        base="review-base",
        head="large-docs",
        reviewers="documentation",
    )
    transfer(source, target, ["large-review"])
    accept(target, "large-review")
    view = target.respond("large-review", "question", text="What about the diagram?")
    assert view["stage"] == "standalone_review"
    changes = agent.calls[-1][1]["review_changes"]
    assert str(source.state_dir) not in json.dumps(changes)
    assert Path(changes["index"]).is_relative_to(target.state_dir)
    assert "END_OF_DIAGRAM" in Path(changes["full_diff"]).read_text()


def test_spaced_task_names_survive_handoff_and_name_conflicts(rig, recipient):
    source, _, _ = rig
    target, agent, _ = recipient
    name = "Review  documentation"
    original = source.start(name, "Review the documentation")
    _, result = transfer(source, target, [name])
    assert result["tasks"] == [{"original": name, "task_id": name}]
    assert accept(target, name)["task_id"] == name
    _, repeated = transfer(source, target, [name])
    imported = name + "-import-1"
    assert repeated["tasks"] == [{"original": name, "task_id": imported}]
    restored = accept(target, imported)
    assert restored["stage"] == original["stage"]
    branch = git(Path(restored["worktree"]), "branch", "--show-current")
    assert " " not in branch
    plan = target.abandon_plan(imported)
    target.abandon(imported, confirmation_token=plan["confirmation_token"])
    assert target.inspect(name)["task_id"] == name
    assert not agent.calls


def test_roundtrip_specification_memory_and_pinned_agents(rig, recipient):
    source, _, _ = rig
    target, agent, executor = recipient
    memory = source.context.save(
        source.repository,
        notes="The increment law matters.",
        sources=[SOURCE],
        expected_current=None,
    )
    source.start("increment", "Fix addOne")
    source.respond("increment", "question", text="Which boundary should the test cover?")
    before = copy.deepcopy(source._state("increment"))
    archive, result = transfer(source, target, ["increment"])
    assert result["tasks"] == [{"original": "increment", "task_id": "increment"}]
    assert not agent.calls and not executor.calls
    restored = target._state("increment")
    assert restored["messages"] == before["messages"]
    assert restored["agent_profiles"] == before["agent_profiles"]
    assert target.context.get() == memory
    assert restored["project_context_id"] == memory["id"]
    assert str(source.state_dir) not in json.dumps(archive)
    assert restored["worktree"].startswith(str(target.state_dir))
    assert source._state("increment") == before
    view = accept(target, "increment")
    assert view["stage"] == "specification_review"
    view = target.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "specification_ready"
    assert not agent.calls and not executor.calls


def test_red_test_patch_needs_local_checks_and_new_approval(rig, recipient):
    source, _, _ = rig
    target, agent, executor = recipient
    old_view = reach_test_review(source)
    original = Workspace(old_view["worktree"])
    transfer(source, target, ["increment"])
    view = accept(target, "increment")
    assert view["stage"] == "test_review"
    assert "approve" not in view["prompt"]["actions"]
    assert Workspace(view["worktree"]).fingerprint() == original.fingerprint()
    assert (Path(view["worktree"]) / TEST).read_text() == (original.path / TEST).read_text()
    with pytest.raises(ValueError):
        target.respond("increment", "approve", approval_token=old_view["prompt"]["approval_token"])
    view = target.respond("increment", "recheck")
    assert executor.calls and not agent.calls
    assert view["stage"] == "test_review"
    view = target.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    assert view["stage"] == "tests_ready"
    view = target.run_phase("increment", "implement")
    assert view["stage"] == "implementation_ready"
    assert agent.calls[0][0] == "implementer"


def test_git_commits_move_without_push_and_existing_tasks_memory_are_preserved(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    view = source.start("increment", "Fix addOne")
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    view = source.run_phase("increment", "tests")
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    source.run_phase("increment", "implement")
    target.start("increment", "A different task")
    prior = copy.deepcopy(target._state("increment"))
    memory = target.context.save(
        target.repository, notes="Keep my notes", sources=[], expected_current=None
    )
    _, result = transfer(source, target, ["increment"])
    assert result["tasks"][0]["task_id"] == "increment-import-1"
    state = target._state("increment-import-1")
    assert git(Path(state["worktree"]), "rev-parse", "HEAD") == git(
        Path(source._state("increment")["worktree"]), "rev-parse", "HEAD"
    )
    assert len(state["commits"]) == 2
    assert target._state("increment") == prior
    assert target.context.get() == memory
    assert accept(target, "increment-import-1")["stage"] == "implementation_ready"


def test_import_rejects_bad_payloads_without_creating_work(rig, recipient):
    source, _, _ = rig
    target, agent, executor = recipient
    source.start("increment", "Fix addOne")
    archive = export_handoff(source, ["increment"])
    for mutate in (
        lambda a: a.update(version=999),
        lambda a: a["tasks"][0]["state"].update(task_id="../../escape"),
        lambda a: a["tasks"][0]["state"].update(next="implement"),
        lambda a: a["tasks"][0]["state"].update(test_paths=["/etc/passwd"]),
        lambda a: a["tasks"][0]["state"].update(phase=["invalid"]),
        lambda a: a["tasks"][0]["state"].update(specification={}),
        lambda a: a["tasks"][0]["state"].update(gate_fingerprint="f" * 64),
        lambda a: a["tasks"][0].update(
            untracked=[{"path": "../escape", "data": "", "executable": False}]
        ),
    ):
        bad = copy.deepcopy(archive)
        mutate(bad)
        with pytest.raises(ValueError):
            preview_handoff(target, bad)
    assert not target.graph.get_state(target._config("increment")).values
    assert not agent.calls and not executor.calls


def test_changed_preview_cannot_be_imported(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    source.start("increment", "Fix addOne")
    archive = export_handoff(source, ["increment"])
    preview = preview_handoff(target, archive)
    archive["tasks"][0]["state"]["request"] = "Something else"
    with pytest.raises(ValueError, match="preview"):
        import_handoff(target, archive, confirmation=preview["confirmation"])


def test_export_refuses_checkout_drift(rig):
    source, _, _ = rig
    view = source.start("increment", "Fix addOne")
    (Path(view["worktree"]) / SOURCE).write_text("-- An external edit\n")
    with pytest.raises(ValueError, match="changed"):
        export_handoff(source, ["increment"])


@pytest.mark.parametrize("phase", ["chat", "explore", "review"])
def test_standalone_conversations_can_continue_after_transfer(rig, recipient, phase):
    source, agent, _ = rig
    target, next_agent, _ = recipient
    if phase == "review":
        git(source.repository, "checkout", "-b", "review-this")
        (source.repository / SOURCE).write_text("-- A change to review\n")
        git(source.repository, "commit", "-am", "A change")
        source.start_review("discuss", "Review this change", base="main", head="review-this")
        action = "question"
    else:
        if phase == "chat":
            answer = {"answer": "An explanation", "references": []}
            role, action = "chat", "answer"
        else:
            answer = ANALYSIS
            role, action = "explorer", "question"
        agent.responses[role].append(answer)
        next_agent.responses[role].append(answer)
        source.start("discuss", "Explain the code", phase=phase)
    transfer(source, target, ["discuss"])
    accept(target, "discuss")
    result = target.respond("discuss", action, text="Explain that in more detail")
    assert result["stage"] not in {"agent_error", "backend_blocked"}, result
    assert next_agent.calls


def test_multiple_tasks_and_memory_only_import(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    saved = source.context.save(
        source.repository, notes="Shared context", sources=[], expected_current=None
    )
    transfer(source, target, [])
    assert target.context.get() == saved
    source.start("first", "First task")
    source.start("second", "Second task")
    _, result = transfer(source, target, ["first", "second"])
    assert len(result["tasks"]) == 2
    assert accept(target, "second")["stage"] == "specification_review"


def test_import_rolls_back_all_tasks_when_a_later_patch_is_corrupt(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    memory = source.context.save(
        source.repository, notes="Should not be installed", sources=[], expected_current=None
    )
    source.start("first", "First task")
    source.start("second", "Second task")
    archive = export_handoff(source, ["first", "second"])
    archive["tasks"][1]["patch"] = "Not a Git patch"
    preview = preview_handoff(target, archive)
    before = git(target.repository, "worktree", "list", "--porcelain")
    with pytest.raises(ValueError, match="patch"):
        import_handoff(target, archive, confirmation=preview["confirmation"])
    assert git(target.repository, "worktree", "list", "--porcelain") == before
    for name in ("first", "second"):
        assert not target.graph.get_state(target._config(name)).values
        assert not (target.state_dir / name).exists()
    assert target.context.latest_id() is None
    with pytest.raises(ValueError):
        target.context.get(memory["id"])


def test_edited_validation_commands_cannot_be_imported(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    source.start("example", "Explain this")
    archive = export_handoff(source, ["example"])
    archive["tasks"][0]["state"]["validation_config"] = {"build": ["sh", "-c", "false"]}
    with pytest.raises(ValueError, match="unsupported workflow fields"):
        preview_handoff(target, archive)


def test_missing_git_base_is_reported_before_mutation(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    source.start("example", "Explain this")
    archive = export_handoff(source, ["example"])
    archive["tasks"][0]["state"]["base"] = "f" * 40
    with pytest.raises(ValueError, match="Fetch"):
        preview_handoff(target, archive)


def test_unfinished_binary_executable_and_intent_to_add_files_roundtrip(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    view = source.start("example", "Explain this")
    worktree = Path(view["worktree"])
    binary = worktree / "fixture.bin"
    binary.write_bytes(b"\xff\x00\x01")
    binary.chmod(0o755)
    added = worktree / "new.txt"
    added.write_text("New but not committed\n")
    git(worktree, "add", "--intent-to-add", "new.txt")
    state = copy.deepcopy(source._state("example"))
    state["gate_fingerprint"] = Workspace(worktree).fingerprint()
    source.graph.update_state(source._config("example"), state, as_node="pause")
    # Re-establish the paused gate after simulating edits accepted by a phase.
    source.graph.invoke(state, source._config("example"))
    transfer(source, target, ["example"])
    imported = Path(target.inspect("example")["worktree"])
    assert (imported / "fixture.bin").read_bytes() == binary.read_bytes()
    assert (imported / "fixture.bin").stat().st_mode & 0o111
    assert Workspace(imported).fingerprint() == Workspace(worktree).fingerprint()


def test_reexport_imported_task_keeps_its_original_pause(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    source.start("example", "Explain this")
    transfer(source, target, ["example"])
    archive = export_handoff(target, ["example"])
    assert archive["tasks"][0]["state"]["stage"] == "specification_review"


def test_successive_developers_can_commit_different_phases(rig, recipient, tmp_path):
    source, _, _ = rig
    second, _, _ = recipient
    view = source.start("increment", "Fix addOne")
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    view = source.run_phase("increment", "tests")
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    transfer(source, second, ["increment"])
    accept(second, "increment")
    assert second.run_phase("increment", "implement")["stage"] == "implementation_ready"
    checkout = tmp_path / "third-developer"
    git(source.repository, "clone", str(source.repository), str(checkout))
    git(checkout, "config", "user.name", "Third developer")
    git(checkout, "config", "user.email", "third@example.invalid")
    git(checkout, "config", "commit.gpgsign", "false")
    git(checkout, "config", "core.hooksPath", "/dev/null")
    third = Harness(checkout, tmp_path / "third-state", ScriptedAgent(), ScriptedExecutor())
    try:
        transfer(second, third, ["increment"])
        accept(third, "increment")
        view = third.run_phase("increment", "refactor")
        assert view["stage"] == "refactor_ready", view
        assert len(third._state("increment")["commits"]) == 3
    finally:
        third.close()


def test_completed_exploration_and_full_feature_retain_their_phase_boundaries(rig, recipient):
    source, agent, _ = rig
    target, _, _ = recipient
    agent.responses["explorer"].append(ANALYSIS)
    source.start("exploration", "Explore this code", phase="explore")
    source.respond("exploration", "done")
    view = reach_test_review(source)
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    assert source.inspect("increment")["stage"] == "ready"
    transfer(source, target, ["exploration", "increment"])
    view = accept(target, "exploration")
    assert view["stage"] == "exploration_ready"
    assert view["prompt"]["available_phases"] == ["explore", "interview"]
    assert accept(target, "increment")["stage"] == "ready"


def test_reports_reject_traversal_and_symlink_escape(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    source.start("example", "Explain this")
    archive = export_handoff(source, ["example"])
    archive["tasks"][0]["reports"]["../../outside"] = "bad"
    with pytest.raises(ValueError, match="path"):
        preview_handoff(target, archive)
    (Path(source._state("example")["report_directory"]) / "leak").symlink_to(source.repository)
    with pytest.raises(ValueError, match="symbolic"):
        export_handoff(source, ["example"])


def test_memory_hash_and_size_are_validated(rig, recipient, monkeypatch):
    source, _, _ = rig
    target, _, _ = recipient
    source.context.save(source.repository, notes="Original", sources=[], expected_current=None)
    archive = export_handoff(source, [])
    tampered = copy.deepcopy(archive)
    tampered["memory"][0]["notes"] = "Tampered"
    with pytest.raises(ValueError, match="memory version"):
        preview_handoff(target, tampered)
    monkeypatch.setattr("lambda_harness.handoff.MAX_HANDOFF_BYTES", 10)
    with pytest.raises(ValueError, match="32 MB"):
        preview_handoff(target, archive)


def test_bundle_cannot_install_other_git_references(rig, recipient):
    source, _, _ = rig
    target, _, _ = recipient
    view = reach_test_review(source)
    source.respond("increment", "approve", approval_token=view["prompt"]["approval_token"])
    archive = export_handoff(source, ["increment"])
    packet = archive["tasks"][0]
    packet["bundle"] = base64.b64encode(
        base64.b64decode(packet["bundle"]).replace(b" HEAD\n", b" refs/heads/main\n", 1)
    ).decode()
    preview = preview_handoff(target, archive)
    original = git(target.repository, "rev-parse", "main")
    with pytest.raises(ValueError, match="bundle"):
        import_handoff(target, archive, confirmation=preview["confirmation"])
    assert git(target.repository, "rev-parse", "main") == original


def test_task_chat_travels_with_handoff_and_can_continue(rig, recipient):
    from lambda_harness.conversation import conversation_messages

    from .test_phases import specification

    source, agent, _ = rig
    target, target_agent, _ = recipient
    specification(source)
    agent.responses["chat"].append({"answer": "Suggested commit message", "references": []})
    source.task_chat("task", "Suggest a commit message")
    transfer(source, target, ["task"])
    accept(target, "task")
    assert conversation_messages(target._state("task"))[-1]["answer"] == "Suggested commit message"
    target_agent.responses["chat"].append({"answer": "Shorter commit message", "references": []})
    target.task_chat("task", "Make it shorter")
    assert any(
        m.get("answer") == "Suggested commit message" for m in target_agent.calls[-1][1]["messages"]
    )
    assert target.inspect("task")["stage"] == "specification_ready"
