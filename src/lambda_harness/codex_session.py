"""A task-local Codex app-server connection with explicit browser approvals."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time

from .process import ProcessError

APPROVALS = {
    "item/commandExecution/requestApproval",
    "item/permissions/requestApproval",
}


def run_session(
    argv, cwd, *, prompt, schema, model, env, timeout, cancel, approve, progress=None, effort=None
):
    """Return the final structured reply and provider model; never auto-grant permissions."""
    if cancel is not None and cancel.is_set():
        raise ProcessError("Model request stopped", kind="cancelled")
    schema = json.loads(json.dumps(schema))

    def strict(value):
        if isinstance(value, dict):
            if "properties" in value:
                value["required"] = list(value["properties"])
            for child in value.values():
                strict(child)
        elif isinstance(value, list):
            for child in value:
                strict(child)

    strict(schema)
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                start_new_session=True,
            )
        except OSError as error:
            raise ProcessError(str(error), kind="unavailable") from error
        started_at = time.monotonic()
        deadline = started_at + timeout
        next_progress = started_at + 15
        last_error = None
        buffer = b""
        thread_id = turn_id = None
        reported_model = None
        answer = None
        total = 0

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        def request(request_id, method, params):
            send({"id": request_id, "method": method, "params": params})

        try:
            request(
                "initialize",
                "initialize",
                {
                    "clientInfo": {"name": "lambda_harness", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ProcessError("Model request stopped", kind="cancelled")
                    if time.monotonic() >= deadline:
                        raise ProcessError(
                            f"Codex session timed out after {timeout:g}s"
                            + (f": {last_error}" if last_error else ""),
                            kind="timeout",
                        )
                    if progress and time.monotonic() >= next_progress:
                        progress(
                            f"Codex: still working ({time.monotonic() - started_at:.0f}s elapsed)"
                        )
                        next_progress = time.monotonic() + 15
                    if b"\n" not in buffer:
                        if not selector.select(0.1):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            errors.seek(0)
                            detail = errors.read()[-4000:].decode(errors="replace")
                            raise ProcessError(
                                "Codex session exited before completing: " + detail,
                                kind="provider",
                                stderr=detail,
                            )
                        total += len(chunk)
                        if total > 64_000_000:
                            raise ProcessError(
                                "Codex session output exceeded its limit", kind="provider"
                            )
                        buffer += chunk
                        continue
                    line, buffer = buffer.split(b"\n", 1)
                    event = json.loads(line)
                    method, params = event.get("method"), event.get("params", {})
                    if method and "id" in event:
                        if params.get("threadId") != thread_id or (
                            turn_id is not None and params.get("turnId") != turn_id
                        ):
                            send(
                                {
                                    "id": event["id"],
                                    "error": {"code": -32602, "message": "Wrong task session"},
                                }
                            )
                            continue
                        if method in APPROVALS:
                            started = time.monotonic()
                            allowed = approve({"method": method, "details": params}, cancel)
                            deadline += time.monotonic() - started
                            next_progress = time.monotonic() + 15
                            if cancel is not None and cancel.is_set():
                                raise ProcessError("Model request stopped", kind="cancelled")
                            if method == "item/permissions/requestApproval":
                                # Grant only the reviewed request, for this turn.
                                result = {
                                    "permissions": params.get("permissions", {}) if allowed else {},
                                    "scope": "turn",
                                }
                            else:
                                result = {"decision": "accept" if allowed else "decline"}
                            send({"id": event["id"], "result": result})
                        elif method == "item/fileChange/requestApproval":
                            # File edits must remain concrete patches reviewed by the harness.
                            send({"id": event["id"], "result": {"decision": "decline"}})
                        else:
                            send(
                                {
                                    "id": event["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": (
                                            "Unsupported interactive request; "
                                            "explain the required action to the developer"
                                        ),
                                    },
                                }
                            )
                        continue
                    if "id" in event:
                        if event.get("error"):
                            raise ProcessError(str(event["error"]), kind="provider")
                        result = event.get("result", {})
                        if event["id"] == "initialize":
                            send({"method": "initialized", "params": {}})
                            settings = {
                                "cwd": str(cwd),
                                "sandbox": "read-only",
                                "approvalPolicy": "on-request",
                                "approvalsReviewer": "user",
                                "ephemeral": True,
                                "allowProviderModelFallback": False,
                                "config": {"web_search": "live"},
                            }
                            if model is not None:
                                settings["model"] = model
                            request("thread", "thread/start", settings)
                        elif event["id"] == "thread":
                            thread_id = result["thread"]["id"]
                            reported_model = result.get("model")
                            if model and reported_model and reported_model != model:
                                raise ProcessError(
                                    f"Requested model {model!r}, "
                                    f"but provider selected {reported_model!r}. "
                                    "Stopped before generation; select an available model.",
                                    kind="provider",
                                )
                            request(
                                "turn",
                                "turn/start",
                                {
                                    "threadId": thread_id,
                                    "input": [{"type": "text", "text": prompt}],
                                    "outputSchema": schema,
                                    **({"model": model} if model is not None else {}),
                                    **({"effort": effort} if effort is not None else {}),
                                },
                            )
                        elif event["id"] == "turn":
                            turn_id = result["turn"]["id"]
                        continue
                    if params.get("threadId") != thread_id:
                        continue
                    if method == "error":
                        last_error = params.get("error", {}).get("message", "Provider error")
                        if progress:
                            suffix = "; retrying" if params.get("willRetry") else ""
                            progress("Codex: " + last_error + suffix)
                    if method == "model/rerouted":
                        reported_model = params.get("toModel") or reported_model
                        if model and reported_model != model:
                            raise ProcessError(
                                f"Provider switched from requested model {model!r} "
                                f"to {reported_model!r}; "
                                "response rejected. Choose a model explicitly before retrying.",
                                kind="provider",
                            )
                    if method == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                            answer = item.get("text")
                    if method == "item/started" and progress:
                        item = params.get("item", {})
                        if item.get("type") in {"commandExecution", "webSearch"}:
                            progress("Codex: " + (item.get("command") or "reading web sources"))
                    if method == "turn/completed":
                        turn = params["turn"]
                        if turn.get("status") != "completed":
                            raise ProcessError(
                                str(turn.get("error") or "Codex turn interrupted"),
                                kind="cancelled"
                                if turn.get("status") == "interrupted"
                                else "provider",
                            )
                        if not answer:
                            raise ProcessError("Codex returned no final response", kind="provider")
                        return answer, reported_model
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ProcessError("Codex connection failed: " + str(error), kind="provider") from error
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    process.wait()
            process.stdin.close()
            process.stdout.close()
