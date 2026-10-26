"""Portable JSON handoffs. Rebuild checkpoints locally; never load a foreign database."""

import base64
import copy
import hashlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, JsonValue

from .agent_library import assignment_problem
from .conversation import conversation_messages
from .process import run_process
from .schemas import REVIEWERS, AgentDefinition, CheckPlan, Exploration, Record, Specification
from .workspace import Workspace, digest, git, task_branch, validate_task_name

MAX_HANDOFF_BYTES = 32 * 1024 * 1024
MAX_TASKS = 30
GATES = {
    "checks_plan": {"question", "revise", "run_checks"},
    "checks_complete": {"question", "revise", "run_checks"},
    "checks_failed": {"question", "revise", "run_checks"},
    "interview": {"answer"},
    "specification_review": {"approve", "question", "revise"},
    "chat": {"answer"},
    "exploration": {"question", "done"},
    "standalone_review": {"question", "revise", "done"},
    "test_review": {"approve", "question", "revise", "recheck"},
    "test_scope_review": {"question", "revise", "choose_phase"},
    "test_change_review": {"question", "revise", "reject"},
    "red_failure_review": {"accept_red", "recheck"},
    "validation_blocked": {"recheck", "revise", "choose_phase"},
    "validation_setup": {"answer", "retry", "choose_phase"},
    "review_findings": {"question", "fix", "dismiss"},
    "walkthrough": {"answer", "skip"},
    "agent_paused": {"revise", "retry"},
    "agent_error": {"retry", "recheck"},
    "backend_blocked": {"retry"},
}
BOUNDARIES = {
    "specification_ready",
    "exploration_ready",
    "tests_requested",
    "tests_ready",
    "implementation_ready",
    "refactor_requested",
    "refactor_ready",
    "reviews_ready",
    "walkthrough_ready",
    "ready",
    "tests_complete",
    "review_complete",
}
RETRY_NODES = {
    "plan_checks",
    "run_checks",
    "discover_validation",
    "chat",
    "explore",
    "standalone_review",
    "interview",
    "worktree",
    "existing_tests",
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
}
LOCAL_FIELDS = {
    "directory",
    "report_directory",
    "worktree",
    "branch",
    "validation_config",
    "validation_overrides",
    "response",
    "handoff_origin",
    "handoff_resume",
}
STATE_FIELDS = {
    "check_plan",
    "check_results",
    "check_history",
    "checks_resume_boundary",
    "seq",
    "task_id",
    "request",
    "mode",
    "initial_phase",
    "walkthrough_enabled",
    "base",
    "base_ref",
    "stage",
    "prompt",
    "next",
    "messages",
    "validations",
    "commits",
    "commit_policy",
    "uncommitted_patches",
    "review_fingerprint",
    "review_round",
    "dispositions",
    "attempts",
    "project_context_id",
    "agent_profiles",
    "task_kind",
    "test_expectation",
    "test_strategy",
    "checking_baseline",
    "review_source",
    "reviewers",
    "starting_revision",
    "standalone_start",
    "restarted_from",
    "phase_fingerprint",
    "gate_fingerprint",
    "specification",
    "exploration",
    "standalone_reviews",
    "validation",
    "validation_knowledge",
    "validation_following",
    "validation_detailed",
    "coverage",
    "failed_reply",
    "retry_node",
    "interrupted_draft",
    "agent_integrity_fingerprint",
    "test_paths",
    "expected_failures",
    "proposal",
    "role",
    "phase",
    "approved_tests",
    "proposed_test_change",
    "patch_count",
    "validated_fingerprint",
    "review_revision",
    "findings",
    "selected_findings",
    "haskell_reviewer",
    "security_reviewer",
    "questions",
    "question_index",
    "walkthrough_answers",
    "selected_phase",
}


class LooseFile(Record):
    path: str
    data: str
    executable: bool


class TaskPacket(Record):
    state: dict[str, JsonValue]
    head: str
    fingerprint: str
    bundle: str | None
    patch: str
    added: list[str]
    untracked: list[LooseFile]
    reports: dict[str, str]


class Handoff(Record):
    format: Literal["lambda-harness-handoff"]
    version: Literal[1]
    created: str
    project: str
    current_memory: str | None
    memory: list[dict[str, JsonValue]]
    tasks: list[TaskPacket] = Field(max_length=MAX_TASKS)


def _path(name):
    if not isinstance(name, str):
        raise ValueError("Handoff file paths must be text")
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or ".." in path.parts
        or any(part.lower() == ".git" for part in path.parts)
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError(f"Invalid handoff file path: {name}")
    return path


def _oid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
        raise ValueError("Handoff revisions must be full Git commit IDs")


def _decode(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid handoff file encoding") from error


def _replace(value, replacements):
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def _validate_state(state):
    if set(state) - STATE_FIELDS:
        raise ValueError("This handoff contains unsupported workflow fields")
    for name in ("task_id", "request", "base", "base_ref", "stage", "next", "initial_phase"):
        if not isinstance(state.get(name), str):
            raise ValueError(f"Missing or invalid workflow field: {name}")
    validate_task_name(state["task_id"])
    for name in ("messages", "validations", "commits", "dispositions"):
        if not isinstance(state.get(name), list) or any(
            not isinstance(item, dict) for item in state[name]
        ):
            raise ValueError(f"Invalid workflow list: {name}")
    for name in ("attempts", "review_round", "seq"):
        if type(state.get(name)) is not int or not 0 <= state[name] <= 1_000_000:
            raise ValueError(f"Invalid workflow count: {name}")
    for name in (
        "agent_profiles",
        "validation_knowledge",
        "specification",
        "exploration",
        "standalone_reviews",
        "validation",
        "coverage",
        "approved_tests",
        "proposal",
        "proposed_test_change",
        "haskell_reviewer",
        "security_reviewer",
        "review_source",
        "standalone_start",
    ):
        if name in state and not isinstance(state[name], dict):
            raise ValueError(f"Invalid workflow record: {name}")
    for name in ("test_paths", "expected_failures"):
        if name in state and (
            not isinstance(state[name], list)
            or not all(isinstance(value, str) for value in state[name])
        ):
            raise ValueError(f"Invalid workflow file or test list: {name}")
    for name in ("findings", "selected_findings", "questions", "walkthrough_answers"):
        if name in state and (
            not isinstance(state[name], list)
            or not all(isinstance(value, dict) for value in state[name])
        ):
            raise ValueError(f"Invalid workflow list: {name}")
    for name in ("patch_count", "question_index"):
        if name in state and (type(state[name]) is not int or state[name] < 0):
            raise ValueError(f"Invalid workflow count: {name}")
    if type(state.get("walkthrough_enabled")) is not bool:
        raise ValueError("Invalid walkthrough preference")
    if state.get("role") not in {None, "test_author", "implementer", "refactorer"}:
        raise ValueError("Invalid coding role")
    if state.get("phase") not in {None, "red", "green"}:
        raise ValueError("Invalid validation phase")
    if state.get("test_strategy") not in {None, "new", "existing"}:
        raise ValueError("Invalid test strategy")
    if state.get("checks_resume_boundary") not in {None, "tests_requested", "refactor_requested"}:
        raise ValueError("Invalid phase after check-runner revalidation")
    if state.get("commit_policy") not in {None, "manual"}:
        raise ValueError("Invalid commit policy")
    if "uncommitted_patches" in state and (
        not isinstance(state["uncommitted_patches"], list)
        or not all(isinstance(patch, dict) for patch in state["uncommitted_patches"])
    ):
        raise ValueError("Invalid uncommitted patches")
    if "checking_baseline" in state and type(state["checking_baseline"]) is not bool:
        raise ValueError("Invalid baseline check state")
    if state.get("validation_following") not in {
        None,
        "tests",
        "implement",
        "refactor",
        "validate",
    }:
        raise ValueError("Invalid phase after validation discovery")
    if "validation_detailed" in state and type(state["validation_detailed"]) is not bool:
        raise ValueError("Invalid validation evidence requirement")
    for name in (
        "gate_fingerprint",
        "phase_fingerprint",
        "validated_fingerprint",
        "agent_integrity_fingerprint",
        "review_fingerprint",
    ):
        if name in state and (
            not isinstance(state[name], str) or not re.fullmatch(r"[a-f0-9]{64}", state[name])
        ):
            raise ValueError("Invalid saved worktree fingerprint")
    stage = state["stage"]
    prompt = state.get("prompt")
    if not isinstance(prompt, dict) or not isinstance(prompt.get("actions"), list):
        raise ValueError("Invalid saved review prompt")
    actions = prompt["actions"]
    if not all(isinstance(action, str) for action in actions):
        raise ValueError("Invalid saved review actions")
    if stage in GATES:
        if state["next"] != "pause" or not set(actions) <= GATES[stage]:
            raise ValueError("Handoff must contain a paused review gate")
    elif stage not in BOUNDARIES or state["next"] != "end" or actions:
        raise ValueError("Handoff must contain a completed phase or a paused review gate")
    if state.get("task_kind") not in {None, "chat", "tests", "review", "checks"}:
        raise ValueError("Share agent definitions through their Git repository")
    if state["initial_phase"] not in {
        "interview",
        "chat",
        "explore",
        "worktree",
        "standalone_review",
        "plan_checks",
    }:
        raise ValueError("Unsupported task phase")
    if state.get("retry_node") is not None and state["retry_node"] not in RETRY_NODES:
        raise ValueError("Unsupported recovery step")
    for key in ("base", "starting_revision", "review_revision"):
        if state.get(key) is not None:
            _oid(state[key])
    for name in state.get("test_paths", []):
        _path(name)
    for name in state.get("approved_tests", {}):
        _path(name)
    if state.get("check_plan"):
        CheckPlan.model_validate(state["check_plan"])
    if "specification" in state:
        Specification.model_validate(state["specification"])
        if any(
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", package)
            for package in state["specification"]["packages"]
        ):
            raise ValueError("Invalid Cabal package in handoff")
    if "exploration" in state:
        Exploration.model_validate(
            {key: value for key, value in state["exploration"].items() if key != "revision"}
        )
        _oid(state["exploration"]["revision"])
    if stage in {"specification_review", "specification_ready", "interview"} and not state.get(
        "specification"
    ):
        raise ValueError("Missing saved specification")
    if stage in {"test_review", "red_failure_review", "validation_blocked"} and not all(
        state.get(key) for key in ("specification", "role", "phase", "proposal", "validation")
    ):
        raise ValueError("Missing saved test or validation details")
    if state.get("task_kind") == "tests" and not state.get("specification"):
        raise ValueError("Missing standalone test scope")
    if state.get("task_kind") == "review":
        for name in ("head", "merge_base", "base"):
            _oid(state["review_source"][name])
        if state.get("reviewers") not in REVIEWERS:
            raise ValueError("Invalid standalone reviewers")
    for profile in state.get("agent_profiles", {}).values():
        definition = AgentDefinition.model_validate(profile["definition"]).model_dump()
        if problem := assignment_problem(definition):
            raise ValueError(problem)
    failed = state.get("failed_reply")
    if failed is not None:
        if not isinstance(failed, dict) or set(failed) != {"response", "stage", "prompt"}:
            raise ValueError("Invalid saved reply")
        if failed["stage"] not in GATES or not isinstance(failed["response"], dict):
            raise ValueError("Invalid saved reply gate")
        if failed["response"].get("action") not in GATES[failed["stage"]]:
            raise ValueError("Invalid saved reply action")
        if not isinstance(failed["prompt"], dict):
            raise ValueError("Invalid saved reply prompt")


def _validated(archive):
    try:
        if len(json.dumps(archive, indent=2).encode()) > MAX_HANDOFF_BYTES:
            raise ValueError("Handoff files must be at most 32 MB")
        data = Handoff.model_validate(archive).model_dump()
        ids = set()
        for snapshot in data["memory"]:
            payload = {k: v for k, v in snapshot.items() if k not in {"id", "created"}}
            if snapshot.get("id") != digest(payload) or snapshot["id"] in ids:
                raise ValueError("Invalid or duplicate project memory version")
            _oid(snapshot["revision"])
            if snapshot.get("version") not in {1, 2, 3} or not isinstance(
                snapshot["documents"], list
            ):
                raise ValueError("Unsupported project memory format")
            if not isinstance(snapshot["created"], str) or not isinstance(
                snapshot.get("notes", ""), str
            ):
                raise ValueError("Invalid project memory")
            if not isinstance(snapshot["missing_sources"], list) or not all(
                isinstance(path, str) for path in snapshot["missing_sources"]
            ):
                raise ValueError("Invalid missing memory sources")
            for document in snapshot["documents"]:
                _path(document["path"])
                if hashlib.sha256(document["content"].encode()).hexdigest() != document["sha256"]:
                    raise ValueError("Project memory reference does not match its hash")
                if (
                    not isinstance(document["purpose"], str)
                    or not isinstance(document["symbols"], list)
                    or not all(isinstance(symbol, str) for symbol in document["symbols"])
                ):
                    raise ValueError("Invalid project memory reference")
            ids.add(snapshot["id"])
        if data["current_memory"] is not None and data["current_memory"] not in ids:
            raise ValueError("Missing current project memory")
        tasks = set()
        for packet in data["tasks"]:
            state = packet["state"]
            _validate_state(state)
            if state["task_id"] in tasks:
                raise ValueError("Duplicate task in handoff")
            tasks.add(state["task_id"])
            if (
                state.get("project_context_id") is not None
                and state["project_context_id"] not in ids
            ):
                raise ValueError("Missing task memory version")
            _oid(packet["head"])
            if not re.fullmatch(r"[a-f0-9]{64}", packet["fingerprint"]):
                raise ValueError("Invalid worktree fingerprint")
            expected = (
                state.get("gate_fingerprint")
                if state["next"] == "pause"
                else state.get("phase_fingerprint")
            )
            if expected is not None and expected != packet["fingerprint"]:
                raise ValueError("Worktree fingerprint differs from its saved review")
            if packet["bundle"] is not None:
                _decode(packet["bundle"])
            names = set()
            for file in packet["untracked"]:
                _path(file["path"])
                if file["path"] in names:
                    raise ValueError("Duplicate file in handoff")
                names.add(file["path"])
                _decode(file["data"])
            for name in [*packet["added"], *packet["reports"]]:
                _path(name)
        return data
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("Invalid or incomplete handoff file") from error


def export_handoff(harness, task_ids):
    """Capture only stable checkpoints, under the same lock as workflow execution."""
    if not isinstance(task_ids, list) or not all(isinstance(item, str) for item in task_ids):
        raise ValueError("Choose tasks to export")
    if len(task_ids) != len(set(task_ids)) or len(task_ids) > MAX_TASKS:
        raise ValueError("Choose at most 30 distinct tasks")
    with harness._lock(), tempfile.TemporaryDirectory() as temporary:
        tasks = []
        for task_id in task_ids:
            checkpoint = harness.graph.get_state(harness._config(task_id))
            state = copy.deepcopy(harness._state(task_id))
            if state.get("checks_origin"):
                raise ValueError("Return to the coding task before exporting its handoff")
            state["messages"] = conversation_messages(state)
            if checkpoint.next and not checkpoint.interrupts:
                raise ValueError("Stop the task at a review gate before exporting")
            if state.get("handoff_resume"):
                state.update(state["handoff_resume"])
            workspace = harness._workspace(state)
            fingerprint = workspace.fingerprint()
            expected = (
                state.get("gate_fingerprint")
                if state["next"] == "pause"
                else state.get("phase_fingerprint")
            )
            if expected and expected != fingerprint:
                raise ValueError(
                    "Worktree changed since its saved review. Recheck it before exporting."
                )
            if state["stage"] == "ready" and not harness._review_matches(state, workspace):
                raise ValueError("Reviewed worktree changed; recheck it before exporting")
            _validate_state({k: v for k, v in state.items() if k not in LOCAL_FIELDS})
            head = workspace.revision
            git(workspace.path, "merge-base", "--is-ancestor", state["base"], head)
            bundle = None
            if head != state["base"]:
                bundle_path = Path(temporary) / f"{task_id}.bundle"
                git(
                    workspace.path,
                    "bundle",
                    "create",
                    str(bundle_path),
                    "HEAD",
                    "^" + state["base"],
                )
                if bundle_path.stat().st_size > MAX_HANDOFF_BYTES:
                    raise ValueError("Task commits are too large for a 32 MB handoff")
                bundle = base64.b64encode(bundle_path.read_bytes()).decode()
            patch = git(
                workspace.path,
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
            )
            if patch:
                patch += "\n"
            added = [
                p
                for p in git(
                    workspace.path, "diff", "--diff-filter=A", "--name-only", "-z", "HEAD"
                ).split("\0")
                if p
            ]
            untracked = []
            for name in git(
                workspace.path, "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0"):
                if not name:
                    continue
                path = workspace.safe_path(name)
                if not path.is_file() or path.stat().st_size > MAX_HANDOFF_BYTES:
                    raise ValueError(f"Cannot export unfinished file: {name}")
                untracked.append(
                    {
                        "path": name,
                        "data": base64.b64encode(path.read_bytes()).decode(),
                        "executable": bool(path.stat().st_mode & 0o111),
                    }
                )
            replacements = [
                (str(workspace.path), "${WORKTREE}"),
                (state["directory"], "${TASK}"),
                (str(harness.repository), "${PROJECT}"),
            ]
            portable = _replace(
                {k: v for k, v in state.items() if k not in LOCAL_FIELDS}, replacements
            )
            reports = {}
            report_dir = Path(state["report_directory"])
            if report_dir.is_symlink():
                raise ValueError("Report directory must not be a symbolic link")
            if report_dir.is_dir():
                for path in report_dir.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("Report files must not be symbolic links")
                    if path.is_file():
                        if path.stat().st_size > MAX_HANDOFF_BYTES:
                            raise ValueError("Report is too large for a handoff")
                        reports[path.relative_to(report_dir).as_posix()] = _replace(
                            path.read_text(), replacements
                        )
            tasks.append(
                {
                    "state": portable,
                    "head": head,
                    "fingerprint": fingerprint,
                    "bundle": bundle,
                    "patch": patch,
                    "added": added,
                    "untracked": untracked,
                    "reports": reports,
                }
            )
            if workspace.fingerprint() != fingerprint:
                raise ValueError("Worktree changed during export; try again after edits finish")
        archive = {
            "format": "lambda-harness-handoff",
            "version": 1,
            "created": datetime.now(UTC).isoformat(),
            "project": harness.repository.name,
            "current_memory": harness.context.latest_id(),
            "memory": [harness.context.get(item["id"]) for item in harness.context.history()],
            "tasks": tasks,
        }
        return _validated(archive)


def _plan(harness, archive):
    names = set()
    tasks = []
    for packet in archive["tasks"]:
        state = packet["state"]
        base = state["base"]
        try:
            git(harness.repository, "cat-file", "-e", f"{base}^{{commit}}")
        except RuntimeError as error:
            raise ValueError(
                f"This checkout is missing base commit {base}. "
                "Fetch it from your team's repository, then preview again."
            ) from error
        original = name = state["task_id"]
        index = 0
        while (
            name in names
            or (harness.state_dir / name).exists()
            or harness.graph.get_state(harness._config(name)).values
        ):
            index += 1
            suffix = f"-import-{index}"
            name = original[: 64 - len(suffix)] + suffix
        names.add(name)
        tasks.append(
            {
                "original": original,
                "task_id": name,
                "stage": state["stage"],
                "request": state["request"],
                "head": packet["head"],
                "commits": len(state["commits"]),
                "uncommitted": bool(packet["patch"] or packet["untracked"]),
                "files": [file["path"] for file in packet["untracked"]],
            }
        )
    return {
        "project": archive["project"],
        "created": archive["created"],
        "tasks": tasks,
        "memory_versions": len(archive["memory"]),
        "keep_current_memory": harness.context.latest_id() is not None,
        "confirmation": digest(
            [
                archive,
                tasks,
                str(harness.repository),
                harness.context.latest_id(),
                harness.validation.model_dump(),
            ]
        ),
    }


def preview_handoff(harness, archive):
    data = _validated(archive)
    with harness._lock():
        return _plan(harness, data)


def _restore_files(harness, packet, directory):
    workspace = Workspace(directory / "worktree")
    if packet["patch"]:
        code, _, error = run_process(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            workspace.path,
            stdin=packet["patch"],
            timeout=120,
        )
        if code:
            raise ValueError(f"Cannot restore the unfinished patch: {error.strip()}")
    for name in packet["added"]:
        _path(name)
        git(workspace.path, "add", "--intent-to-add", "--", name)
    for file in packet["untracked"]:
        path = workspace.safe_path(file["path"])
        if path.exists():
            raise ValueError(f"Unfinished file would overwrite a tracked file: {file['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_decode(file["data"]))
        path.chmod(0o755 if file["executable"] else 0o644)
    if workspace.fingerprint() != packet["fingerprint"]:
        raise ValueError("Restored worktree does not match the exported files")
    replacements = [
        ("${WORKTREE}", str(workspace.path)),
        ("${TASK}", str(directory)),
        ("${PROJECT}", str(harness.repository)),
    ]
    for name, text in packet["reports"].items():
        path = directory / "reports" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_replace(text, replacements))
    return _replace(packet["state"], replacements)


def import_handoff(harness, archive, *, confirmation):
    data = _validated(archive)
    with harness._lock(), tempfile.TemporaryDirectory() as temporary:
        plan = _plan(harness, data)
        if confirmation != plan["confirmation"]:
            raise ValueError(
                "The handoff or destination changed. Review a fresh preview before importing."
            )
        created = []
        try:
            for packet, item in zip(data["tasks"], plan["tasks"], strict=True):
                task_id = item["task_id"]
                directory = harness.state_dir / task_id
                branch = task_branch(task_id, directory)
                if git(harness.repository, "branch", "--list", branch):
                    raise ValueError("An import branch already exists; inspect it before retrying")
                if packet["bundle"]:
                    bundle = Path(temporary) / "commits.bundle"
                    bundle.write_bytes(_decode(packet["bundle"]))
                    git(harness.repository, "bundle", "verify", str(bundle))
                    heads = git(harness.repository, "bundle", "list-heads", str(bundle))
                    if heads != f"{packet['head']} HEAD":
                        raise ValueError("The Git bundle does not match the handoff revision")
                    git(
                        harness.repository,
                        "fetch",
                        "--no-tags",
                        "--no-write-fetch-head",
                        str(bundle),
                        "HEAD",
                    )
                git(
                    harness.repository,
                    "merge-base",
                    "--is-ancestor",
                    packet["state"]["base"],
                    packet["head"],
                )
                directory.mkdir(mode=0o700)
                created.append((task_id, directory, branch))
                git(
                    harness.repository,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(directory / "worktree"),
                    packet["head"],
                )
                state = _restore_files(harness, packet, directory)
                state.update(
                    task_id=task_id,
                    directory=str(directory),
                    report_directory=str(directory / "reports"),
                    worktree=str(directory / "worktree"),
                    branch=branch,
                    mode="phased",
                    validation_config=harness.validation.model_dump(),
                    validation_overrides=harness.validation.model_dump(exclude_unset=True),
                )
                # Commands and tool availability must be rediscovered on this machine.
                state.pop("validation_knowledge", None)
                state.pop("validated_fingerprint", None)
                if state.get("retry_node") == "commit" or (
                    state.get("failed_reply", {}).get("response", {}).get("action")
                    in {"approve", "accept_red"}
                    and state.get("role")
                ):
                    state["retry_node"] = "validate"
                    state.pop("failed_reply", None)
                if state["stage"] in {"test_review", "red_failure_review"}:
                    state["prompt"]["actions"] = [
                        a for a in state["prompt"]["actions"] if a not in {"approve", "accept_red"}
                    ]
                    state["prompt"]["guidance"] = (
                        "Run Recheck on this machine before approving these tests. "
                        "Earlier results remain as history."
                    )
                state["handoff_resume"] = {
                    key: copy.deepcopy(state[key]) for key in ("stage", "prompt", "next")
                }
                state["handoff_origin"] = {
                    "project": data["project"],
                    "task": item["original"],
                    "exported": data["created"],
                }
                harness._gate(
                    state,
                    "handoff_review",
                    {
                        "summary": "Review this imported task before continuing",
                        "guidance": f"Paused at {item['stage'].replace('_', ' ')}. "
                        "Review the conversation and worktree. Continue restores that step "
                        "without calling a model. Earlier approvals and check results are "
                        "historical; pending test approval requires local checks.",
                        "imported_stage": item["stage"],
                    },
                    ["continue_handoff"],
                )
                harness.graph.invoke(state, harness._config(task_id))
            with harness.context.connection:
                for snapshot in data["memory"]:
                    harness.context.connection.execute(
                        "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)",
                        (
                            snapshot["id"],
                            snapshot["revision"],
                            snapshot["created"],
                            json.dumps(snapshot),
                        ),
                    )
                if harness.context.latest_id() is None and data["current_memory"]:
                    harness.context.connection.execute(
                        "INSERT INTO current_snapshot VALUES (1, ?)", (data["current_memory"],)
                    )
        except BaseException:
            for task_id, directory, branch in reversed(created):
                harness.graph.checkpointer.delete_thread(task_id)
                if (directory / "worktree" / ".git").exists():
                    git(
                        harness.repository,
                        "worktree",
                        "remove",
                        "--force",
                        str(directory / "worktree"),
                    )
                if git(harness.repository, "branch", "--list", branch):
                    git(harness.repository, "branch", "-D", branch)
                shutil.rmtree(directory)
            raise
        return {
            "tasks": [
                {key: item[key] for key in ("original", "task_id")} for item in plan["tasks"]
            ],
            "memory_versions": plan["memory_versions"],
        }
