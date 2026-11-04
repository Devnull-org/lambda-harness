"""A conversational controller with native workers, independent of phase ordering."""

import copy
import json
from pathlib import Path
from uuid import uuid4

from .failures import AgentInterrupted, BackendError
from .schemas import REVIEWERS, validate_response
from .workspace import save_json

WORKERS = {"coding_worker", "check_worker", "research_worker", "review_worker"}


def recent_dialogue(state):
    """Keep task intent and results without copying old replacement-file proposals."""
    records = []
    for message in state["messages"]:
        record = {
            key: message[key]
            for key in ("role", "agent_role", "text", "question", "answer", "summary")
            if message.get(key)
        }
        if record:
            records.append(record)
    selected, size = [], 0
    for record in reversed(records):
        count = len(json.dumps(record, ensure_ascii=False).encode())
        if selected and size + count > 100_000:
            break
        if count > 100_000:
            continue
        selected.append(record)
        size += count
        if len(selected) >= 30:
            break
    return list(reversed(selected))


def controller_results(results):
    """Keep recent evidence bounded; full command receipts stay on disk."""
    selected = copy.deepcopy(results[-6:])
    for result in selected:
        result["summary"] = result["summary"][:8000]
        commands = result.get("commands", [])
        result["command_count"] = len(commands)
        result["commands"] = commands[-20:]
        for command in result["commands"]:
            command["command"] = command.get("command", "")[:2000]
            command["output_tail"] = command.get("output_tail", "")[-1000:]
    return selected


class Orchestrator:
    def orchestrate(self, task_id, text):
        if not text.strip():
            raise ValueError("Tell the orchestrator what you want to do")
        if self._state(task_id).get("task_kind") in {"agent_builder", "review"}:
            return self.task_chat(task_id, text)
        with self._lock(task_id):
            state = copy.deepcopy(self._state(task_id))
            if state.get("worktree") and not Path(state["worktree"]).is_dir():
                answer = (
                    "This task's worktree is missing, so I cannot inspect or continue its "
                    "saved changes. Restore the worktree at its saved path, or start a new "
                    "task from the repository. The previous conversation is still available."
                )
                state["messages"].append({"role": "orchestrator", "text": text})
                self._orchestration_failure(state, answer)
                self.graph.update_state(self._config(task_id), state, as_node="finish")
                return self._view(state)
            self._start_orchestration_turn(state, text)
            config = self._config(task_id)
            # Adopt the current worktree, including a legacy pending phase. No
            # approval, test result, or commit is manufactured by this transition.
            self.graph.update_state(config, state, as_node="finish")
            self.graph.invoke(state, config)
            return self._view(self._state(task_id))

    @staticmethod
    def _orchestration_failure(state, answer):
        """Persist failures without requiring the broken checkout to be readable."""
        state["orchestrated"] = True
        state.setdefault("orchestration", {})["status"] = "waiting"
        state["messages"].append({"role": "orchestrator", "answer": answer})
        state["stage"], state["next"] = "orchestrator_waiting", "end"
        state["prompt"] = {"summary": answer, "error": answer, "actions": ["question"]}

    def _start_orchestration_turn(self, state, text):
        if not state.get("worktree"):
            self._create_worktree(state)
        if state.get("orchestration", {}).get("status") == "working":
            state["messages"].append(
                {
                    "role": "orchestrator",
                    "answer": (
                        "The previous turn stopped before completion. "
                        "Inspect partial work before continuing."
                    ),
                }
            )
        state["orchestrated"] = True
        state["orchestration"] = {
            "turn": uuid4().hex,
            "instruction": text,
            "previous_stage": state.get("stage"),
            "results": [],
            "status": "working",
        }
        state["messages"].append({"role": "orchestrator", "text": text})
        state["stage"], state["next"] = "orchestrating", "orchestrator"
        state["prompt"] = {"actions": []}
        state.pop("agent_integrity_fingerprint", None)
        state.pop("checks_origin", None)
        state.pop("checking_baseline", None)
        state.pop("instruction_boundary", None)
        state.pop("failed_reply", None)
        # Existing tasks also receive the current controller/worker definitions.
        profiles = self.agents.selected(self.repository)
        state.setdefault("agent_profiles", {}).update(
            {key: value for key, value in profiles.items() if key in WORKERS | {"orchestrator"}}
        )

    def _orchestrator(self, state):
        if not state.get("orchestrated"):
            self._start_orchestration_turn(state, state["request"])
        run = state["orchestration"]
        decision = self._ask(
            state,
            "orchestrator",
            latest_user_message=run["instruction"],
            dialogue=recent_dialogue(state),
            worker_results=controller_results(run["results"]),
            legacy_stage=run.get("previous_stage"),
            workflow_actions=[],
            messages=recent_dialogue(state),
            # The controller can inspect the diff with its own tools. Avoid
            # repeating large source patches in every controller turn.
            diff="Inspect the task worktree with git diff when needed.",
        )
        state["messages"].append({"role": "orchestrator", "answer": decision["answer"]})
        run["status"] = decision["status"]
        if decision["action"] == "respond":
            state["stage"] = (
                "orchestrator_ready" if decision["status"] == "complete" else "orchestrator_waiting"
            )
            state["prompt"] = {"summary": decision["answer"], "actions": ["question"]}
            state["next"] = "end"
            return
        run["assignment"] = {
            "id": uuid4().hex,
            "worker": decision["worker"],
            "instruction": decision["instruction"],
        }
        state["stage"], state["next"] = "orchestrating", "orchestrator_worker"
        state["prompt"] = {"summary": decision["answer"], "actions": []}

    def _orchestrator_worker(self, state):
        run = state["orchestration"]
        assignment = run["assignment"]
        role = assignment["worker"]
        if role not in WORKERS:
            raise ValueError("Unknown delegated worker")
        workspace = self._workspace(state)
        directory = Path(state["directory"]) / "delegations" / f"{assignment['id']}-{role}"
        started, receipt = directory / "started.json", directory / "result.json"
        before = workspace.fingerprint()
        if receipt.exists():
            result = json.loads(receipt.read_text())
        elif started.exists():
            # A crash may happen after real edits but before the reply is saved.
            # Return to the controller to inspect those edits instead of replaying.
            result = {
                "summary": (
                    "The previous worker was interrupted before returning. "
                    "Inspect the current worktree and continue from its saved changes."
                ),
                "status": "blocked",
                "remaining": ["Inspect partial work before continuing"],
                "commands": [],
                "files_changed": workspace.fingerprint()
                != json.loads(started.read_text())["fingerprint"],
            }
        else:
            save_json(started, {**assignment, "fingerprint": before})
            context = {
                "request": state["request"],
                "worker_role": role,
                "worker_instruction": assignment["instruction"],
                "latest_user_message": run["instruction"],
                "agent_timeout": state.get("agent_timeout"),
                "specification": state.get("specification"),
                "project_context": self.context.for_agent(state["project_context_id"], workspace)
                if state.get("project_context_id")
                else None,
            }
            if profile := state.get("agent_profiles", {}).get(role):
                context["agent_profile"] = profile
            self.progress(
                f"{state['task_id']}: {role.replace('_', ' ')} · {assignment['instruction'][:180]}"
            )
            try:
                result = validate_response(role, context, self.agent(role, context, workspace.path))
            except (AgentInterrupted, KeyboardInterrupt):
                state["interrupted_draft"] = run["instruction"]
                raise AgentInterrupted() from None
            except (BackendError, ValueError, RuntimeError, OSError) as error:
                result = {
                    "summary": str(error) or type(error).__name__,
                    "status": "blocked",
                    "remaining": ["Resolve the worker error"],
                }
            result["files_changed"] = workspace.fingerprint() != before
            result["commands"] = copy.deepcopy(getattr(self.agent, "last_commands", []))
            if role in {"research_worker", "review_worker"} and result["files_changed"]:
                result["status"] = "blocked"
                result["summary"] = (
                    "A read-only worker changed files. Inspect the worktree before continuing."
                )
            if generation := getattr(self.agent, "last_generation", None):
                result["generation"] = copy.deepcopy(generation)
            save_json(receipt, result)
        if not receipt.exists():
            save_json(receipt, result)
        if result.get("files_changed"):
            # Previous phase evidence does not validate the newly edited tree.
            for key in (
                "validation",
                "validated_fingerprint",
                "reviews",
                "review_fingerprint",
                "gate_fingerprint",
                "phase_fingerprint",
                *REVIEWERS,
            ):
                state.pop(key, None)
        result = {"worker": role, **result, "receipt": str(receipt)}
        run["results"].append(result)
        state["messages"].append(
            {"role": role, "worker_result": result, "answer": result["summary"]}
        )
        state["stage"], state["next"] = "orchestrating", "orchestrator"
        state["prompt"] = {"actions": []}
