"""Read model catalogs through provider CLIs, without generating a response."""

import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .process import run_process


class ModelConnection:
    def __init__(self, process, timeout):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.buffer = b""
        self.size = 0

    def send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError("Model discovery exited before returning a catalog") from error

    def receive(self, matches):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < self.deadline:
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Provider returned an invalid model response")
                    if matches(message):
                        return message
                if not selector.select(max(0, self.deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Model discovery exited before returning a catalog")
                self.size += len(chunk)
                if self.size > 2_000_000:
                    raise ValueError("Model catalog is too large")
                self.buffer += chunk
        raise RuntimeError("Model discovery timed out; you can still use the provider default")


@contextmanager
def connection(argv, cwd, environment, timeout):
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            yield ModelConnection(process, timeout)
        finally:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:
                    raise
                process.wait()


def codex_models(client):
    client.send(
        {
            "id": 0,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "lambda_harness",
                    "title": "Lambda Harness",
                    "version": "0.1.0",
                }
            },
        }
    )
    if client.receive(lambda m: m.get("id") == 0).get("error"):
        raise RuntimeError("Could not initialize provider model discovery")
    client.send({"method": "initialized", "params": {}})
    models, cursor, seen = [], None, set()
    for page in range(1, 101):
        client.send(
            {
                "id": page,
                "method": "model/list",
                "params": {
                    "limit": 100,
                    "includeHidden": False,
                    "cursor": cursor,
                },
            }
        )
        reply = client.receive(lambda m, page=page: m.get("id") == page)
        if reply.get("error"):
            raise RuntimeError("Provider could not list models; check its login and connection")
        result = reply["result"]
        models.extend(m for m in result["data"] if not m.get("hidden"))
        cursor = result.get("nextCursor")
        if not cursor:
            return {
                "models": [
                    {
                        "id": m["model"],
                        "name": m.get("displayName", m["model"]),
                        "efforts": [
                            e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])
                        ],
                        "default_effort": m.get("defaultReasoningEffort"),
                    }
                    for m in models
                ],
                "default": next((m["model"] for m in models if m.get("isDefault")), None),
            }
        if cursor in seen:
            break
        seen.add(cursor)
    raise RuntimeError("Provider model pagination did not finish")


def claude_models(client):
    client.send(
        {"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}
    )
    reply = client.receive(
        lambda m: (
            m.get("type") == "control_response"
            and m.get("response", {}).get("request_id") == "models"
        )
    )["response"]
    if reply.get("subtype") == "error":
        raise RuntimeError("Provider could not list models; check its login and connection")
    models = reply["response"]["models"]
    return {
        "models": [
            {
                "id": m["value"],
                "name": m.get("displayName", m["value"]),
                "resolved_model": m.get("resolvedModel"),
                "efforts": m.get("supportedEffortLevels", []),
            }
            for m in models
        ],
        "default": next((m["value"] for m in models if m["value"] == "default"), None),
    }


def line_models(output):
    result = {"models": []}
    for line in re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output).splitlines():
        match = re.fullmatch(r"([\w.\[\],=:/-]+) - (.+)", line.strip())
        if not match:
            continue
        model_id, name = match.groups()
        marker = re.search(r" \((current|default|current, default)\)$", name)
        if marker:
            for flag in marker[1].split(", "):
                result[flag] = model_id
            name = name[: marker.start()]
        result["models"].append({"id": model_id, "name": name})
    return result


def discover_models(agent, cwd, *, timeout=15):
    backend = agent.backend
    if not backend.models_command:
        return {
            "models": [],
            "current": agent.model,
            "default": None,
            "notice": "This backend does not expose a model catalog.",
        }
    executable = agent.executable(cwd)
    if not executable:
        raise RuntimeError(f"Backend {agent.name!r} is not installed")
    argv = [executable if arg == "{executable}" else arg for arg in backend.models_command]
    with TemporaryDirectory(prefix="lambda-harness-models-") as scratch:
        if backend.models_format in {"codex", "claude"}:
            with connection(argv, scratch, agent.environment(), timeout) as client:
                result = (
                    codex_models(client)
                    if backend.models_format == "codex"
                    else claude_models(client)
                )
        else:
            code, stdout, _ = run_process(
                argv, Path(scratch), env=agent.environment(), timeout=timeout
            )
            if code:
                raise RuntimeError(
                    "Could not list models; check the provider's login and connection"
                )
            result = line_models(stdout) if backend.models_format == "lines" else json.loads(stdout)
    if not isinstance(result, dict) or not isinstance(result.get("models"), list):
        raise ValueError("Provider returned an invalid model catalog")
    models, seen = [], set()
    for item in result["models"]:
        if not isinstance(item, dict) or not valid_model(item.get("id")):
            raise ValueError("Provider returned an invalid model identifier")
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        name = item.get("name") or item["id"]
        if not isinstance(name, str):
            raise ValueError("Provider returned an invalid model name")
        efforts = item.get("efforts", [])
        if not isinstance(efforts, list) or not all(valid_effort(e) for e in efforts):
            raise ValueError("Provider returned invalid effort options")
        if not (backend.effort_args or backend.approval_command or backend.input_format == "json"):
            efforts = []
        models.append(
            {
                "id": item["id"],
                "name": name[:200],
                "efforts": list(dict.fromkeys(efforts)),
                "default_effort": item.get("default_effort")
                if item.get("default_effort") in efforts
                else None,
                "resolved_model": item.get("resolved_model")
                if valid_model(item.get("resolved_model"))
                else None,
            }
        )
    if not models or len(models) > 1000:
        raise ValueError("Provider returned an empty or oversized model catalog")
    for key in ("current", "default"):
        if result.get(key) is not None and not valid_model(result[key]):
            raise ValueError("Provider returned an invalid default model")
    current = result.get("current")
    # This invocation explicitly ignores Codex's saved model preference.
    if backend.models_format == "codex" and "--ignore-user-config" in backend.command:
        current = result.get("default")
    return {
        "models": models,
        "current": agent.model or current,
        "default": result.get("default"),
        "notice": None,
    }


def valid_model(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


def valid_effort(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) is not None
