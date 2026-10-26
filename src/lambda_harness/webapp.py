"""The local website for persistent development workflows."""

import copy
import json
import re
import secrets
import shutil
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .agent_library import (
    AVATARS,
    ROLES,
    AgentLibrary,
    assignment_problem,
    discover_agent_repository,
)
from .backends import CommandAgent
from .cancellation import ModelCancellation
from .config import RoutedAgent, load_settings
from .conversation import conversation_messages
from .discovery import SelectionCancelled, SelectionRequired, select_backend
from .handoff import MAX_HANDOFF_BYTES, export_handoff, import_handoff, preview_handoff
from .model_catalog import discover_models, valid_model
from .phone import PhoneAccess, request_origin
from .project_context import ContextStore, state_directory
from .project_settings import ProjectSettings, project_identity
from .projects import ProjectRegistry, validate_repository
from .review_source import resolve_review
from .validation import LocalExecutor
from .workflow import Harness
from .workspace import Workspace, digest, git, validate_task_name


class LocalApp:
    def __init__(self, args):
        self.args = args
        self.settings = load_settings(args.config)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.job = None  # Latest job, retained for project-level callers.
        self.jobs = {}
        self.cancellations = {}
        self.workers = []
        self.worker = None
        self.cancellation = ModelCancellation()
        self.shutdown = threading.Event()
        self.identity = project_identity(args.repo)
        with ProjectSettings(state_directory(args.repo, args.state_dir)) as preferences:
            saved_backend = preferences.backend()
        self.unavailable_backend = (
            saved_backend if saved_backend not in {None, "auto", *self.settings.backends} else None
        )
        saved_backend = None if self.unavailable_backend else saved_backend
        self.project_backend = saved_backend
        self.backend_origin = "project" if saved_backend else "default"
        self.selected_backend = self.project_backend
        self.model_lock = threading.Lock()
        self.model_cache = {}
        self.agent_settings = args.state_dir / "agents"
        with AgentLibrary(self.agent_settings) as library:
            self.agent_repository = (
                args.agents_repo or library.saved_source(args.repo) or discover_agent_repository()
            )

    def agents(self):
        with AgentLibrary(self.agent_settings, self.agent_repository) as library:
            problem = None
            try:
                profiles = library.list()
                selected = library.assignments(self.args.repo)
            except (ValueError, RuntimeError, OSError) as error:
                profiles, selected, problem = [], {}, str(error)
            repository = str(library.repository) if library.repository else None
            writable = library.writable
        return {
            "project": self.identity,
            "roles": ROLES,
            "avatars": AVATARS,
            "repository": repository,
            "writable": writable and problem is None,
            "error": problem,
            "profiles": [
                {**profile, "assignment_problem": assignment_problem(profile["definition"])}
                for profile in profiles
            ],
            "selected": {role: agent_id for role, agent_id in selected.items() if agent_id},
        }

    def select_agent_repository(self, body):
        with self.lock:
            if self.running():
                raise ValueError("Wait for the current operation before changing repositories")
            if not isinstance(body.get("repository"), str) or not body["repository"].strip():
                raise ValueError("Enter the path to an agent repository checkout")
            with AgentLibrary(self.agent_settings, body["repository"]) as library:
                library.list()
                library.select_source(self.args.repo, library.repository)
                self.agent_repository = library.repository
        return self.agents()

    def assign_agent(self, body):
        with self.lock:
            if self.running():
                raise ValueError("Wait for the current operation before changing agents")
            if not isinstance(body.get("role"), str) or (
                body.get("agent_id") is not None and not isinstance(body["agent_id"], str)
            ):
                raise ValueError("Choose an agent and role")
            with AgentLibrary(self.agent_settings, self.agent_repository) as library:
                library.assign(self.args.repo, body["role"], body.get("agent_id"))
        return {"saved": True}

    def project(self):
        with ContextStore(state_directory(self.args.repo, self.args.state_dir)) as context:
            saved = {"latest": context.latest_id(), "history": context.history()}
        flake = (self.args.nix_flake or self.args.repo).expanduser().resolve()
        return {
            "project": self.identity,
            "context": saved,
            "backends": [
                {"name": name, "available": bool(CommandAgent(backend).executable(self.args.repo))}
                for name, backend in sorted(self.settings.backends.items())
            ],
            "backend": self.project_backend or self.settings.default_backend,
            "backend_origin": self.backend_origin,
            "unavailable_backend": self.unavailable_backend,
            "tools": {
                "git": bool(shutil.which("git")),
                "nix": bool(shutil.which("nix")),
                "flake": (flake / "flake.nix").is_file(),
            },
        }

    def context_snapshot(self, snapshot=None):
        with ContextStore(state_directory(self.args.repo, self.args.state_dir)) as context:
            return context.get(snapshot)

    def context_preview(self, snapshot=None, task_id=None):
        harness = self.harness()
        try:
            if task_id:
                state = harness._state(task_id)
                workspace = harness._workspace(state)
                selected = snapshot or state.get("project_context_id")
            else:
                workspace = Workspace(self.args.repo)
                selected = snapshot or harness.context.latest_id()
            return {
                "context": harness.context.for_agent(selected, workspace) if selected else None,
                "revision": workspace.revision,
                "task_id": task_id,
            }
        finally:
            harness.close()

    def project_change(self, operation, body):
        with self.lock:
            if self.running():
                raise ValueError("Wait for the current operation before changing project settings")
            if operation == "backend":
                name = body.get("backend")
                if not isinstance(name, str) or (
                    name != "auto" and name not in self.settings.backends
                ):
                    raise ValueError("Unknown backend")
                with ProjectSettings(
                    state_directory(self.args.repo, self.args.state_dir)
                ) as preferences:
                    preferences.save_backend(name)
                self.project_backend = self.selected_backend = name
                self.backend_origin = "project"
                self.unavailable_backend = None
                return {"backend": name, "project": self.identity}
            harness = self.harness()
            try:
                if operation == "use":
                    if not isinstance(body.get("task_id"), str) or not isinstance(
                        body.get("snapshot"), str
                    ):
                        raise ValueError("Choose a task and saved snapshot")
                    return harness.use_context(body["task_id"], body["snapshot"])
                with harness._lock():
                    if operation in {"save", "restore"}:
                        if "expected_current" not in body:
                            raise ValueError("Include the current memory version before saving")
                        if operation == "restore":
                            if not isinstance(body.get("snapshot"), str) or not body["snapshot"]:
                                raise ValueError("Choose a saved memory version")
                            return harness.context.restore(
                                body["snapshot"], expected_current=body["expected_current"]
                            )
                        return harness.context.save(
                            self.args.repo,
                            notes=body.get("notes"),
                            sources=body.get("sources"),
                            expected_current=body["expected_current"],
                            refresh_sources=body.get("refresh_sources", False),
                        )
                    sources = body.get("sources")
                    if not isinstance(sources, list) or not all(
                        isinstance(path, str) for path in sources
                    ):
                        raise ValueError("Choose source file paths")
                    return harness.context.refresh(self.args.repo, sources=sources)
            finally:
                harness.close()

    def running(self):
        with self.lock:
            return bool((self.job or {}).get("running")) or any(
                job["running"] for job in self.jobs.values()
            )

    def stop(self, task_id=None, job_id=None):
        if any(value is not None and not isinstance(value, str) for value in (task_id, job_id)):
            raise ValueError("Choose a task and operation to stop")
        with self.lock:
            running = list(
                {job["id"]: job for job in self.jobs.values() if job["running"]}.values()
            )
            if task_id is None:
                if len(running) > 1:
                    raise ValueError("Choose the task to stop")
                task_id = running[0]["task_id"] if running else None
            job = self.jobs.get(task_id)
            if not job or not job["running"] or (job_id and job["id"] != job_id):
                return False
            return self.cancellations[task_id].stop()

    def harness(self, agent=None, *, cancellation=None, progress=None):
        return Harness(
            self.args.repo,
            self.args.state_dir,
            agent,
            LocalExecutor(
                nix_flake=self.args.nix_flake,
                timeout=self.args.check_timeout,
                cancel=self.shutdown,
                cancellation=cancellation or self.cancellation,
            ),
            unsigned_commits=self.args.unsigned_commits,
            progress=progress or self.progress,
            validation=self.settings.validation,
            agent_repository=self.agent_repository,
            pause_on_error=True,
        )

    def progress(self, message, job=None):
        with self.lock:
            target = job if job is not None else self.job
            if target is not None:
                target["log"] = [*target["log"], message][-200:]

    def state(self, task_id=None):
        with self.lock:
            selected_job = self.jobs.get(task_id) if task_id is not None else self.job
            job = copy.deepcopy(selected_job)
            # Other tasks need sidebar status only; their conversations and logs stay scoped.
            jobs = [
                {
                    **{key: job[key] for key in ("id", "task_id", "running", "error")},
                    "scope_task_id": name,
                }
                for name, job in self.jobs.items()
            ]
            cancellation = (
                self.cancellations.get(task_id) if task_id is not None else self.cancellation
            )
            project_running = self.running()
        harness = self.harness()
        try:
            harness.graph.checkpointer.setup()
            names = [
                row[0]
                for row in harness.connection.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
                )
            ]
            tasks = []
            snapshots = {}
            for name in names:
                saved = harness.graph.get_state(harness._config(name)).values
                if not saved:
                    continue
                snapshots[name] = saved
                item = {"task_id": name, "stage": saved["stage"]}
                item["outcome"] = harness._outcome(saved, saved.get("prompt", {}))
                if saved["stage"] == "test_change_review" and saved.get("next") == "pause":
                    # Questions refresh approval tokens without creating a new patch request.
                    item["permission_request"] = digest(saved["proposed_test_change"])
                if (
                    saved["stage"] in {"validation_blocked", "backend_blocked", "agent_error"}
                    and saved.get("next") == "pause"
                ):
                    item["attention_request"] = digest(saved.get("validation", saved["prompt"]))
                if saved.get("task_kind") == "agent_builder":
                    item.update(
                        task_kind="agent_builder",
                        display_name=(saved.get("draft_definition") or {}).get("name", "New agent"),
                    )
                tasks.append(item)
            task = None
            if task_id in snapshots:
                saved = snapshots[task_id]
                task = harness._view(saved, inspect_worktree=False)
                task["request"] = saved["request"]
                task["messages"] = conversation_messages(saved)
                task["can_use_context"] = harness.can_use_context(task_id)
                with ProjectSettings(harness.state_dir) as preferences:
                    task["model_selection"] = preferences.task_model(task_id)
        finally:
            harness.close()
        active = cancellation.active if cancellation else None
        return {
            "project": self.identity,
            "repository": str(self.args.repo),
            "base": git(self.args.repo, "branch", "--show-current") or "HEAD",
            "tasks": tasks,
            "task": task,
            "selected_task_id": task_id,
            "job": job,
            "jobs": jobs,
            "project_running": project_running,
            "can_stop": active is not None and not active.is_set(),
            "stopping": active is not None and active.is_set(),
            "activity_kind": cancellation.kind if cancellation else None,
            "backend": self.project_backend or self.settings.default_backend,
        }

    def models(self, refresh=False):
        routed = RoutedAgent(self.settings, backend=self.selected_backend, model=self.args.model)
        candidates = (
            [
                CommandAgent(profile, name=name, model=self.args.model)
                for name, profile in sorted(self.settings.backends.items())
                if CommandAgent(profile).executable(self.args.repo)
            ]
            if routed.automatic
            else [routed.resolve("chat")]
        )
        catalogs = []
        # This lock never blocks chat, stop, or state requests.
        with self.model_lock:
            for agent in candidates:
                key = (agent.name, agent.model)
                cached = self.model_cache.get(key)
                if refresh or cached is None or time.monotonic() - cached[0] > 300:
                    try:
                        catalog = discover_models(agent, self.args.repo)
                    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
                        catalog = {
                            "models": [],
                            "current": agent.model,
                            "default": None,
                            "error": str(error),
                        }
                    self.model_cache[key] = (time.monotonic(), catalog)
                catalogs.append({"backend": agent.name, **self.model_cache[key][1]})
        return {"backend": "auto" if routed.automatic else candidates[0].name, "catalogs": catalogs}

    def start(self, command):
        allowed = {
            "checks",
            "task_checks",
            "task_chat",
            "start",
            "agent_builder",
            "chat",
            "explore",
            "review",
            "review_action",
            "tests",
            "resume",
            "run",
            "respond",
            "restart",
            "abandon",
        }
        if command.get("command") not in allowed:
            raise ValueError("Unknown workflow command")
        if not isinstance(command.get("task_id"), str):
            raise ValueError("Choose a task name")
        validate_task_name(command["task_id"])
        command = dict(command)
        if "backend" not in command and "model" not in command:
            with ProjectSettings(
                state_directory(self.args.repo, self.args.state_dir)
            ) as preferences:
                if saved := preferences.task_model(command["task_id"]):
                    command.update(saved)
        for field in (
            "request",
            "base",
            "action",
            "phase",
            "text",
            "token",
            "confirmation",
            "backend",
            "model",
            "head",
            "reviewers",
            "expectation",
            "agent_id",
            "role",
            "finding_id",
        ):
            if command.get(field) is not None and not isinstance(command[field], str):
                raise ValueError(f"{field} must be text")
        ids = command.get("finding_ids", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("Finding IDs must be a list of names")
        if not isinstance(command.get("walkthrough", False), bool):
            raise ValueError("Walkthrough must be enabled or disabled")
        if "automatic" in command and not isinstance(command["automatic"], bool):
            raise ValueError("Automatic must be enabled or disabled")
        for field in ("packages", "criteria"):
            if field in command and (
                not isinstance(command[field], list)
                or not all(isinstance(item, str) for item in command[field])
            ):
                raise ValueError(f"{field} must be a list of text entries")
        backend = command.get("backend", self.project_backend)
        if backend is not None and backend != "auto" and backend not in self.settings.backends:
            raise ValueError("Unknown backend")
        if command.get("model") is not None:
            if not valid_model(command["model"]):
                raise ValueError("Choose a valid model identifier")
            if (backend or self.settings.default_backend) == "auto":
                raise ValueError("Choose a model from a specific backend")
        with self.lock:
            if self.shutdown.is_set():
                raise ValueError("This project was closed; reopen it before starting work")
            if (self.job or {}).get("running") and self.job.get("handoff"):
                raise ValueError("Wait for the project transfer to finish")
            task_id = command["task_id"]
            if self.jobs.get(task_id, {}).get("running"):
                raise ValueError("This task is already running; stop it before sending another")
            job = {
                "id": secrets.token_hex(8),
                "operation": command["command"],
                "task_id": command["task_id"],
                "running": True,
                "log": [],
                "result": None,
                "error": None,
                "selection": None,
            }
            cancellation = ModelCancellation()
            self.job = self.jobs[task_id] = job
            self.cancellation = self.cancellations[task_id] = cancellation
            self.worker = threading.Thread(
                target=self._run,
                args=(dict(command), backend, job, cancellation),
                name=f"harness-web-{task_id}",
            )
            self.workers = [worker for worker in self.workers if worker.is_alive()]
            self.workers.append(self.worker)
            self.worker.start()
        return {"accepted": True}

    def _run(self, command, backend, job, cancellation):
        def progress(message):
            self.progress(message, job)

        harness = None
        try:
            agent = RoutedAgent(
                self.settings,
                backend=backend,
                model=command.get("model", self.args.model),
                timeout=self.args.agent_timeout,
                progress=progress,
                cancellation=cancellation,
            )
            action, task_id = command["command"], command["task_id"]
            if self.shutdown.is_set():
                raise SelectionCancelled
            source = None
            if action == "review":
                with cancellation.request() as cancel:
                    source = resolve_review(
                        self.args.repo,
                        command.get("base", ""),
                        command.get("head", ""),
                        cancel=cancel,
                        progress=progress,
                    )
            if action not in {"abandon", "review_action"} and not (
                action == "respond"
                and command.get("action")
                in {"save_agent", "continue_handoff", "run_checks", "return_to_task"}
            ):
                selected = select_backend(agent, self.args.repo)
                progress(f"Selected backend: {selected}")
                with ProjectSettings(
                    state_directory(self.args.repo, self.args.state_dir)
                ) as preferences:
                    preferences.save_task_model(
                        task_id, selected, command.get("model", self.args.model)
                    )
            if self.shutdown.is_set():
                raise SelectionCancelled
            harness = self.harness(agent, cancellation=cancellation, progress=progress)
            if action == "agent_builder":
                result = harness.start_agent_builder(
                    task_id, command.get("request", ""), agent_id=command.get("agent_id")
                )
            elif action == "checks":
                result = harness.start_checks(
                    task_id, command.get("request", ""), base=command.get("base") or "HEAD"
                )
            elif action == "task_checks":
                result = harness.run_task_checks(task_id)
            elif action == "task_chat":
                result = harness.task_chat(task_id, command.get("text", ""))
            elif action in {"start", "explore", "chat"}:
                if not command.get("base"):
                    raise ValueError("Choose a base branch or commit")
                result = harness.start(
                    task_id,
                    command.get("request", ""),
                    base=command["base"],
                    phase="interview" if action == "start" else action,
                    walkthrough=command.get("walkthrough", False),
                )
            elif action == "review":
                result = harness.start_review(
                    task_id,
                    command.get("request", ""),
                    base=command.get("base", ""),
                    head=command.get("head", ""),
                    reviewers=command.get("reviewers", "both"),
                    source=source,
                )
            elif action == "tests":
                result = harness.start_tests(
                    task_id,
                    command.get("request", ""),
                    base=command.get("base", ""),
                    packages=command.get("packages", []),
                    criteria=command.get("criteria", []),
                    expectation=command.get("expectation", "red"),
                )
            elif action == "resume":
                result = harness.resume(task_id, retry_backend=True)
            elif action == "review_action":
                with cancellation.request() as cancel:
                    result = harness.review_action(
                        task_id,
                        command.get("action"),
                        token=command.get("token"),
                        role=command.get("role"),
                        finding_id=command.get("finding_id"),
                        cancel=cancel,
                    )
            elif action == "run":
                result = harness.run_phase(
                    task_id, command.get("phase", ""), automatic=command.get("automatic", False)
                )
            elif action == "respond":
                result = harness.respond(
                    task_id,
                    command.get("action", ""),
                    text=command.get("text", ""),
                    approval_token=command.get("token"),
                    finding_ids=command.get("finding_ids", []),
                    **({"automatic": command["automatic"]} if "automatic" in command else {}),
                )
            elif action == "restart":
                result = harness.restart(task_id)
            else:
                if command.get("confirmation") != task_id:
                    raise ValueError("Type the task name to confirm deletion")
                result = harness.abandon(task_id, confirmation_token=command.get("token"))
            with self.lock:
                job["result"] = result
                if result["task_id"] != command["task_id"]:
                    job["origin_task_id"] = command["task_id"]
                    job["task_id"] = result["task_id"]
                    self.jobs.setdefault(result["task_id"], job)
                    self.cancellations.setdefault(result["task_id"], cancellation)
        except SelectionRequired as error:
            with self.lock:
                job["selection"] = error.view()
                job["pending_command"] = command
        except SelectionCancelled:
            with self.lock:
                job["error"] = "Backend selection stopped. No new task was started."
        except Exception as error:
            with self.lock:
                job["error"] = str(error)
        finally:
            if harness:
                harness.close()
            with self.lock:
                job["running"] = False

    def abandon_plan(self, task_id):
        if not task_id:
            raise ValueError("Choose a task to abandon")
        harness = self.harness()
        try:
            return harness.abandon_plan(task_id)
        finally:
            harness.close()

    def reserve_handoff(self, operation):
        with self.lock:
            if self.shutdown.is_set():
                raise ValueError("This project was closed; reopen it before transferring work")
            if self.running():
                raise ValueError("Stop or finish the current operation before transferring work")
            self.job = {
                "id": secrets.token_hex(8),
                "task_id": "project handoff",
                "handoff": True,
                "running": True,
                "log": [f"Preparing handoff {operation}…"],
                "result": None,
                "error": None,
                "selection": None,
            }

    def handoff(self, operation, body, *, reserved=False):
        if not reserved:
            self.reserve_handoff(operation)
        harness = None
        try:
            harness = self.harness()
            if operation == "export":
                result = export_handoff(harness, body.get("tasks"))
            elif operation == "preview":
                result = preview_handoff(harness, body.get("archive"))
            else:
                result = import_handoff(
                    harness, body.get("archive"), confirmation=body.get("confirmation")
                )
            self.progress(f"Handoff {operation} complete")
            return result
        except Exception as error:
            with self.lock:
                self.job["error"] = str(error)
            raise
        finally:
            if harness:
                harness.close()
            with self.lock:
                self.job["running"] = False

    def close(self):
        self.shutdown.set()
        with self.lock:
            cancellations = list(self.cancellations.values())
            workers = list(self.workers)
        for cancellation in cancellations:
            cancellation.close()
        for worker in workers:
            worker.join()


class ProjectHub:
    """Each project owns its app and worker; requests select a project explicitly."""

    def __init__(self, args, initial=None):
        self.args = copy.copy(args)
        self.registry = ProjectRegistry(args.state_dir)
        self.lock = threading.RLock()
        self.token = initial.token if initial else secrets.token_urlsafe(32)
        self.apps = {}
        self.default_project = None
        if args.repo:
            project = self.registry.add(args.repo)
            self.default_project = project["id"]
            if initial:
                self.apps[project["id"]] = initial

    def projects(self):
        return {
            "projects": [
                {
                    **project,
                    "available": Path(project["repository"]).is_dir(),
                    "running": bool(
                        self.apps[project["id"]].running() if project["id"] in self.apps else False
                    ),
                }
                for project in self.registry.list()
            ],
            "default_project": self.default_project,
        }

    def project(self, project_id=None):
        project_id = self.default_project if project_id is None else project_id
        if project_id is None:
            return None
        entry = self.registry.get(project_id)
        if project_id not in self.apps:
            args = copy.copy(self.args)
            args.repo = validate_repository(entry["repository"])
            if not self.args.repo or args.repo != Path(self.args.repo).expanduser().resolve():
                args.nix_flake = None
            self.apps[project_id] = LocalApp(args)
        return self.apps[project_id]

    def add(self, body):
        with self.lock:
            return self.registry.add(body.get("repository"))

    def remove(self, body):
        with self.lock:
            project_id = body.get("project_id")
            self.registry.get(project_id)
            if body.get("confirmation") != project_id:
                raise ValueError("Please confirm removal of this project")
            app = self.apps.get(project_id)
            if app:
                with app.lock:
                    if app.running():
                        raise ValueError(
                            "This project has running work. "
                            "Stop it or wait before removing the project."
                        )
                app.close()
                del self.apps[project_id]
            self.registry.remove(project_id)
            if self.default_project == project_id:
                self.default_project = None
            return self.projects()

    def close(self):
        for app in self.apps.values():
            app.close()


def create_server(app, port=8765, *, phone=None):
    hub = app if isinstance(app, ProjectHub) else ProjectHub(app.args, initial=app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, content_type="application/json"):
            data = json.dumps(body).encode() if content_type == "application/json" else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

        def authorized(self, api=False):
            host = f"127.0.0.1:{self.server.server_port}"
            try:
                request_origin(self.headers, host, phone)
            except ValueError as error:
                self.send(403, {"error": str(error)})
                return False
            if api and not secrets.compare_digest(
                self.headers.get("X-Harness-Token", "").encode(), hub.token.encode()
            ):
                self.send(403, {"error": "Reload the harness page to connect"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path)
            if not self.authorized(api=path.path.startswith("/api/")):
                return
            try:
                if path.path == "/api/models":
                    with hub.lock:
                        app = hub.project(self.headers.get("X-Harness-Project"))
                    if app is None:
                        raise ValueError("Add or open a project first")
                    self.send(200, app.models(parse_qs(path.query).get("refresh") == ["1"]))
                    return
                with hub.lock:
                    self.get(path)
            except (ValueError, RuntimeError, OSError) as error:
                self.send(400, {"error": str(error)})

        def get(self, path):
            if path.path == "/phone-qr.svg" and phone is not None:
                self.send(200, phone.qr_svg(), "image/svg+xml")
                return
            if path.path == "/api/projects":
                self.send(200, hub.projects())
                return
            query = parse_qs(path.query)
            task_id = query.get("task", [None])[0]
            if path.path.startswith("/api/"):
                app = hub.project(self.headers.get("X-Harness-Project"))
                if path.path == "/api/state":
                    state = (
                        app.state(task_id)
                        if app
                        else {
                            "project": None,
                            "repository": "",
                            "base": "",
                            "tasks": [],
                            "task": None,
                            "job": None,
                            "can_stop": False,
                            "stopping": False,
                            "backend": "auto",
                        }
                    )
                    self.send(
                        200, {**state, **hub.projects(), "phone": phone.info() if phone else None}
                    )
                    return
                if app is None:
                    raise ValueError("Add or open a project first")
                if path.path == "/api/abandon-plan":
                    self.send(200, app.abandon_plan(task_id))
                elif path.path == "/api/agents":
                    self.send(200, app.agents())
                elif path.path == "/api/project":
                    self.send(200, app.project())
                elif path.path == "/api/context/preview":
                    self.send(200, app.context_preview(query.get("snapshot", [None])[0], task_id))
                elif path.path == "/api/context":
                    self.send(200, app.context_snapshot(query.get("snapshot", [None])[0]))
                else:
                    self.send(404, {"error": "Not found"})
                return
            routes = {
                "/": "index.html",
                "/projects": "index.html",
                "/agents": "index.html",
                "/project-memory": "index.html",
                "/project-settings": "index.html",
                "/guide": "guide.html",
                "/app.js": "app.js",
                "/theme.js": "theme.js",
                "/style.css": "style.css",
                "/fonts/Geist.woff2": "fonts/Geist.woff2",
                "/fonts/JetBrainsMono.woff2": "fonts/JetBrainsMono.woff2",
            }
            match = re.fullmatch(
                r"/projects/([a-f0-9]{16})(?:/(memory|settings|agents))?", path.path
            )
            if match and any(item["id"] == match[1] for item in hub.registry.list()):
                routes[path.path] = "index.html"
            name = routes.get(path.path)
            if name is None:
                self.send(
                    404, {"error": "Project or page not found. Open /projects to choose a project."}
                )
                return
            content = files("lambda_harness").joinpath("web", name).read_bytes()
            if name == "index.html":
                content = content.replace(b"HARNESS_SESSION_TOKEN", hub.token.encode())
            types = {
                "index.html": "text/html; charset=utf-8",
                "guide.html": "text/html; charset=utf-8",
                "app.js": "text/javascript; charset=utf-8",
                "theme.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8",
                "fonts/Geist.woff2": "font/woff2",
                "fonts/JetBrainsMono.woff2": "font/woff2",
            }
            self.send(200, content, types[name])

        def do_POST(self):
            if not self.authorized(api=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                transfer = self.path in {
                    "/api/handoff/export",
                    "/api/handoff/preview",
                    "/api/handoff/import",
                }
                limit = MAX_HANDOFF_BYTES + 1_000_000 if transfer else 1_000_000
                if not 0 < length <= limit:
                    raise ValueError("Request is empty or exceeds the allowed size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if transfer:
                    with hub.lock:
                        app = hub.project(self.headers.get("X-Harness-Project"))
                        if app is None:
                            raise ValueError("Add or open the destination project first")
                        operation = self.path.rsplit("/", 1)[1]
                        app.reserve_handoff(operation)
                    # Bundling commits can take time. Other tabs can still read status.
                    self.send(200, app.handoff(operation, body, reserved=True))
                    return
                with hub.lock:
                    self.post(body)
            except (ValueError, RuntimeError, OSError) as error:
                self.send(400, {"error": str(error)})

        def post(self, body):
            if self.path == "/api/projects":
                self.send(200, hub.add(body))
                return
            if self.path == "/api/projects/remove":
                self.send(200, hub.remove(body))
                return
            app = hub.project(self.headers.get("X-Harness-Project"))
            if app is None:
                raise ValueError("Add or open a project first")
            if self.path == "/api/command":
                self.send(202, app.start(body))
            elif self.path == "/api/agents/source":
                self.send(200, app.select_agent_repository(body))
            elif self.path == "/api/agents/assign":
                self.send(200, app.assign_agent(body))
            elif self.path == "/api/stop":
                self.send(200, {"stopping": app.stop(body.get("task_id"), body.get("job_id"))})
            elif self.path in {
                "/api/context",
                "/api/context/use",
                "/api/context/save",
                "/api/context/restore",
                "/api/backend",
            }:
                operation = {
                    "/api/context": "refresh",
                    "/api/context/use": "use",
                    "/api/context/save": "save",
                    "/api/context/restore": "restore",
                    "/api/backend": "backend",
                }[self.path]
                self.send(200, app.project_change(operation, body))
            else:
                self.send(404, {"error": "Not found"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.hub = hub
    return server


def serve(args):
    phone = PhoneAccess.discover(args.tailscale_port) if args.tailscale else None
    app = ProjectHub(args)
    server = create_server(app, args.port, phone=phone)
    print(f"Lambda Harness: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Saved work: {args.state_dir}", flush=True)
    if args.repo:
        print(f"Initial project: {args.repo}", flush=True)
    print("Keep this process running while using the page.", flush=True)

    def stop_server(*_):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, stop_server)
    try:
        if phone:
            print("Connecting private phone access through Tailscale…", flush=True)
            phone.start(server.server_port)
            print(f"Phone: {phone.origin} (Tailscale account: {phone.login})", flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if phone:
            phone.close()
        app.close()
        server.server_close()
