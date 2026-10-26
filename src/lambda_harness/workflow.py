"""Persistent local TDD workflow with explicit specification and test approval."""

import copy
import fcntl
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from .agent_context import CODING_ROLES, NODE_ROLES, messages_for_agent
from .agent_library import AVATARS, ROLES, AgentLibrary
from .check_runner import CheckRunner
from .cleanup import cleanup_plan, remove_resources
from .conversation import conversation_messages, interview_message
from .failures import AgentInterrupted, BackendError, BackendResponseError
from .github_actions import post_review_comment
from .link_checks import check_documentation_links
from .project_context import ContextStore, state_directory
from .recovery import can_recover, commit_failed, recovery_actions, recovery_prompt
from .reports import chat_report, exploration_report, pr_description, standalone_review_report
from .review_context import review_diff
from .review_source import resolve_review
from .schemas import REVIEWERS, validate_response
from .validation import ValidationConfig, ValidationInterrupted, classify_validation, test_evidence
from .validation_discovery import (
    knowledge_is_current,
    prepare_knowledge,
    project_inventory,
    saved_overrides,
    validation_environment,
)
from .workspace import (
    Workspace,
    cabal_library_dependencies_only,
    digest,
    git,
    is_test_path,
    protected_configuration,
    resolve_base,
    save_json,
    task_branch,
    validate_task_name,
)

PHASE_NODES = {
    "explore": "explore",
    "interview": "interview",
    "tests": "tests",
    "implement": "implement",
    "refactor": "refactor",
    "review-haskell": "review_haskell",
    "review-security": "review_security",
    "walkthrough": "walkthrough",
    "handoff": "finish",
}


class Harness(CheckRunner):
    def __init__(
        self,
        repository,
        state_dir,
        agent,
        execute,
        max_repair_rounds=3,
        *,
        unsigned_commits=False,
        progress=None,
        validation=None,
        agent_repository=None,
        pause_on_error=False,
    ):
        self.repository = Path(repository).expanduser().resolve()
        if Path(git(self.repository, "rev-parse", "--show-toplevel")).resolve() != self.repository:
            raise ValueError("Repository must be the root of a Git checkout")
        self.state_dir = state_directory(self.repository, state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.context = ContextStore(self.state_dir)
        self.agents = AgentLibrary(Path(state_dir) / "agents", agent_repository)
        self.agent, self.execute = agent, execute
        self.max_repair_rounds = max(1, max_repair_rounds)
        self.pause_on_error = pause_on_error
        self.unsigned_commits = unsigned_commits
        self.progress = progress or (lambda message: None)
        self.validation = validation or ValidationConfig()
        self.connection = sqlite3.connect(
            self.state_dir / "checkpoints.sqlite", check_same_thread=False
        )
        builder = StateGraph(dict)
        nodes = (
            "plan_checks",
            "run_checks",
            "prepare",
            "chat",
            "agent_builder",
            "explore",
            "standalone_review",
            "interview",
            "worktree",
            "existing_tests",
            "discover_validation",
            "tests",
            "implement",
            "refactor",
            "validate",
            "commit",
            "review_haskell",
            "review_security",
            "review_gate",
            "walkthrough",
            "finish",
            "reply",
        )
        for name in nodes:
            builder.add_node(name, self._node(name))
        builder.add_node("pause", self._pause)
        routes = {name: name for name in (*nodes, "pause")}
        routes["end"] = END
        builder.add_conditional_edges(START, lambda state: state["next"], routes)
        for name in (*nodes, "pause"):
            builder.add_conditional_edges(name, lambda state: state["next"], routes)
        self.graph = builder.compile(checkpointer=SqliteSaver(self.connection))

    @contextmanager
    def _lock(self):
        with (self.state_dir / "lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Another harness process is running for this repository"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _config(self, task_id):
        validate_task_name(task_id)
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 500}

    def _state(self, task_id, *, allow_abandoning=False):
        state = self.graph.get_state(self._config(task_id)).values
        if not state:
            raise ValueError(f"Unknown task: {task_id}")
        if state["stage"] == "abandoning" and not allow_abandoning:
            raise ValueError(f"Task deletion is unfinished; run abandon {task_id} again")
        return state

    def start(
        self, task_id, request, *, base="HEAD", mode="phased", walkthrough=False, phase="interview"
    ):
        with self._lock():
            return self._start(
                task_id, request, base=base, mode=mode, walkthrough=walkthrough, phase=phase
            )

    def start_agent_builder(self, task_id, request, *, agent_id=None):
        with self._lock():
            if not self.agents.writable:
                raise ValueError("Choose a writable agent repository on the Agents page first")
            existing = self.agents.get(agent_id) if agent_id else None
            return self._start(
                task_id,
                request,
                phase="agent_builder",
                standalone={
                    "task_kind": "agent_builder",
                    "agent_origin": {
                        "id": existing["id"] if existing else uuid4().hex,
                        "version": existing["version"] if existing else None,
                        "repository": self.agents.catalog,
                    },
                    "draft_definition": existing["definition"] if existing else None,
                },
            )

    def start_tests(self, task_id, request, *, packages, criteria, base="HEAD", expectation="red"):
        """Write and validate tests from a developer's scope, stopping after test approval."""
        if expectation not in {"red", "green"}:
            raise ValueError("Choose failing regression tests or passing coverage tests")
        for name, items in (("packages", packages), ("criteria", criteria)):
            if (
                not isinstance(items, list)
                or not items
                or not all(isinstance(item, str) and item.strip() for item in items)
            ):
                raise ValueError(f"Provide at least one nonempty {name} entry")
        specification = {
            "problem": request,
            "expected_behavior": request,
            "acceptance_criteria": [
                {"id": f"A{i}", "description": text.strip()} for i, text in enumerate(criteria, 1)
            ],
            "packages": list(dict.fromkeys(package.strip() for package in packages)),
            "out_of_scope": ["Production implementation and refactoring"],
            "constraints": ["Write only tests, fixtures, and necessary Cabal test wiring."],
            "open_questions": [],
            "assumptions": [],
        }
        with self._lock():
            # Reject misspelled packages before creating a task or invoking a model.
            revision = resolve_base(self.repository, base)
            files = git(self.repository, "ls-tree", "-r", "--name-only", revision).splitlines()
            for package in specification["packages"]:
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", package) or not any(
                    Path(path).name == package + ".cabal" for path in files
                ):
                    raise ValueError(f"Unknown Cabal package: {package}")
            return self._start(
                task_id,
                request,
                base=base,
                phase="worktree",
                standalone={
                    "task_kind": "tests",
                    "specification": specification,
                    "test_expectation": expectation,
                },
            )

    def start_review(self, task_id, request, *, base, head, reviewers="both", source=None):
        """Inspect a pinned change without running validation, editing code, or publishing."""
        if reviewers not in REVIEWERS:
            raise ValueError(
                "Choose code, security, documentation, code + security, or all reviewers"
            )
        with self._lock():
            source = source or resolve_review(self.repository, base, head)
            return self._start(
                task_id,
                request,
                base=source["merge_base"],
                phase="standalone_review",
                standalone={
                    "task_kind": "review",
                    "review_source": source,
                    "reviewers": reviewers,
                    "starting_revision": source["head"],
                },
            )

    def _start(
        self,
        task_id,
        request,
        *,
        base="HEAD",
        mode="phased",
        walkthrough=False,
        restarted_from=None,
        phase="interview",
        standalone=None,
    ):
        config = self._config(task_id)
        if self.graph.get_state(config).values:
            raise ValueError(f"Task already exists: {task_id}; resume it instead")
        if mode not in {"phased", "full", "interview"} or not request.strip():
            raise ValueError("A request and a phased, full, or interview mode are required")
        if phase not in {"interview", "explore", "chat"} and not standalone:
            raise ValueError("A new task must start with interview, explore, or chat")
        base_revision = resolve_base(self.repository, base)
        agent_profiles = {} if phase == "agent_builder" else self.agents.selected(self.repository)
        directory = self.state_dir / task_id
        directory.mkdir(mode=0o700)
        state = {
            "task_id": task_id,
            "request": request,
            "mode": mode,
            "initial_phase": phase,
            "walkthrough_enabled": walkthrough,
            "directory": str(directory),
            "report_directory": str(directory / "reports"),
            "base": base_revision,
            "base_ref": base,
            "stage": "preparing",
            "prompt": {"summary": "Preparing the task worktree", "actions": []},
            "next": "prepare",
            "seq": 0,
            "messages": [],
            "validations": [],
            "commits": [],
            "review_round": 0,
            "dispositions": [],
            "attempts": 0,
            "project_context_id": None if phase == "agent_builder" else self.context.latest_id(),
            "agent_profiles": agent_profiles,
            "validation_config": self.validation.model_dump(),
            "validation_overrides": self.validation.model_dump(exclude_unset=True),
        }
        if phase == "chat":
            state["task_kind"], state["mode"] = "chat", "phased"
        if standalone:
            state.update(copy.deepcopy(standalone))
            state["standalone_start"] = copy.deepcopy(standalone)
            state["mode"] = "phased"
        if restarted_from:
            state["restarted_from"] = restarted_from
        self.graph.invoke(state, config)
        return self._view(self._state(task_id))

    def restart(self, task_id, *, new_task_id=None, request=None, base=None):
        """Start a new attempt without changing the previous task or its worktree."""
        with self._lock():
            original = self._state(task_id)
            if base is not None and original.get("task_kind") == "review":
                raise ValueError("Start a new review to choose a different comparison base")
            if new_task_id is None:
                attempt = 1
                while True:
                    suffix = f"-restart-{attempt}"
                    candidate = task_id[: 64 - len(suffix)] + suffix
                    if (
                        not self.graph.get_state(self._config(candidate)).values
                        and not (self.state_dir / candidate).exists()
                    ):
                        new_task_id = candidate
                        break
                    attempt += 1
            return self._start(
                new_task_id,
                original["request"] if request is None else request,
                base=original["base"] if base is None else base,
                walkthrough=original["walkthrough_enabled"],
                restarted_from=task_id,
                phase=original.get("initial_phase", "interview"),
                standalone=original.get("standalone_start"),
            )

    def inspect(self, task_id):
        with self._lock():
            return self._view(self._state(task_id, allow_abandoning=True))

    def _abandon_plan(self, task_id):
        state = self._state(task_id, allow_abandoning=True)
        plan = cleanup_plan(self.repository, self.state_dir, state)
        checkpoint = self.graph.get_state(self._config(task_id)).config
        return {**plan, "confirmation_token": digest([checkpoint, plan])}

    def abandon_plan(self, task_id):
        """Describe exactly which task resources a confirmation will remove."""
        with self._lock():
            return self._abandon_plan(task_id)

    def abandon(self, task_id, *, confirmation_token):
        """Delete one confirmed task, retaining shared knowledge and other tasks."""
        with self._lock():
            plan = self._abandon_plan(task_id)
            if not confirmation_token:
                raise ValueError("Task deletion requires confirmation")
            if confirmation_token != plan["confirmation_token"]:
                raise ValueError("Task changed since confirmation; inspect and confirm it again")
            state = copy.deepcopy(self._state(task_id, allow_abandoning=True))
            state.update(
                stage="abandoning",
                next="end",
                cleanup_revision=plan["revision"],
                prompt={
                    "summary": f"Deletion is unfinished. Run abandon {task_id} again to finish.",
                    "actions": [],
                },
            )
            try:
                self.graph.update_state(self._config(task_id), state, as_node="finish")
                remove_resources(self.repository, plan)
                self.graph.checkpointer.delete_thread(task_id)
            except sqlite3.Error as error:
                raise RuntimeError(
                    f"Could not finish deleting task data; run abandon {task_id} again: {error}"
                ) from error
            return {"task_id": task_id, "stage": "abandoned"}

    def resume(self, task_id, *, retry_backend=False):
        """Continue interrupted execution without answering a human gate."""
        with self._lock():
            state = self._state(task_id)
            snapshot = self.graph.get_state(self._config(task_id))
            if (
                retry_backend
                and state["stage"] == "backend_blocked"
                and "retry" in state["prompt"]["actions"]
                and any(task.interrupts for task in snapshot.tasks)
            ):
                self.graph.invoke(Command(resume={"action": "retry"}), self._config(task_id))
                state = self._state(task_id)
            elif snapshot.next and not any(task.interrupts for task in snapshot.tasks):
                self.graph.invoke(None, self._config(task_id))
                state = self._state(task_id)
            return self._view(state)

    def run_phase(self, task_id, phase, *, automatic=False):
        """Start one eligible phase from a saved boundary, never bypass a pending gate."""
        if not isinstance(automatic, bool):
            raise ValueError("Automatic must be enabled or disabled")
        with self._lock():
            state = copy.deepcopy(self._state(task_id))
            config = self._config(task_id)
            if self.graph.get_state(config).next:
                raise ValueError("The current phase is pending; use resume and answer its gate")
            available = self._available_phases(state)
            if phase not in available:
                raise ValueError(
                    f"Phase {phase!r} is not available; "
                    f"choose from: {', '.join(available) or 'none'}"
                )
            expected = state.get("phase_fingerprint", state.get("gate_fingerprint"))
            if self._workspace(state).fingerprint() != expected:
                raise ValueError(
                    "Checkout changed since the completed phase; restore the saved revision "
                    "and files, or start a new task for the changed checkout"
                )
            state["mode"] = "full" if automatic else "phased"
            state["selected_phase"] = phase
            if phase == "interview":
                state.pop("specification", None)
            state["next"] = "worktree" if phase == "tests" else PHASE_NODES[phase]
            if state["stage"] == "specification_ready" and phase in {"implement", "refactor"}:
                state["next"] = "existing_tests"
            state["prompt"], state["attempts"], state["patch_count"] = {}, 0, 0
            self.graph.invoke(state, config)
            return self._view(self._state(task_id))

    def can_use_context(self, task_id):
        checkpoint = self.graph.get_state(self._config(task_id))
        if checkpoint.values.get("task_kind") == "agent_builder":
            return False
        if checkpoint.values.get("stage") in {"ready", "abandoning"}:
            return False
        return not checkpoint.next or (
            checkpoint.values.get("stage") == "chat" and bool(checkpoint.interrupts)
        )

    def use_context(self, task_id, snapshot_id=None):
        """Select memory between phases or between general chat messages."""
        with self._lock():
            config = self._config(task_id)
            state = copy.deepcopy(self._state(task_id))
            if state["stage"] == "ready":
                raise ValueError("This task is finished; start a new task to use different context")
            if not self.can_use_context(task_id):
                raise ValueError("The current phase is pending; finish it before changing context")
            snapshot = self.context.get(snapshot_id)
            state["project_context_id"] = snapshot["id"]
            self.graph.update_state(config, state)
            save_json(Path(state["report_directory"]) / "current-review.json", self._view(state))
            return self._view(self._state(task_id))

    def respond(self, task_id, action, **fields):
        if "automatic" in fields and not isinstance(fields["automatic"], bool):
            raise ValueError("Automatic must be enabled or disabled")
        with self._lock():
            state = self._state(task_id)
            actions = recovery_actions(state)
            if self._can_choose_phase(state):
                actions.append("choose_phase")
            if self._can_discuss_implementation(state):
                actions.extend(["question", "repair"])
            if state["stage"] == "test_change_review":
                actions.extend(
                    [
                        "continue_implementation",
                        "approve_changes",
                        "revise_changes",
                        "deny_changes",
                        "revise",
                        "reject",
                    ]
                )
            if action not in actions:
                raise ValueError(f"Action {action!r} is not available at {state.get('stage')}")
            if action == "choose_phase":
                workspace = self._workspace(state)
                if workspace.revision != state["base"] or git(
                    workspace.path, "status", "--porcelain"
                ):
                    raise ValueError(
                        "Worktree changed; restore a clean task base before changing phases"
                    )
            if action in {
                "approve",
                "accept_red",
                "save_agent",
                "continue_handoff",
                "continue_implementation",
                "approve_changes",
                "revise_changes",
                "deny_changes",
                "continue_without_commits",
                "run_checks",
                "revalidate_changes",
            }:
                if fields.get("approval_token") != state["prompt"].get("approval_token"):
                    raise ValueError("Stale or missing approval token")
                if self._workspace(state).fingerprint() != state["gate_fingerprint"]:
                    raise ValueError(
                        "Worktree changed since review; approval is stale. Recheck first."
                    )
            if action in {
                "answer",
                "question",
                "revise",
                "revise_changes",
                "dismiss",
                "accept_red",
            }:
                if not fields.get("text", "").strip():
                    raise ValueError(f"{action} requires text")
            if action in {"fix", "dismiss"}:
                ids = fields.get("finding_ids", [])
                if not ids or not set(ids) <= {f["id"] for f in state["findings"]}:
                    raise ValueError("Select existing finding IDs")
            self.graph.invoke(Command(resume={"action": action, **fields}), self._config(task_id))
            return self._view(self._state(task_id))

    def close(self):
        self.connection.close()
        self.context.close()
        self.agents.close()

    def review_action(self, task_id, action, *, token, role=None, finding_id=None, cancel=None):
        """Run a requested review tool without invoking a model or advancing the review."""
        with self._lock():
            state = copy.deepcopy(self._state(task_id))
            tools = self._review_actions(state)
            if not tools or tools["token"] != token:
                raise ValueError("The review changed; refresh it before requesting this action")
            self._assert_review_revision(state)
            if action == "check_links":
                result = check_documentation_links(
                    state["worktree"],
                    state["review_source"],
                    state["report_directory"],
                    cancel=cancel,
                    progress=self.progress,
                )
            elif action == "post_review_comment":
                result = self._post_finding(state, role, finding_id, cancel)
            else:
                raise ValueError("Unknown review action")
            receipt = {
                "stage": "review_action",
                "action": action,
                "review_token": tools["token"],
                **result,
            }
            if receipt not in state["messages"]:
                state["messages"].append(receipt)
                self.graph.update_state(self._config(task_id), state)
                if state["stage"] == "standalone_review":
                    # Re-enter the same interrupt after recording the tool result.
                    # No review or model node runs; exports still see a stable gate.
                    self.graph.invoke(None, self._config(task_id))
            reports = Path(state["report_directory"])
            (reports / "review.md").write_text(standalone_review_report(state))
            save_json(reports / "current-review.json", self._view(state))
            return self._view(state)

    def _post_finding(self, state, role, finding_id, cancel):
        finding = next(
            (
                item
                for item in state["standalone_reviews"].get(role, {}).get("findings", [])
                if item["id"] == finding_id
            ),
            None,
        )
        if finding is None:
            raise ValueError("Select an existing finding from this review")
        return post_review_comment(
            self.repository,
            state["review_source"],
            role,
            finding,
            cancel=cancel,
            progress=self.progress,
        )

    def _review_actions(self, state):
        if state.get("task_kind") != "review" or state["stage"] not in {
            "standalone_review",
            "review_complete",
        }:
            return None
        token = digest([state["review_source"], state["standalone_reviews"]])
        return {
            "token": token,
            "github": bool(state["review_source"].get("pull_request")),
            "posts": [
                m
                for m in state["messages"]
                if m.get("action") == "post_review_comment" and m.get("review_token") == token
            ],
        }

    def _workspace(self, state):
        return Workspace(state.get("worktree", self.repository))

    def _view(self, state, *, inspect_worktree=True):
        view = {
            key: state[key]
            for key in (
                "task_id",
                "stage",
                "mode",
                "commit_policy",
                "check_plan",
                "prompt",
                "worktree",
                "base",
                "base_ref",
                "restarted_from",
                "report_directory",
                "project_context_id",
                "task_kind",
                "test_expectation",
                "test_strategy",
                "validation_knowledge",
                "review_source",
                "agent_profiles",
                "saved_agent",
                "handoff_origin",
            )
            if key in state
        }
        if state.get("agent_origin"):
            view["agent_id"] = state["agent_origin"]["id"]
        if state["stage"] == "abandoning":
            view["revision"] = state.get("cleanup_revision")
        elif inspect_worktree:
            view["revision"] = self._workspace(state).revision
        else:
            # A browser poll reads checkpoints while another thread may remove a worktree.
            commits = state.get("commits", [])
            view["revision"] = (
                commits[-1]["revision"]
                if commits
                else state.get("starting_revision", state["base"])
            )
        if state["stage"] == "specification_ready":
            view["prompt"] = {**view["prompt"], "available_phases": self._available_phases(state)}
        if state["stage"] == "test_change_review":
            proposal = state["proposed_test_change"]
            view["prompt"] = {
                **view["prompt"],
                "summary": "Permission requested",
                "explanation": (
                    "Review the proposed patch below. Approval applies this patch and runs "
                    "validation. Deny or request changes to send it back to the coding agent. "
                    "Ask a question can also revise this proposal when you describe an edit. "
                    "Permission applies only to this patch, not future changes."
                ),
                "permission_reason": proposal.get("permission_request") or proposal["summary"],
                "permission_files": [
                    {"path": c["path"], "operation": "delete" if c["content"] is None else "write"}
                    for c in proposal["changes"]
                ],
                "diff": state["prompt"].get("diff")
                if "diff" in state["prompt"]
                else self._workspace(state).proposed_diff(proposal["changes"]),
                "actions": ["approve_changes", "question", "revise_changes", "deny_changes"],
            }
        if self._can_discuss_implementation(state):
            view["prompt"] = {
                **self._validation_feedback(state),
                **view["prompt"],
                "summary": "Implementation needs attention",
                "actions": list(dict.fromkeys(["question", "repair", *view["prompt"]["actions"]])),
            }
        if self._can_choose_phase(state):
            view["prompt"] = {
                **view["prompt"],
                "actions": list(dict.fromkeys([*view["prompt"]["actions"], "choose_phase"])),
            }
        if can_recover(state):
            recovery = recovery_prompt(state)
            view["prompt"] = {**recovery, **view["prompt"]}
            for key in ("guidance", "suggested_commands"):
                if key in recovery:
                    view["prompt"][key] = recovery[key]
            view["prompt"]["actions"] = list(
                dict.fromkeys([*view["prompt"]["actions"], *recovery_actions(state)])
            )
        if state.get("task_kind") == "tests":
            view["test_scope"] = {
                key: state["specification"][key] for key in ("packages", "acceptance_criteria")
            }
        if actions := self._review_actions(state):
            view["review_actions"] = actions
        view["can_run_checks"] = state.get("next") == "end" and state.get("task_kind") not in {
            "review",
            "agent_builder",
            "checks",
        }
        view["checks_in_task"] = bool(state.get("checks_origin"))
        if state.get("check_results"):
            view["check_results"] = {
                **state["check_results"],
                "commands": [
                    {
                        **command,
                        "stdout": command.get("stdout", "")[-6000:],
                        "stderr": command.get("stderr", "")[-6000:],
                    }
                    for command in state["check_results"]["commands"]
                ],
            }
        view["outcome"] = self._outcome(state, view["prompt"])
        return view

    @staticmethod
    def _outcome(state, prompt):
        """Report saved workflow evidence without asking a model to invent a status."""
        stage = state["stage"]
        if stage in {"checks_complete", "checks_failed"}:
            result = state["check_results"]
            return {
                "kind": "success"
                if result["status"] == "passed"
                else "waiting"
                if result["status"] == "partial"
                else "error",
                "title": {
                    "passed": "Checks passed",
                    "partial": "Checks passed with omissions",
                    "stopped": "Checks stopped",
                    "failed": "Checks failed",
                }[result["status"]],
                "detail": result["summary"],
                "guidance": prompt.get("guidance", ""),
            }
        errors = {"backend_blocked", "agent_error", "validation_blocked"}
        completed = {"ready", "review_complete", "tests_complete", "agent_ready"}
        if stage == "validation_blocked" and state.get("validation", {}).get("status") == "stopped":
            kind, title = "paused", "Checks stopped"
            detail = "Validation was interrupted. The changes are saved but are not yet validated."
            guidance = "Run checks again or give the agent new instructions."
        elif stage in errors:
            kind, title = "error", "Stopped: action needed"
            detail = prompt.get("error") or prompt.get("summary") or "The last step failed."
            if stage == "validation_blocked":
                validation = state.get("validation", {})
                failed = [
                    name
                    for name, result in validation.get("commands", {}).items()
                    if result.get("exit_code") != 0
                ]
                detail = "Validation failed" + (": " + ", ".join(failed) if failed else ".")
                if validation.get("evidence_error"):
                    detail += "\n" + validation["evidence_error"]
                if validation.get("skipped_commands"):
                    detail += "\nNot run: " + ", ".join(validation["skipped_commands"])
            guidance = (
                prompt.get("guidance", "") + " Choose a recovery action below. "
                "Nothing will retry automatically."
            ).strip()
        elif stage == "agent_paused":
            kind, title = "paused", "Stopped by request"
            detail = prompt.get("summary", "The operation was stopped.")
            guidance = "Choose Retry or send new instructions when you want to continue."
        elif stage in completed or state.get("next") == "end":
            kind = "success"
            title = "Task complete" if stage in completed else "Step complete"
            detail = prompt.get("summary", "The selected step completed.")
            if state.get("commit_policy") == "manual" and "uncommitted" not in detail:
                detail += " No commit was created; changes remain uncommitted in the task worktree."
            guidance = (
                "Waiting for your choice in one-step mode. Choose the next step below, "
                "or select automatic continuation before starting it."
                if prompt.get("available_phases")
                else "No further steps are running."
            )
        else:
            kind, title = "waiting", "Your input is needed"
            detail = prompt.get("summary", stage.replace("_", " ").capitalize())
            guidance = "Choose an action below to continue."
        return {"kind": kind, "title": title, "detail": detail, "guidance": guidance}

    @staticmethod
    def _can_choose_phase(state):
        if (
            state.get("task_kind", "feature") != "feature"
            or state.get("commits")
            or state.get("selected_findings")
            or state.get("agent_integrity_fingerprint")
        ):
            return False
        return (
            state["stage"] == "test_scope_review"
            or state["stage"] == "validation_setup"
            or state.get("checking_baseline", False)
            and state["stage"]
            in {"validation_blocked", "agent_error", "backend_blocked", "agent_paused"}
            or state["stage"] in {"backend_blocked", "agent_error", "agent_paused"}
            and (
                state.get("retry_node") == "tests"
                or state.get("retry_node") == "discover_validation"
                and state.get("validation_following") == "tests"
            )
        )

    def _available_phases(self, state):
        if state.get("task_kind") in {"tests", "review", "chat", "agent_builder", "checks"}:
            return []
        following = {
            "specification_ready": ["explore", "interview", "tests", "implement", "refactor"],
            "exploration_ready": ["explore", "interview"],
            "tests_requested": ["tests"],
            "tests_ready": ["implement"],
            "implementation_ready": ["refactor"],
            "refactor_requested": ["refactor"],
            "walkthrough_ready": ["handoff"],
        }
        if state["stage"] in {"refactor_ready", "reviews_ready"}:
            missing = [
                phase
                for phase, role in (
                    ("review-haskell", "haskell_reviewer"),
                    ("review-security", "security_reviewer"),
                )
                if state.get(role, {}).get("revision") != state["review_revision"]
            ]
            if missing:
                return missing
            return ["walkthrough"] if state["walkthrough_enabled"] else ["walkthrough", "handoff"]
        return following.get(state["stage"], [])

    def _phase_complete(self, state, stage, summary):
        state["stage"], state["next"] = stage, "end"
        state["phase_fingerprint"] = self._workspace(state).fingerprint()
        state["prompt"] = {
            "summary": summary,
            "actions": [],
            "available_phases": self._available_phases(state),
        }
        reports = Path(state["report_directory"])
        if "specification" in state:
            save_json(reports / "specification.json", state["specification"])
        if stage == "exploration_ready":
            state["prompt"].update(
                exploration=state["exploration"], report=str(reports / "exploration.md")
            )
        if stage == "review_complete":
            state["prompt"].update(
                reviews=state["standalone_reviews"],
                report=str(reports / "review.md"),
            )
        if stage == "tests_complete":
            state["prompt"].update(validation=state["validation"], coverage=state["coverage"])
        save_json(reports / "patches.json", state["commits"])
        save_json(reports / "dispositions.json", state["dispositions"])
        save_json(reports / "walkthrough.json", state.get("walkthrough_answers", []))
        save_json(reports / "current-review.json", self._view(state))

    def _node(self, name):
        def run(original):
            state = copy.deepcopy(original)
            journal = Path(state["directory"]) / "steps" / f"{state['seq']:04d}-{name}.json"
            if journal.exists():
                return json.loads(journal.read_text())
            state["messages"] = conversation_messages(state)
            activity = (
                "keeping validated changes uncommitted"
                if name == "commit" and state.get("commit_policy") == "manual"
                else name.replace("_", " ")
            )
            self.progress(f"{state['task_id']}: {activity}")
            try:
                getattr(self, "_" + name)(state)
            except (ValueError, RuntimeError, OSError) as error:
                if name == "reply":
                    state["failed_reply"] = {
                        key: copy.deepcopy(original[key]) for key in ("response", "stage", "prompt")
                    }
                state["retry_node"] = name
                actions = (
                    ["retry", "recheck"]
                    if state.get("validation")
                    and not state.get("agent_integrity_fingerprint")
                    and not state.get("checks_origin")
                    and state.get("task_kind") != "checks"
                    else ["retry"]
                )
                if isinstance(error, AgentInterrupted):
                    self._gate(
                        state,
                        "agent_paused",
                        {
                            "summary": "Model request stopped. Your conversation is saved.",
                            "guidance": "Revise your instructions, retry this request, or quit.",
                            "draft": state["interrupted_draft"],
                        },
                        ["revise", "retry"],
                    )
                elif isinstance(error, BackendError):
                    self._gate(state, "backend_blocked", error.prompt(), ["retry"])
                else:
                    self._gate(state, "agent_error", {"error": str(error)}, actions)
            state["seq"] += 1
            save_json(journal, state)
            return state

        return run

    def _gate(self, state, stage, prompt, actions):
        state["stage"], state["next"] = stage, "pause"
        state["gate_fingerprint"] = self._workspace(state).fingerprint()
        token = digest([state["task_id"], state["seq"], stage, state["gate_fingerprint"], prompt])
        state["prompt"] = {**prompt, "actions": actions, "approval_token": token}
        save_json(Path(state["report_directory"]) / "current-review.json", self._view(state))

    def _pause(self, original):
        state = copy.deepcopy(original)
        state["response"] = interrupt(self._view(state))
        state["next"] = "reply"
        return state

    def _context(self, state, role, **extra):
        messages = messages_for_agent(state, role, explain=extra.get("intent") == "explain")
        if state.get("task_kind") == "agent_builder":
            return {
                "request": state["request"],
                "messages": messages,
                "definition": state.get("draft_definition"),
                "roles": ROLES,
                "coding_toolchains": ["haskell"],
                "avatars": AVATARS,
                **extra,
            }
        workspace = self._workspace(state)
        context = {
            "request": state["request"],
            "task_kind": state.get("task_kind", "feature"),
            "test_expectation": state.get("test_expectation", "red"),
            "test_strategy": state.get("test_strategy", "new"),
            "review_source": state.get("review_source"),
            "specification": state.get("specification"),
            "messages": messages,
            "revision": workspace.revision,
            "base_revision": state["base"],
            "approved_tests": state.get("approved_tests", {}),
            "validation_config": state.get("validation_config", self.validation.model_dump()),
            "validation_knowledge": state.get("validation_knowledge"),
            "patches": [
                {
                    "revision": patch["revision"],
                    "role": patch["role"],
                    **({"summary": patch["summary"]} if patch["role"] == role else {}),
                }
                for patch in state["commits"]
            ],
        }
        if role in CODING_ROLES:
            context["selected_findings"] = state.get("selected_findings", [])
        if state.get("task_kind") == "review":
            context.update(
                review_diff(workspace, state["review_source"], state["report_directory"])
            )
        else:
            context["diff"] = workspace.diff(state["base"])
        if state.get("project_context_id"):
            context["project_context"] = self.context.for_agent(
                state["project_context_id"], workspace
            )
        if state.get("validation"):
            context["validation"] = state["validation"]
        if role == "explorer" and state.get("exploration"):
            context["exploration"] = state["exploration"]
        return {**context, **extra}

    def _ask(self, state, role, **extra):
        context = self._context(state, role, **extra)
        if profile := state.get("agent_profiles", {}).get(role):
            context["agent_profile"] = profile
        workspace = self._workspace(state)
        before = workspace.fingerprint()
        if state.get("agent_integrity_fingerprint"):
            self._check_agent_checkout(state, workspace, state["agent_integrity_fingerprint"])
            del state["agent_integrity_fingerprint"]
        cache = Path(state["directory"]) / "proposals" / f"{state['seq']:04d}-{role}.json"
        if cache.exists():
            result = json.loads(cache.read_text())
        else:
            for attempt in range(2):
                try:
                    result = self.agent(role, context, workspace.path)
                    try:
                        result = validate_response(role, context, result, notify=self.progress)
                    except ValidationError as error:
                        raise BackendResponseError(
                            str(error), json.dumps(result, ensure_ascii=False, default=str), ""
                        ) from error
                except (AgentInterrupted, KeyboardInterrupt) as error:
                    self._check_agent_checkout(state, workspace, before)
                    latest = next(
                        (
                            message["text"]
                            for message in reversed(context["messages"])
                            if message.get("text")
                        ),
                        state["request"],
                    )
                    state["interrupted_draft"] = (
                        extra.get("question") or extra.get("developer_answer") or latest
                    )
                    raise AgentInterrupted() from error
                except BackendError as error:
                    self._save_backend_failure(state, role, attempt, error)
                    self._check_agent_checkout(state, workspace, before)
                    # Repair only response formatting, once, within a bounded prompt.
                    if (
                        attempt == 0
                        and isinstance(error, BackendResponseError)
                        and len(error.stdout.encode()) <= 64_000
                    ):
                        correction = {
                            **context,
                            "response_repair": {
                                "validation_error": str(error)[:4000],
                                "previous_response": error.stdout,
                            },
                        }
                        if len(json.dumps(correction, ensure_ascii=False).encode()) <= 400_000:
                            context = correction
                            self.progress(
                                f"{state['task_id']}: invalid {role} response; "
                                "requesting one correction"
                            )
                            continue
                    raise
                except (ValueError, RuntimeError, OSError):
                    self._check_agent_checkout(state, workspace, before)
                    raise
                self._check_agent_checkout(state, workspace, before)
                break
            save_json(cache, result)
        return result

    @staticmethod
    def _check_agent_checkout(state, workspace, before):
        if workspace.fingerprint() != before:
            state["agent_integrity_fingerprint"] = before
            raise ValueError(
                "Agent changed the checkout directly; inspect and restore it before retrying"
            )

    @staticmethod
    def _save_backend_failure(state, role, attempt, error):
        directory = Path(state["directory"]) / "backend-responses" / f"{state['seq']:04d}-{role}"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory if attempt == 0 else directory / f"attempt-{attempt + 1}"
        destination.mkdir(exist_ok=True, mode=0o700)
        for filename, text in (("stdout.txt", error.stdout), ("stderr.txt", error.stderr)):
            path = destination / filename
            path.write_text(text)
            path.chmod(0o600)
        metadata = destination / "failure.json"
        save_json(metadata, {"backend": error.backend, "kind": error.kind, "error": str(error)})
        metadata.chmod(0o600)
        error.diagnostics = str(directory)

    def _interview(self, state):
        extra = (
            {"intent": "implementation_interview"}
            if state.get("selected_phase") == "interview" and state.get("exploration")
            else {}
        )
        result = self._ask(state, "interviewer", **extra)
        state["specification"] = result["specification"]
        questions = result["questions"] or result["specification"]["open_questions"]
        state["messages"].append(
            interview_message(result["specification"], questions, state["seq"])
        )
        if questions:
            self._gate(state, "interview", {"questions": questions}, ["answer"])
        else:
            self._gate(
                state,
                "specification_review",
                {"specification": state["specification"]},
                ["approve", "question", "revise"],
            )

    def _agent_builder(self, state):
        result = self._ask(state, "agent_builder")
        state["draft_definition"] = result["definition"]
        state["messages"].append({"role": "agent_builder", "answer": result["answer"]})
        actions = ["answer", "save_agent"] if result["definition"] else ["answer"]
        self._gate(state, "agent_builder", {"definition": result["definition"]}, actions)

    def _chat(self, state):
        # Keep the response and write ID stable until this user message is checkpointed.
        user_index = next(
            (
                i
                for i in range(len(state["messages"]) - 1, -1, -1)
                if state["messages"][i].get("text")
            ),
            -1,
        )
        turn_path = Path(state["directory"]) / "chat-turns" / f"{user_index}.json"
        if turn_path.exists():
            turn = json.loads(turn_path.read_text())
        else:
            latest = state["messages"][user_index]["text"] if user_index >= 0 else state["request"]
            turn = {
                "id": uuid4().hex,
                "reply": self._ask(state, "chat", latest_user_message=latest),
            }
            if generation := getattr(self.agent, "last_generation", None):
                turn["generation"] = copy.deepcopy(generation)
            save_json(turn_path, turn)
        result = copy.deepcopy(turn["reply"])
        note = result.pop("memory_note", None)
        if note is not None:
            try:
                saved = self.context.append_note(self.repository, note=note, operation=turn["id"])
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                result["answer"] = (
                    "The note was not saved to project memory. See the save result below."
                )
                result["memory_save"] = {"status": "error", "note": note, "error": str(error)}
            else:
                state["project_context_id"] = saved["id"]
                result["memory_save"] = {"status": "saved", "note": note, "snapshot": saved["id"]}
        message = {"stage": "chat", "role": "chat", "chat_turn": turn["id"], **result}
        if turn.get("generation"):
            message["generation"] = turn["generation"]
        for index, previous in enumerate(state["messages"]):
            if previous.get("chat_turn") == turn["id"]:
                state["messages"][index] = message
                break
        else:
            state["messages"].append(message)
        reports = Path(state["report_directory"])
        save_json(
            reports / "chat.json", {"request": state["request"], "messages": state["messages"]}
        )
        (reports / "chat.md").write_text(chat_report(state))
        references = list(
            dict.fromkeys(
                reference
                for message in state["messages"]
                if message.get("role") == "chat"
                for reference in message["references"]
            )
        )
        self._gate(state, "chat", {"references": references}, ["answer"])

    def _explore(self, state):
        result = self._ask(state, "explorer", intent="explore")
        state["exploration"] = {**result, "revision": self._workspace(state).revision}
        state["messages"].append({"stage": "exploration", "role": "explorer", **result})
        reports = Path(state["report_directory"])
        save_json(reports / "exploration.json", state["exploration"])
        (reports / "exploration.md").write_text(exploration_report(state))
        self._gate(
            state,
            "exploration",
            {"exploration": state["exploration"], "report": str(reports / "exploration.md")},
            ["question", "done"],
        )

    def _create_worktree(self, state):
        path = Path(state["directory"]) / "worktree"
        branch = task_branch(state["task_id"], state["directory"])
        revision = state.get("starting_revision", state["base"])
        if not path.exists():
            git(self.repository, "worktree", "add", "-b", branch, str(path), revision)
        elif (
            Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve()
            or git(path, "branch", "--show-current") != branch
            or git(path, "rev-parse", "HEAD") != revision
            or git(path, "status", "--porcelain")
        ):
            raise ValueError("Existing task worktree differs from its saved base; inspect it first")
        state["worktree"], state["branch"] = str(path), branch

    def _assert_review_revision(self, state):
        workspace = self._workspace(state)
        if workspace.revision != state["review_source"]["head"] or git(
            workspace.path, "status", "--porcelain"
        ):
            raise ValueError("Review worktree changed; restore the pinned revision before retrying")

    def _standalone_review(self, state):
        self._assert_review_revision(state)
        roles = REVIEWERS[state["reviewers"]]
        reviews = {role: self._ask(state, role) for role in roles}
        state["standalone_reviews"] = reviews
        state["messages"].append({"stage": "standalone_review", "reviews": reviews})
        reports = Path(state["report_directory"])
        save_json(reports / "review.json", {"source": state["review_source"], "reviews": reviews})
        (reports / "review.md").write_text(standalone_review_report(state))
        self._gate(
            state,
            "standalone_review",
            {"summary": "Review ready", "reviews": reviews, "report": str(reports / "review.md")},
            ["question", "revise", "done"],
        )

    def _prepare(self, state):
        self._create_worktree(state)
        state["next"] = state.get("initial_phase", "interview")

    def _worktree(self, state):
        if "worktree" not in state:
            self._create_worktree(state)
        workspace = self._workspace(state)
        for package in state["specification"]["packages"]:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", package) or not any(
                Path(p).name == package + ".cabal" for p in workspace.files()
            ):
                raise ValueError(f"Unknown Cabal package: {package}")
        state["next"] = "tests"

    def _existing_tests(self, state):
        self._worktree(state)
        workspace = self._workspace(state)
        state["test_strategy"], state["checking_baseline"] = "existing", True
        state["test_paths"], state["expected_failures"] = [], []
        state["approved_tests"] = workspace.test_manifest()
        state["role"] = "implementer" if state["selected_phase"] == "implement" else "refactorer"
        state["phase"] = "green"
        state["proposal"] = {
            "summary": "Check existing coverage before changing code",
            "changes": [],
            "commit_message": "No commit: baseline checks",
        }
        state["next"] = "validate"

    def _tests(self, state):
        unexpected = [
            p
            for p in self._workspace(state).changed_paths()
            if not is_test_path(p) and not p.endswith(".cabal")
        ]
        if unexpected:
            raise ValueError(
                "Uncommitted production edits cannot enter a test-first patch. "
                "Save or restore them before revising tests: " + ", ".join(unexpected)
            )
        if not self._ensure_validation_knowledge(state, "tests"):
            return
        proposal = self._ask(state, "test_author")
        if not proposal["changes"]:
            state["messages"].append({"role": "test_author", "answer": proposal["summary"]})
            self._gate(
                state, "test_scope_review", {"summary": proposal["summary"]}, ["question", "revise"]
            )
            return
        criteria = {c["id"] for c in state["specification"]["acceptance_criteria"]}
        if not all(proposal["coverage"].get(key) for key in criteria):
            raise ValueError("Tests must map every acceptance criterion to named test cases")
        self._workspace(state).apply(proposal["changes"], tests_only=True)
        state["test_strategy"] = "new"
        state["test_paths"] = sorted(
            set(state.get("test_paths", [])) | {c["path"] for c in proposal["changes"]}
        )
        state["expected_failures"], state["coverage"] = (
            proposal["expected_failures"],
            proposal["coverage"],
        )
        state["proposal"], state["role"], state["phase"] = (
            proposal,
            "test_author",
            state.get("test_expectation", "red"),
        )
        state["next"] = "validate"

    def _assert_tests(self, state):
        if (
            "approved_tests" in state
            and self._workspace(state).test_manifest(state["test_paths"]) != state["approved_tests"]
        ):
            raise ValueError(
                "Approved tests changed outside the test review gate; "
                "restore them or request revised tests"
            )

    def _code(self, state, role):
        self._assert_tests(state)
        if not self._ensure_validation_knowledge(
            state, "implement" if role == "implementer" else "refactor"
        ):
            return
        proposal = self._ask(state, role)
        state["role"] = role
        self._accept_code(state, proposal)

    def _accept_code(self, state, proposal):
        self._assert_tests(state)
        blocked = self._test_changes(state, proposal)
        if blocked or proposal.get("permission_request"):
            state["proposed_test_change"] = proposal
            self._gate(
                state,
                "test_change_review",
                {
                    "proposal": proposal,
                    "blocked_paths": blocked,
                    "diff": self._workspace(state).proposed_diff(proposal["changes"]),
                },
                ["approve_changes", "question", "revise_changes", "deny_changes"],
            )
            return
        self._apply_code(state, proposal)

    def _apply_code(self, state, proposal, *, approved=False):
        workspace = self._workspace(state)
        workspace.apply(
            proposal["changes"],
            approved_paths=[c["path"] for c in proposal["changes"]] if approved else (),
        )
        if approved:
            state["approved_tests"] = workspace.test_manifest(state.get("test_paths", []))
        self._assert_tests(state)
        state["proposal"] = proposal
        state["messages"].append(
            {
                "kind": "work_update",
                "agent_role": state["role"],
                "answer": "Applied patch: " + proposal["summary"] + "\n\nRunning validation now.",
            }
        )
        state["phase"] = (
            "red"
            if state["role"] == "implementer"
            and not approved
            and not proposal.get("complete", True)
            and state.get("test_strategy") != "existing"
            else "green"
        )
        state["attempts"] += 1
        state["next"] = "validate"

    def _test_changes(self, state, proposal):
        workspace = self._workspace(state)
        blocked = []
        for change in proposal["changes"]:
            name = change["path"]
            if (
                is_test_path(name)
                or name in state["approved_tests"]
                or protected_configuration(name)
            ):
                blocked.append(name)
            elif name.endswith(".cabal"):
                path = workspace.safe_path(name)
                before = path.read_text() if path.is_file() else None
                if not cabal_library_dependencies_only(before, change["content"]):
                    blocked.append(name)
        return blocked

    def _implement(self, state):
        self._code(state, "implementer")

    def _refactor(self, state):
        self._code(state, "refactorer")

    def _ensure_validation_knowledge(self, state, following):
        detailed = following == "tests" or state.get("test_strategy") != "existing"
        if knowledge_is_current(
            self._workspace(state),
            state.get("validation_knowledge"),
            state["specification"]["packages"],
            detailed,
            validation_environment(self.execute),
        ):
            return True
        state["validation_following"] = following
        state["validation_detailed"] = detailed
        state["next"] = "discover_validation"
        return False

    def _discover_validation(self, state):
        workspace = self._workspace(state)
        overrides = saved_overrides(state, self.validation)
        discovery = self._ask(
            state,
            "validation_discovery",
            inventory=project_inventory(workspace),
            validation_overrides=overrides,
            # Do not present old implicit defaults as a discovered project convention.
            validation_config=overrides,
            detailed_test_evidence=state["validation_detailed"],
            validation_environment=validation_environment(self.execute),
        )
        knowledge = prepare_knowledge(
            workspace,
            discovery,
            state["specification"]["packages"],
            overrides,
            detailed=state["validation_detailed"],
            environment=validation_environment(self.execute),
        )
        state["validation_overrides"] = overrides
        state["validation_knowledge"] = knowledge
        state["messages"].append(
            {
                "role": "validation_discovery",
                "answer": knowledge["summary"],
                "questions": knowledge["questions"],
            }
        )
        save_json(Path(state["report_directory"]) / "validation-setup.json", knowledge)
        if not knowledge["ready"]:
            self._gate(
                state,
                "validation_setup",
                {
                    "questions": knowledge["questions"],
                    "guidance": "Project inspection is complete. "
                    "No validation commands have run for this setup.",
                },
                ["answer", "retry"],
            )
            state["retry_node"] = "discover_validation"
            return
        state["validation_config"] = knowledge["checks"]
        state["next"] = state["validation_following"]

    @staticmethod
    def _can_discuss_implementation(state):
        return (
            state["stage"] == "validation_blocked"
            and state.get("role") in {"implementer", "refactorer"}
            and not state.get("checking_baseline")
        )

    def _validation_feedback(self, state, *, retrying=False):
        validation = state.get("validation", {})
        commands = validation.get("commands", {})
        failures = [name for name, result in commands.items() if result["exit_code"] != 0]
        details = [
            {
                "check": name,
                "output": (
                    commands[name].get("stderr_tail") or commands[name].get("stdout_tail") or ""
                )[-2000:],
            }
            for name in failures
        ]
        if validation.get("status") == "stopped":
            answer = "Validation stopped. Your code changes are saved."
        elif validation.get("status") == "green":
            answer = "Validation passed: " + ", ".join(commands) + "."
            return {"answer": answer, "failure_details": []}
        elif validation.get("status") in {"expected_red", "needs_red_review"}:
            return {
                "answer": "Expected test failures confirmed."
                + (
                    " Nix checks failed and need your review before proceeding."
                    if validation["status"] == "needs_red_review"
                    else " Continuing implementation."
                ),
                "failure_details": details,
            }
        else:
            answer = "Validation failed: " + (", ".join(failures) or "test evidence") + "."
            if validation.get("evidence_error"):
                answer += "\n" + validation["evidence_error"]
            if skipped := validation.get("skipped_commands"):
                answer += "\nNot run: " + ", ".join(skipped) + "."
        if retrying:
            answer += "\n\nThe coding agent is continuing with these failures to repair the patch."
        else:
            if (
                validation.get("status") != "stopped"
                and state.get("attempts", 0) >= self.max_repair_rounds
            ):
                answer += f"\nAutomatic repairs paused after {state['attempts']} attempts."
            answer += (
                "\n\nYour changes are saved. Ask what failed or describe the next fix in the "
                "message box to continue implementation. No task restart is needed. "
                "Run checks again only rechecks the current files."
            )
        return {"answer": answer, "failure_details": details}

    def _validate(self, state):
        if not self._ensure_validation_knowledge(state, "validate"):
            return
        workspace = self._workspace(state)
        if git(workspace.path, "ls-files", "--others", "--exclude-standard"):
            raise ValueError(
                "Untracked files are outside the review diff; inspect and git add them first"
            )
        configuration = ValidationConfig.model_validate(
            state.setdefault("validation_config", self.validation.model_dump())
        )
        commands = configuration.commands(state["specification"]["packages"])
        state.pop("validated_fingerprint", None)
        results = {}
        for name, command in commands:
            self.progress(f"{state['task_id']}: {' '.join(command)}")
            options = (
                {"report_glob": configuration.report_pattern(name.removeprefix("test:"))}
                if name.startswith("test:")
                else {}
            )
            stopped = False
            try:
                results[name] = self.execute(command, workspace.path, **options)
            except ValidationInterrupted as error:
                results[name] = error.result
                stopped = True
            save_json(
                Path(state["directory"])
                / "logs"
                / f"{state['seq']:04d}-{name.replace(':', '-')}.json",
                results[name],
            )
            if stopped:
                validation = {
                    "status": "stopped",
                    "phase": state["phase"],
                    "role": state["role"],
                    "revision": workspace.revision,
                    "log_directory": str(Path(state["directory"]) / "logs"),
                    "stopped_command": name,
                }
                state["validation"] = validation
                state["validations"].append(validation)
                save_json(Path(state["report_directory"]) / "validation.json", state["validations"])
                self._gate(
                    state,
                    "validation_blocked",
                    {
                        "summary": "Checks stopped. Your task and changes are saved.",
                        "guidance": "Use Recheck to run validation again when you are ready.",
                        "validation": validation,
                    },
                    ["recheck"],
                )
                return
            if name == "lint":
                lint_fingerprint = workspace.fingerprint()
            self.progress(
                f"{state['task_id']}: {name} "
                + ("passed" if results[name]["exit_code"] == 0 else "failed")
            )
            # Gather lint and compiler diagnostics before sending a broken patch back.
            # Nix and test runs cannot make a patch with these failures acceptable.
            if name == "build" and any(results[k]["exit_code"] != 0 for k in ("lint", "build")):
                self.progress(f"{state['task_id']}: skipping Nix and tests until lint/build passes")
                break
        if workspace.fingerprint() != lint_fingerprint:
            raise ValueError("Files changed while checks ran; inspect the worktree and recheck")
        if state["role"] != "test_author":
            self._assert_tests(state)
        elif any(
            not is_test_path(p) and not p.endswith(".cabal") for p in workspace.changed_paths()
        ):
            raise ValueError(
                "Production files changed during test validation; inspect and restore them"
            )
        allow_cabal = configuration.junit_glob is None and not state["validation_detailed"]
        status = classify_validation(
            results,
            state.get("expected_failures", []),
            phase=state["phase"],
            allow_cabal=allow_cabal,
        )
        state["validated_fingerprint"] = workspace.fingerprint()
        validation = {
            "status": status,
            "phase": state["phase"],
            "role": state["role"],
            "revision": workspace.revision,
            "fingerprint": state["validated_fingerprint"],
            "log_directory": str(Path(state["directory"]) / "logs"),
            "commands": {
                k: {
                    "argv": v["argv"],
                    "exit_code": v["exit_code"],
                    "stdout_tail": v["stdout"][-8000:],
                    "stderr_tail": v["stderr"][-8000:],
                }
                for k, v in results.items()
            },
            "skipped_commands": [name for name, _ in commands if name not in results],
        }
        try:
            validation["test_evidence"] = test_evidence(results, allow_cabal=allow_cabal)
            if state.get("task_kind") == "tests":
                covered = {name for names in state["coverage"].values() for name in names}
                missing = covered - set(validation["test_evidence"]["identities"])
                if missing:
                    status = validation["status"] = "blocked"
                    validation["evidence_error"] = (
                        "Tests named in coverage did not execute: " + ", ".join(sorted(missing))
                    )
        except ValueError as error:
            validation["evidence_error"] = str(error)
        state["validation"] = validation
        state["validations"].append(validation)
        save_json(Path(state["report_directory"]) / "validation.json", state["validations"])
        if state["role"] in {"implementer", "refactorer"} and not state.get("checking_baseline"):
            state["messages"].append(
                {
                    "kind": "work_update",
                    "agent_role": state["role"],
                    **self._validation_feedback(
                        state,
                        retrying=status == "blocked"
                        and not self.pause_on_error
                        and state["attempts"] < self.max_repair_rounds,
                    ),
                }
            )
        if status == "needs_red_review":
            self._gate(
                state,
                "red_failure_review",
                {
                    "validation": validation,
                    "explanation": "Inspect the Nix logs. Accept only if its failures come "
                    "solely from the expected red tests; record the evidence.",
                },
                ["accept_red", "recheck"],
            )
        elif status == "blocked":
            if state.get("checking_baseline"):
                self._gate(state, "validation_blocked", {"validation": validation}, ["recheck"])
            elif (
                state["role"] in {"implementer", "refactorer"}
                and not self.pause_on_error
                and state["attempts"] < self.max_repair_rounds
            ):
                state["next"] = "implement" if state["role"] == "implementer" else "refactor"
            else:
                self._gate(
                    state, "validation_blocked", {"validation": validation}, ["recheck", "revise"]
                )
        else:
            self._validated(state)

    def _validated(self, state):
        if boundary := state.pop("checks_resume_boundary", None):
            if boundary not in {"tests_requested", "refactor_requested"}:
                raise ValueError("Invalid phase after check-runner revalidation")
            self._phase_complete(
                state, boundary, "Formatting revalidated. Continue the requested step."
            )
        elif state.pop("checking_baseline", False):
            state["next"] = PHASE_NODES[state["selected_phase"]]
        elif state["role"] == "test_author":
            self._gate(
                state,
                "test_review",
                {
                    "summary": state["proposal"]["summary"],
                    "diff": self._workspace(state).diff(),
                    "coverage": state["coverage"],
                    "validation": state["validation"],
                },
                ["approve", "question", "revise", "recheck"],
            )
        else:
            state["next"] = "commit"

    def _commit(self, state):
        workspace = self._workspace(state)
        if state.get("commit_policy") == "manual":
            if workspace.fingerprint() != state["validated_fingerprint"]:
                raise ValueError(
                    "Worktree changed since validation; rerun checks before continuing"
                )
            state.setdefault("uncommitted_patches", []).append(
                {
                    "summary": state["proposal"]["summary"],
                    "role": state["role"],
                    "fingerprint": workspace.fingerprint(),
                    "validation": state["validation"],
                }
            )
            self._after_commit(state, workspace.revision)
            outcome = self._outcome(state, state.get("prompt", {}))
            message = (
                outcome["detail"] + "\n\n" + outcome["guidance"]
                if state["next"] == "end"
                else "No commit was created. Keeping validated changes in the task worktree "
                "and continuing to the next step."
            )
            state["messages"].append(
                {"kind": "work_update", "agent_role": state["role"], "answer": message}
            )
            self.progress(f"{state['task_id']}: {message}")
            return
        revision = workspace.commit(
            state["proposal"]["commit_message"],
            expected=state["validated_fingerprint"],
            step=f"{state['task_id']}/{state['seq']}",
            unsigned=self.unsigned_commits,
        )
        if revision != state["base"] and not any(
            c["revision"] == revision for c in state["commits"]
        ):
            state["commits"].append(
                {
                    "revision": revision,
                    "role": state["role"],
                    "summary": state["proposal"]["summary"],
                    "validation": state["validation"],
                }
            )
        self._after_commit(state, revision)

    def _continue_without_commits(self, state):
        if not commit_failed(state):
            raise ValueError("Continuing without commits is only available at a failed commit")
        if self._workspace(state).fingerprint() != state["validated_fingerprint"]:
            raise ValueError("Worktree changed since validation; rerun checks before continuing")
        state["commit_policy"] = "manual"
        state["messages"].append(
            {
                "kind": "work_update",
                "agent_role": state["role"],
                "answer": "Continuing without commits for this task. Changes stay in the task "
                "worktree; validation, permissions, and reviews still apply. Existing commits "
                "are preserved. You can commit the remaining changes manually later.",
            }
        )
        state["next"] = "commit"

    def _after_commit(self, state, revision):
        workspace = self._workspace(state)
        saved = "left uncommitted" if state.get("commit_policy") == "manual" else "committed"
        if state["role"] == "test_author":
            state["approved_tests"] = workspace.test_manifest(state["test_paths"])
            state["attempts"], state["patch_count"], state["next"] = 0, 0, "implement"
            if state.get("task_kind") == "tests":
                self._phase_complete(
                    state, "tests_complete", f"Approved tests {saved}. Test task complete."
                )
            elif state["mode"] == "phased":
                self._phase_complete(
                    state, "tests_ready", f"Approved tests {saved}. Tests phase complete."
                )
        elif state["role"] == "implementer":
            if state["proposal"].get("complete", True):
                state["attempts"], state["patch_count"], state["next"] = 0, 0, "refactor"
                if state["mode"] == "phased":
                    self._phase_complete(
                        state, "implementation_ready", "Implementation complete and validated."
                    )
            else:
                self._more_patches(state, "implement")
        elif not state["proposal"].get("complete", True):
            self._more_patches(state, "refactor")
        else:
            state["review_revision"] = revision
            state["review_fingerprint"] = workspace.fingerprint()
            state["review_round"] += 1
            state.pop("haskell_reviewer", None)
            state.pop("security_reviewer", None)
            state["next"] = "review_haskell"
            if state["mode"] == "phased":
                self._phase_complete(state, "refactor_ready", "Refactoring complete and validated.")

    def _more_patches(self, state, next_node):
        state["patch_count"] = state.get("patch_count", 0) + 1
        state["attempts"] = 0
        if state["patch_count"] >= 12:
            state["retry_node"] = next_node
            self._gate(
                state,
                "agent_error",
                {"error": "Twelve patches reached; inspect progress before continuing"},
                ["retry"],
            )
        else:
            state["next"] = next_node

    def _review(self, state, role, next_node):
        workspace = self._workspace(state)
        if not self._review_matches(state, workspace):
            raise ValueError("Review revision changed; revalidate before reviewing")
        result = self._ask(state, role)
        result["revision"] = workspace.revision
        result["fingerprint"] = workspace.fingerprint()
        state[role] = result
        filename = "haskell-review.json" if role == "haskell_reviewer" else "security-review.json"
        save_json(Path(state["report_directory"]) / filename, result)
        save_json(
            Path(state["report_directory"]) / f"round-{state['review_round']}-{filename}", result
        )
        state["next"] = next_node

    @staticmethod
    def _review_matches(state, workspace):
        if state.get("review_fingerprint"):
            return workspace.fingerprint() == state["review_fingerprint"]
        return workspace.revision == state["review_revision"] and not git(
            workspace.path, "status", "--porcelain"
        )

    def _review_haskell(self, state):
        self._review(state, "haskell_reviewer", "review_gate")

    def _review_security(self, state):
        self._review(state, "security_reviewer", "review_gate")

    def _review_gate(self, state):
        if state["mode"] == "full":
            for role, node in (
                ("haskell_reviewer", "review_haskell"),
                ("security_reviewer", "review_security"),
            ):
                if state.get(role, {}).get("revision") != state["review_revision"]:
                    state["next"] = node
                    return
        findings = [
            finding
            for role in ("haskell_reviewer", "security_reviewer")
            for finding in state.get(role, {}).get("findings", [])
        ]
        if len({f["id"] for f in findings}) != len(findings):
            raise ValueError("Reviewer finding IDs must be unique")
        dismissed = {
            d["id"]
            for d in state["dispositions"]
            if d["revision"] == state["review_revision"]
            and (
                not state.get("review_fingerprint")
                or d.get("fingerprint") == state["review_fingerprint"]
            )
        }
        state["findings"] = [f for f in findings if f["id"] not in dismissed]
        if state["findings"]:
            self._gate(
                state,
                "review_findings",
                {"findings": state["findings"]},
                ["question", "fix", "dismiss"],
            )
        else:
            state["next"] = "walkthrough" if state["walkthrough_enabled"] else "finish"
            if state["mode"] == "phased":
                self._phase_complete(
                    state, "reviews_ready", "Selected review complete. Findings resolved."
                )

    def _walkthrough(self, state):
        state["questions"] = self._ask(state, "walkthrough")["questions"]
        state["question_index"], state["walkthrough_answers"] = 0, []
        self._gate(state, "walkthrough", state["questions"][0], ["answer", "skip"])

    def _finish(self, state):
        reports = Path(state["report_directory"])
        reports.mkdir(parents=True, exist_ok=True)
        save_json(reports / "specification.json", state["specification"])
        if state["stage"] == "specification_review" or "worktree" not in state:
            self._phase_complete(
                state, "specification_ready", "Specification approved. Interview complete."
            )
            return
        else:
            workspace = self._workspace(state)
            if not self._review_matches(state, workspace):
                raise ValueError(
                    "Reviewed worktree changed before handoff; revalidate and review again"
                )
            state["stage"] = "ready"
            save_json(reports / "patches.json", state["commits"])
            save_json(reports / "dispositions.json", state["dispositions"])
            save_json(reports / "walkthrough.json", state.get("walkthrough_answers", []))
            (reports / "pr-description.md").write_text(pr_description(state))
        state["prompt"] = {"summary": "Local work is ready for developer inspection", "actions": []}
        if state.get("commit_policy") == "manual":
            state["prompt"]["summary"] += ". Changes remain uncommitted in the task worktree."
        state["phase_fingerprint"] = self._workspace(state).fingerprint()
        state["next"] = "end"

    def _reply(self, state):
        response = state.pop("response")
        action, stage = response["action"], state["stage"]
        if "automatic" in response:
            state["mode"] = "full" if response["automatic"] else "phased"
        if stage.startswith("checks_"):
            self._checks_reply(state, action, response.get("text", ""))
        elif action == "continue_without_commits":
            self._continue_without_commits(state)
        elif action == "choose_phase":
            for key in (
                "checking_baseline",
                "approved_tests",
                "test_paths",
                "test_strategy",
                "expected_failures",
                "proposal",
                "role",
                "phase",
                "retry_node",
            ):
                state.pop(key, None)
            state["mode"] = "phased"
            self._phase_complete(
                state, "specification_ready", "Choose the next step for the approved scope."
            )
        elif stage == "handoff_review" and action == "continue_handoff":
            state.update(state.pop("handoff_resume"))
            if state["next"] == "pause":
                self._gate(state, state["stage"], state["prompt"], state["prompt"]["actions"])
            else:
                save_json(
                    Path(state["report_directory"]) / "current-review.json", self._view(state)
                )
        elif stage == "validation_setup" and action == "answer":
            state["messages"].append({"stage": stage, "text": response["text"]})
            state["next"] = "discover_validation"
        elif stage == "agent_paused" and action == "revise":
            if state["retry_node"] == "reply":
                state.update(state.pop("failed_reply"))
                state["response"]["text"] = response["text"]
            else:
                state["messages"].append(
                    {
                        "stage": stage,
                        "action": "revise",
                        "text": response["text"],
                        "agent_role": NODE_ROLES.get(state["retry_node"], state.get("role")),
                        "replaces_interrupted_instructions": state["interrupted_draft"],
                    }
                )
            state["next"] = state["retry_node"]
        elif stage == "agent_builder" and action == "answer":
            state["messages"].append({"stage": stage, "action": action, "text": response["text"]})
            state["next"] = "agent_builder"
        elif stage == "agent_builder" and action == "save_agent":
            origin = state["agent_origin"]
            if origin["repository"] != self.agents.catalog:
                raise ValueError(
                    "The agent repository changed. Reopen this agent from the Agents page."
                )
            saved = self.agents.save(
                origin["id"], state["draft_definition"], expected_version=origin["version"]
            )
            state["saved_agent"] = saved
            state["messages"].append(
                {
                    "role": "agent_builder",
                    "answer": (
                        f"Saved {saved['definition']['name']} to {saved['path']}. "
                        "Review the Git diff, then commit and push to share it. "
                        "Assign it to a project when you want to use it."
                    ),
                    "saved_agent": saved,
                }
            )
            self._phase_complete(
                state, "agent_ready", "Agent saved to the repository. Commit and push to share it."
            )
        elif stage == "chat" and action == "answer":
            state["messages"].append({"stage": stage, "action": action, "text": response["text"]})
            state["next"] = "chat"
        elif stage == "standalone_review":
            self._assert_review_revision(state)
            if action == "done":
                self._phase_complete(
                    state, "review_complete", "Review saved. No code changes made."
                )
            elif action == "question":
                answers = []
                answers_by_role = {}
                for role in state["standalone_reviews"]:
                    answer = self._ask(
                        state,
                        role,
                        intent="explain",
                        question=response["text"],
                        review_packet=state["standalone_reviews"][role],
                    )
                    answers_by_role[role] = answer
                    answers.append(f"{role.replace('_', ' ').capitalize()}: {answer['answer']}")
                answer = "\n\n".join(answers)
                state["messages"].append(
                    {
                        "question": response["text"],
                        "answer": answer,
                        "answers_by_role": answers_by_role,
                    }
                )
                self._gate(
                    state,
                    stage,
                    {**state["prompt"], "answer": answer},
                    ["question", "revise", "done"],
                )
            else:
                state["messages"].append(
                    {"stage": stage, "action": action, "text": response["text"]}
                )
                state["next"] = "standalone_review"
        elif stage == "exploration" and action == "done":
            self._phase_complete(
                state, "exploration_ready", "Exploration saved. Start any further phase explicitly."
            )
            if state["mode"] == "full":
                state["next"] = "interview"
        elif stage == "exploration" and action == "question":
            state["messages"].append({"stage": stage, "action": action, "text": response["text"]})
            state["next"] = "explore"
        elif action == "question" and self._can_discuss_implementation(state):
            answer = self._ask(
                state,
                state["role"],
                intent="discuss_implementation",
                question=response["text"],
                applied_proposal=state.get("proposal"),
            )
            state["messages"].append(
                {
                    "agent_role": state["role"],
                    "question": response["text"],
                    "answer": answer["answer"],
                    "suggested_commands": answer.get("suggested_commands", []),
                }
            )
            if proposal := answer.get("proposal"):
                state["attempts"] = 0
                self._accept_code(state, proposal)
            else:
                self._gate(
                    state,
                    stage,
                    {
                        **state["prompt"],
                        "answer": answer["answer"],
                        "suggested_commands": answer.get("suggested_commands", []),
                    },
                    ["question", *[a for a in state["prompt"]["actions"] if a != "question"]],
                )
        elif action == "question" and can_recover(state):
            role = NODE_ROLES.get(state.get("retry_node"), state.get("role", "interviewer"))
            if role == "reviewers":
                role = "haskell_reviewer"
            answer = self._ask(
                state,
                role,
                intent="recover_operation",
                question=response["text"],
                failed_operation=state.get("retry_node", "validate"),
                error_packet=recovery_prompt(state),
                recovery_actions=[
                    a
                    for a in recovery_actions(state)
                    if a in {"retry", "recheck", "continue_without_commits"}
                ],
            )
            chosen = answer.get("next_action")
            if chosen and chosen not in recovery_actions(state):
                raise ValueError("The suggested recovery action is unavailable at this step")
            state["messages"].append(
                {
                    "agent_role": role,
                    "question": response["text"],
                    **answer,
                }
            )
            if chosen == "continue_without_commits":
                self._continue_without_commits(state)
            elif chosen in {"retry", "recheck"}:
                state["messages"].append({"agent_role": role, "text": response["text"]})
                if chosen == "retry" and state["retry_node"] == "reply":
                    state.update(state.pop("failed_reply"))
                state["next"] = "validate" if chosen == "recheck" else state["retry_node"]
            else:
                self._gate(state, stage, {**state["prompt"], **answer}, state["prompt"]["actions"])
        elif action == "question":
            role = {
                "specification_review": "interviewer",
                "test_review": "test_author",
                "test_scope_review": "test_author",
                "test_change_review": state.get("role", "implementer"),
                "review_findings": "security_reviewer"
                if "SEC-" in response["text"]
                else "haskell_reviewer",
            }[stage]
            answer = self._ask(
                state,
                role,
                intent="discuss_patch" if stage == "test_change_review" else "explain",
                question=response["text"],
                **(
                    {"current_proposal": state["proposed_test_change"]}
                    if stage == "test_change_review"
                    else {
                        "review_packet": state[role]
                        if stage == "review_findings"
                        else state["prompt"]
                    }
                ),
            )
            proposal = answer.get("proposal") if stage == "test_change_review" else None
            revision = None
            if proposal is not None:
                # Revision changes the pending proposal, so approval must target the new diff.
                diff = self._workspace(state).proposed_diff(proposal["changes"])
                blocked = self._test_changes(state, proposal)
                previous = {c["path"]: c for c in state["proposed_test_change"]["changes"]}
                revised = {c["path"]: c for c in proposal["changes"]}
                revision = {
                    "changed_files": [
                        path
                        for path in sorted(previous.keys() | revised.keys())
                        if previous.get(path) != revised.get(path)
                    ]
                }
                state["proposed_test_change"] = proposal
                prompt = {
                    "proposal": proposal,
                    "blocked_paths": blocked,
                    "diff": diff,
                    "answer": answer["answer"],
                    "patch_revision": revision,
                }
            else:
                prompt = {**state["prompt"], "answer": answer["answer"]}
            state["messages"].append(
                {
                    "agent_role": role,
                    "question": response["text"],
                    "answer": answer["answer"],
                    **(
                        {"proposal_digest": digest(proposal), "patch_revision": revision}
                        if proposal is not None
                        else {}
                    ),
                }
            )
            self._gate(state, stage, prompt, state["prompt"]["actions"])
        elif action == "approve":
            state["next"] = (
                ("worktree" if state["mode"] == "full" else "finish")
                if stage == "specification_review"
                else "commit"
            )
        elif action == "accept_red":
            state["validation"]["red_exception"] = response["text"]
            state["validations"][-1] = copy.deepcopy(state["validation"])
            save_json(Path(state["report_directory"]) / "validation.json", state["validations"])
            state["validated_fingerprint"] = self._workspace(state).fingerprint()
            self._validated(state)
        elif action == "repair":
            state["attempts"] = 0
            state["next"] = "implement" if state["role"] == "implementer" else "refactor"
        elif action == "recheck":
            state["next"] = "validate"
        elif action == "retry":
            if state["retry_node"] == "reply":
                state.update(state.pop("failed_reply"))
            state["next"] = state["retry_node"]
        elif action == "approve_changes":
            proposal = state["proposed_test_change"]
            self._assert_tests(state)
            state["messages"].append(
                {
                    "agent_role": state["role"],
                    "action": action,
                    "text": "The developer approved this exact patch. Apply it and validate it.",
                    "proposal_digest": digest(proposal),
                    "approved_paths": [c["path"] for c in proposal["changes"]],
                }
            )
            self._apply_code(state, proposal, approved=True)
        elif action in {"revise_changes", "deny_changes"}:
            state["messages"].append(
                {
                    "agent_role": state["role"],
                    "action": action,
                    "rejected_test_change": state["proposed_test_change"],
                    "text": (
                        "The developer requests a revised patch. "
                        if action == "revise_changes"
                        else "The developer denied this patch. Continue without those changes. "
                    )
                    + response.get("text", ""),
                }
            )
            state["attempts"] = 0
            state["next"] = "implement" if state["role"] == "implementer" else "refactor"
        elif action == "continue_implementation":
            state["messages"].append(
                {
                    "agent_role": state["role"],
                    "action": action,
                    "rejected_test_change": state["proposed_test_change"],
                    "text": (
                        "Continue implementation now. Revise the blocked proposal to implement "
                        "the approved scope while preserving all existing tests and their Cabal "
                        "configuration. Main-library build-depends edits are allowed. Return a "
                        "code patch for the harness to apply and validate, not an explanation. "
                        "Previous passing checks cover the baseline, not this proposed patch."
                    )
                    + ("\n" + response["text"] if response.get("text") else ""),
                }
            )
            state["attempts"] = 0
            state["next"] = "implement" if state["role"] == "implementer" else "refactor"
        elif action == "reject":
            state["messages"].append(
                {
                    "agent_role": state["role"],
                    "rejected_test_change": state["proposed_test_change"],
                }
            )
            state["attempts"] = 0
            state["next"] = "implement" if state["role"] == "implementer" else "refactor"
        elif stage == "walkthrough":
            if action == "skip":
                state["next"] = "finish"
            else:
                question = state["questions"][state["question_index"]]
                feedback = self._ask(
                    state,
                    "walkthrough_feedback",
                    question=question,
                    developer_answer=response["text"],
                )
                state["walkthrough_answers"].append(
                    {**question, "answer": response["text"], **feedback}
                )
                state["question_index"] += 1
                if state["question_index"] == len(state["questions"]):
                    state["next"] = "finish"
                else:
                    self._gate(
                        state,
                        "walkthrough",
                        {
                            **state["questions"][state["question_index"]],
                            "feedback": feedback["feedback"],
                        },
                        ["answer", "skip"],
                    )
            if state["next"] == "finish" and state["mode"] == "phased":
                self._phase_complete(
                    state, "walkthrough_ready", "Walkthrough finished. Handoff can be prepared."
                )
        elif action in {"fix", "dismiss"}:
            selected = [f for f in state["findings"] if f["id"] in response["finding_ids"]]
            if action == "dismiss":
                state["dispositions"] += [
                    {
                        "id": f["id"],
                        "reason": response["text"],
                        "revision": state["review_revision"],
                        "fingerprint": state.get("review_fingerprint"),
                    }
                    for f in selected
                ]
                state["next"] = "review_gate"
            else:
                state["selected_findings"], state["attempts"] = selected, 0
                state["next"] = "tests" if any(f["needs_test"] for f in selected) else "refactor"
                if state["mode"] == "phased":
                    next_stage = (
                        "tests_requested" if state["next"] == "tests" else "refactor_requested"
                    )
                    self._phase_complete(
                        state, next_stage, "Fix selected. Start the indicated phase to address it."
                    )
        else:
            state["attempts"] = 0
            if stage in {"interview", "specification_review"}:
                state["next"] = "interview"
            elif (
                stage in {"test_review", "test_change_review", "test_scope_review"}
                or state.get("role") == "test_author"
            ):
                state["next"] = "tests"
            else:
                state["next"] = "implement" if state["role"] == "implementer" else "refactor"
            state["messages"].append(
                {
                    "stage": stage,
                    "action": action,
                    "text": response.get("text", ""),
                    "agent_role": NODE_ROLES[state["next"]],
                }
            )
            if stage == "test_change_review" and state["mode"] == "phased":
                self._phase_complete(
                    state,
                    "tests_requested",
                    "Test revision requested. Start the tests phase to discuss it.",
                )
