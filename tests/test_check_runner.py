"""Discover before executing; keep formatting in the selected worktree."""

from pathlib import Path

import pytest

from lambda_harness.process import ProcessError
from lambda_harness.schemas import CheckPlan
from lambda_harness.validation import LocalExecutor, ValidationInterrupted

from .conftest import SOURCE, command_result
from .test_phases import refactored


def plan(**overrides):
    return {
        "summary": "Read the local test recipes and their suite coverage.",
        "sources": ["example-app/example-app.cabal"],
        "coverage": ["Formatting and lint", "Aggregate checks", "All project suites"],
        "omissions": [],
        "questions": [],
        "commands": [
            {
                "name": name,
                "argv": argv,
                "directory": ".",
                "environment": env,
                "purpose": name,
                "modifies_files": writes,
            }
            for name, argv, env, writes in [
                ("Format", ["nix", "fmt"], "project", True),
                ("Lint", ["just", "lint"], "nix", True),
                ("Check", ["just", "check"], "project", False),
                ("Remaining suites", ["cabal", "test", "all"], "nix", False),
            ]
        ],
        **overrides,
    }


class CheckExecutor:
    def __init__(self, *, modify=False, fail=False, stop=False, unexpected=False):
        self.calls = []
        self.modify, self.fail, self.stop, self.unexpected = modify, fail, stop, unexpected

    def __call__(self, argv, cwd, **options):
        self.calls.append((argv, cwd, options))
        if self.modify and argv == ["nix", "fmt"]:
            path = cwd / SOURCE
            path.write_text(path.read_text() + "-- Formatted\n")
        if self.unexpected and argv == ["just", "check"]:
            (cwd / SOURCE).write_text("-- Unexpected rewrite\n")
        if self.stop and argv == ["just", "check"]:
            raise ValidationInterrupted(argv, ProcessError("stop", kind="cancelled"))
        return command_result(argv, exit_code=1 if self.fail and argv == ["just", "check"] else 0)


def start(rig, proposed=None):
    harness, agent, _ = rig
    agent.responses["test_runner"].append(proposed or plan())
    return harness.start_checks("checks", "Run the full checks", base="main")


def run(harness, view, action="run_checks"):
    return harness.respond(view["task_id"], action, approval_token=view["prompt"]["approval_token"])


def test_discovery_precedes_execution_and_failures_do_not_skip_other_suites(rig):
    harness, agent, _ = rig
    executor = CheckExecutor(fail=True)
    harness.execute = executor
    view = start(rig)
    assert view["stage"] == "checks_plan" and not executor.calls
    assert agent.calls[-1][0] == "test_runner"
    assert "project_inventory" in agent.calls[-1][1]
    assert agent.calls[-1][1]["agent_profile"]["definition"]["role"] == "test_runner"
    view = run(harness, view)
    assert view["stage"] == "checks_failed" and len(executor.calls) == 4
    assert view["check_results"]["not_run"] == []
    assert view["check_results"]["commands"][2]["exit_code"] == 1
    assert view["outcome"]["kind"] == "error"
    assert not harness._state("checks")["commits"]
    assert (Path(view["check_results"]["report_directory"]) / "report.json").is_file()


def test_direct_task_checks_keep_formatting_and_require_revalidation(rig):
    harness, agent, normal_executor = rig
    original = refactored(harness)
    harness.execute = CheckExecutor(modify=True)
    agent.responses["test_runner"].append(plan())
    view = harness.run_task_checks("task")
    assert view["worktree"] == original["worktree"]
    view = run(harness, view)
    assert view["stage"] == "checks_complete"
    assert (Path(original["worktree"]) / SOURCE).read_text().endswith("-- Formatted\n")
    view = run(harness, view, "return_to_task")
    assert view["stage"] == "checks_changed"
    with pytest.raises(ValueError, match="pending"):
        harness.run_phase("task", "review-haskell")
    harness.execute = normal_executor
    before = len(normal_executor.calls)
    view = run(harness, view, "revalidate_changes")
    assert view["stage"] == "refactor_ready" and len(normal_executor.calls) > before
    assert not harness._state("task").get("haskell_reviewer")
    assert "check_history" in harness._state("task")


def test_unchanged_checks_restore_original_phase(rig):
    harness, agent, _ = rig
    original = refactored(harness)
    harness.execute = CheckExecutor()
    agent.responses["test_runner"].append(plan())
    view = run(harness, harness.run_task_checks("task"))
    view = run(harness, view, "return_to_task")
    assert view["stage"] == original["stage"]
    assert view["prompt"] == original["prompt"]
    assert harness.run_phase("task", "review-haskell")["stage"] == "reviews_ready"


def test_stopped_run_records_unrun_suites_and_can_be_rerun(rig):
    harness, _, _ = rig
    executor = CheckExecutor(stop=True)
    harness.execute = executor
    view = run(harness, start(rig))
    assert view["check_results"]["status"] == "stopped"
    assert view["check_results"]["not_run"] == ["Remaining suites"]
    executor.stop = False
    assert run(harness, view)["check_results"]["status"] == "passed"


def test_omissions_never_report_all_checks_passed(rig):
    harness, _, _ = rig
    harness.execute = CheckExecutor()
    view = run(harness, start(rig, plan(omissions=["Nightly AWS suite needs credentials"])))
    assert view["check_results"]["status"] == "partial"
    assert view["outcome"]["kind"] != "success"


def test_unanswered_questions_block_execution(rig):
    harness, _, _ = rig
    harness.execute = CheckExecutor()
    view = start(rig, plan(questions=["Which database service should tests use?"]))
    assert "run_checks" not in view["prompt"]["actions"]
    with pytest.raises(ValueError, match="not available"):
        run(harness, view)


def test_plan_cannot_run_after_checkout_changes(rig):
    harness, _, _ = rig
    harness.execute = CheckExecutor()
    view = start(rig)
    (Path(view["worktree"]) / SOURCE).write_text("-- changed\n")
    with pytest.raises(ValueError, match="stale"):
        run(harness, view)
    assert not harness.execute.calls


def test_unexpected_source_write_stops_verification(rig):
    harness, _, _ = rig
    harness.execute = CheckExecutor(unexpected=True)
    view = run(harness, start(rig))
    assert view["check_results"]["status"] == "failed"
    assert view["check_results"]["not_run"] == ["Remaining suites"]
    assert "unexpectedly changed" in view["check_results"]["commands"][-1]["error"]


def test_command_environments_use_project_root_for_nix(monkeypatch, tmp_path):
    captured = []

    def execute(argv, cwd, **kw):
        captured.append(argv)
        return 0, "", ""

    monkeypatch.setattr("lambda_harness.validation.run_process", execute)
    executor = LocalExecutor()
    executor(["nix", "fmt"], tmp_path, use_nix=False)
    executor(["pytest"], tmp_path / "subdir", use_nix=True, project_root=tmp_path)
    assert captured == [["nix", "fmt"], ["nix", "develop", str(tmp_path), "--command", "pytest"]]


def test_plan_rejects_late_formatting_and_escaping_directories():
    proposed = plan()
    proposed["commands"].reverse()
    with pytest.raises(ValueError, match="formatters before"):
        CheckPlan.model_validate(proposed)
    proposed = plan()
    proposed["commands"][0]["directory"] = "../other"
    with pytest.raises(ValueError, match="inside"):
        CheckPlan.model_validate(proposed)


def test_runner_plan_survives_restart_and_can_be_exported(rig, repository, tmp_path):
    from lambda_harness.contracts import Harness
    from lambda_harness.handoff import export_handoff

    harness, agent, _ = rig
    view = start(rig)
    harness.close()
    resumed = Harness(repository, tmp_path / "state", agent, CheckExecutor())
    try:
        assert resumed.inspect("checks")["check_plan"] == view["check_plan"]
        assert export_handoff(resumed, ["checks"])
        assert run(resumed, view)["check_results"]["status"] == "passed"
    finally:
        resumed.close()


def test_revalidation_does_not_skip_a_requested_test_phase(rig):
    harness, agent, normal_executor = rig
    refactored(harness)
    state = harness._state("task")
    harness._phase_complete(state, "tests_requested", "Add regression coverage for a finding.")
    harness.graph.update_state(harness._config("task"), state, as_node="refactor")
    harness.execute = CheckExecutor(modify=True)
    agent.responses["test_runner"].append(plan())
    view = run(harness, harness.run_task_checks("task"))
    view = run(harness, view, "return_to_task")
    harness.execute = normal_executor
    view = run(harness, view, "revalidate_changes")
    assert view["stage"] == "tests_requested"
    assert view["prompt"]["available_phases"] == ["tests"]


def test_interrupted_command_journal_is_not_silently_replayed(rig):
    from lambda_harness.workspace import save_json

    harness, _, _ = rig
    harness.execute = CheckExecutor()
    view = start(rig)
    state = harness._state("checks")
    # reply is the next node; run_checks will follow it at seq + 1.
    directory = Path(state["report_directory"]) / f"checks-{state['seq'] + 1:04d}"
    save_json(directory / "started.json", {"plan": state["check_plan"]})
    view = run(harness, view)
    assert view["check_results"]["status"] == "stopped"
    assert len(view["check_results"]["not_run"]) == 4
    assert not harness.execute.calls


def test_existing_edits_also_require_revalidation_before_returning(rig):
    harness, agent, _ = rig
    original = refactored(harness)
    path = Path(original["worktree"]) / SOURCE
    path.write_text(path.read_text() + "-- Edited before invoking the runner\n")
    harness.execute = CheckExecutor()
    agent.responses["test_runner"].append(plan())
    view = run(harness, harness.run_task_checks("task"))
    assert run(harness, view, "return_to_task")["stage"] == "checks_changed"
