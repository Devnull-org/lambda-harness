"""Repository-driven check plans and explicit execution, separate from code generation."""

import copy
import json
import shlex
from pathlib import Path

from .process import ProcessError
from .validation import ValidationInterrupted
from .validation_discovery import project_inventory, source_hashes, validation_environment
from .workspace import save_json


class CheckRunner:
    def start_checks(self, task_id, request, *, base="HEAD"):
        with self._lock():
            return self._start(
                task_id,
                request or "Discover and run all repository checks and test suites.",
                base=base,
                phase="plan_checks",
                standalone={"task_kind": "checks"},
            )

    def run_task_checks(self, task_id):
        """Use the selected task's actual worktree; retain its workflow for returning later."""
        with self._lock():
            state = copy.deepcopy(self._state(task_id))
            if self.graph.get_state(self._config(task_id)).next:
                raise ValueError("Finish or resolve the current step before starting a check run")
            if state.get("task_kind") in {"review", "agent_builder"}:
                raise ValueError("Run checks from a coding task or start a dedicated check task")
            origin = {
                k: v
                for k, v in state.items()
                if not k.startswith(("check_", "checks_")) and k not in {"messages", "seq"}
            }
            state["checks_origin"] = origin
            profile = self.agents.selected(self.repository).get("test_runner")
            if profile:
                state["agent_profiles"] = {
                    **state.get("agent_profiles", {}),
                    "test_runner": profile,
                }
            state["checks_started_fingerprint"] = state.get(
                "phase_fingerprint", self._workspace(state).fingerprint()
            )
            state["next"] = "plan_checks"
            state["check_results"] = None
            self.graph.invoke(state, self._config(task_id))
            return self._view(self._state(task_id))

    def _plan_checks(self, state):
        workspace = self._workspace(state)
        plan = self._ask(
            state,
            "test_runner",
            project_inventory=project_inventory(workspace),
            execution_environment=validation_environment(self.execute),
            previous_check_plan=state.get("check_plan"),
            previous_check_results=state.get("check_results"),
        )
        source_hashes(workspace, [{"path": name} for name in plan["sources"]])
        for command in plan["commands"]:
            directory = workspace.safe_path(command["directory"])
            if not directory.is_dir():
                raise ValueError(f"Check directory does not exist: {command['directory']}")
        state["check_plan"] = plan
        state["messages"].append({"role": "test_runner", "answer": plan["summary"]})
        self._gate(
            state,
            "checks_plan",
            {
                "summary": "Test runner: command plan ready",
                "guidance": "Review the commands, suite coverage, and omissions below. "
                "Commands marked as rewriting files run first; changes stay in this worktree. "
                "The runner does not create commits or repair failing code.",
                "questions": plan["questions"],
            },
            [
                "question",
                "revise",
                *([] if plan["questions"] else ["run_checks"]),
                *(["return_to_task"] if state.get("checks_origin") else []),
            ],
        )

    def _run_checks(self, state):
        workspace = self._workspace(state)
        plan = state["check_plan"]
        directory = Path(state["report_directory"]) / f"checks-{state['seq']:04d}"
        record = directory / "results.json"
        started = directory / "started.json"
        results = []
        interrupted = False
        if started.exists():
            # A process crash must not silently replay commands with file-writing effects.
            results = json.loads(record.read_text()) if record.exists() else []
            interrupted = True
        else:
            save_json(started, {"plan": plan, "fingerprint": workspace.fingerprint()})
            for index, command in enumerate(plan["commands"]):
                before = workspace.fingerprint()
                source_before = {name: workspace.file_hash(name) for name in workspace.files()}
                head_before = workspace.revision
                self.progress(
                    f"{state['task_id']}: {command['name']} · {shlex.join(command['argv'])}"
                )
                try:
                    result = self.execute(
                        command["argv"],
                        workspace.safe_path(command["directory"]),
                        use_nix=command["environment"] == "nix",
                        project_root=workspace.path,
                    )
                except ValidationInterrupted as error:
                    result = error.result
                    interrupted = True
                except ProcessError as error:
                    result = {
                        "argv": command["argv"],
                        "exit_code": None,
                        "stdout": error.stdout,
                        "stderr": str(error) + "\n" + error.stderr,
                    }
                after = workspace.fingerprint()
                entry = {
                    "name": command["name"],
                    **result,
                    "files_changed": before != after,
                    "modifies_files": command["modifies_files"],
                }
                source_changed = workspace.revision != head_before or any(
                    workspace.file_hash(name) != value for name, value in source_before.items()
                )
                if workspace.revision != head_before:
                    entry["error"] = (
                        "A check changed Git HEAD. Inspect the worktree before continuing."
                    )
                elif source_changed and not command["modifies_files"]:
                    entry["error"] = (
                        "A verification command unexpectedly changed files. "
                        "Inspect the diff before continuing."
                    )
                results.append(entry)
                save_json(directory / f"{index + 1:02d}.json", entry)
                save_json(record, results)
                self.progress(
                    f"{state['task_id']}: {command['name']} "
                    + (
                        "passed"
                        if entry["exit_code"] == 0 and not entry.get("error")
                        else "failed or stopped"
                    )
                )
                if interrupted or entry.get("error"):
                    break
        missing = [item["name"] for item in plan["commands"][len(results) :]]
        failures = [item["name"] for item in results if item["exit_code"] != 0 or item.get("error")]
        status = (
            "stopped"
            if interrupted
            else "failed"
            if failures or missing
            else "partial"
            if plan["omissions"]
            else "passed"
        )
        summary = {
            "passed": "All planned checks and test commands passed.",
            "partial": "All planned commands passed, but some checks or suites were omitted.",
            "failed": "Checks finished with failures: " + ", ".join(failures),
            "stopped": "Check run interrupted. Completed results are saved; "
            "remaining commands did not run.",
        }[status]
        run = {
            "status": status,
            "summary": summary,
            "commands": results,
            "not_run": missing,
            "omissions": plan["omissions"],
            "fingerprint": workspace.fingerprint(),
            "report_directory": str(directory),
            "diff": workspace.diff(),
            "coverage": plan["coverage"],
        }
        state["check_results"] = run
        state.setdefault("check_history", []).append(run)
        save_json(directory / "report.json", run)
        state["messages"].append(
            {"role": "test_runner", "answer": summary, "report_directory": str(directory)}
        )
        self._gate(
            state,
            "checks_complete" if status in {"passed", "partial"} else "checks_failed",
            {
                "summary": summary,
                "guidance": "Review every command result below. "
                "No commits were created. You can rerun all commands or revise the plan.",
            },
            [
                "question",
                "revise",
                "run_checks",
                *(["return_to_task"] if state.get("checks_origin") else []),
            ],
        )

    def _checks_reply(self, state, action, text):
        if action in {"question", "revise"}:
            state["messages"].append({"agent_role": "test_runner", "text": text})
            state["next"] = "plan_checks"
        elif action == "run_checks":
            if state["check_plan"]["questions"]:
                raise ValueError("Resolve the plan's questions before running commands")
            state["next"] = "run_checks"
        elif action == "return_to_task":
            if self._workspace(state).fingerprint() != state["checks_started_fingerprint"]:
                self._gate(
                    state,
                    "checks_changed",
                    {
                        "summary": "Checks changed the worktree. Earlier validation and reviews "
                        "are now stale.",
                        "guidance": "Inspect the worktree diff. "
                        "It includes earlier uncommitted edits. "
                        "Accepting keeps these changes, "
                        "updates the approved test snapshot if tests were formatted, "
                        "and reruns workflow validation "
                        "before implementation or review continues.",
                        "diff": self._workspace(state).diff(),
                    },
                    ["revalidate_changes", "question"],
                )
            else:
                self._restore_after_checks(state, changed=False)
        elif action == "revalidate_changes":
            self._restore_after_checks(state, changed=True)

    def _restore_after_checks(self, state, *, changed):
        origin = state.pop("checks_origin")
        for key in list(state):
            if key not in {"seq", "messages", "check_history"}:
                state.pop(key)
        state.update(origin)
        if changed:
            workspace = self._workspace(state)
            if state.get("role") and state.get("proposal"):
                if state["stage"] in {"tests_requested", "refactor_requested"}:
                    state["checks_resume_boundary"] = state["stage"]
                state["approved_tests"] = workspace.test_manifest(state.get("test_paths", []))
                state.pop("validated_fingerprint", None)
                state.pop("haskell_reviewer", None)
                state.pop("security_reviewer", None)
                state["next"] = "validate"
            else:
                state["phase_fingerprint"] = workspace.fingerprint()
