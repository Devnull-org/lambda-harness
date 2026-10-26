"""Serializable boundaries between the workflow, agents, and command runner."""

from pathlib import Path
from typing import Any, Protocol, TypedDict


class CommandResult(TypedDict):
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    junit_xml: list[str]


class Agent(Protocol):
    def __call__(self, role: str, context: dict[str, Any], cwd: Path) -> dict[str, Any]: ...


class Executor(Protocol):
    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        *,
        report_glob: str | None = None,
        use_nix: bool = True,
        project_root: Path | None = None,
    ) -> CommandResult: ...


class View(TypedDict, total=False):
    task_id: str
    stage: str
    prompt: dict[str, Any]
    worktree: str
    base: str
    base_ref: str
    restarted_from: str
    revision: str
    report_directory: str
    project_context_id: str | None
