"""Git-backed agent definitions; only project selections live in local state."""

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from importlib.resources import files
from pathlib import Path

from .schemas import AgentDefinition
from .workspace import digest, git

ROLES = {
    "test_runner": "Test runner",
    "chat": "General chat",
    "explorer": "Explorer",
    "interviewer": "Requirements interviewer",
    "test_author": "Test author",
    "implementer": "Implementer",
    "refactorer": "Refactorer",
    "reviewer": "Code reviewer",
    "security_reviewer": "Security reviewer",
    "documentation_reviewer": "Documentation reviewer",
    "walkthrough": "Code walkthrough",
}
AVATARS = ("lambda", "robot", "fox", "owl", "crab", "shield")
DEFAULT_AGENTS = {role: f"default-{role.replace('_', '-')}" for role in ROLES}


def assignment_problem(definition):
    if definition["role"] in {"test_author", "implementer", "refactorer"} and {
        language.casefold() for language in definition["languages"]
    } != {"haskell"}:
        return (
            "This coding role needs a supported toolchain. Coding workflows currently support "
            "Haskell only. The definition can be saved for later; chat, exploration, and review "
            "can use other languages now."
        )
    return None


def agent_repository(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
        raise ValueError("Choose the root of a Git checkout for your agents")
    if (path / "agents").is_symlink():
        raise ValueError("The agents directory must not be a symbolic link")
    return path


def discover_agent_repository():
    # Installed packages use bundled definitions; source launches can edit the checkout.
    for candidate in (Path.cwd(), Path(__file__).resolve().parents[2]):
        if (candidate / "agents").is_dir() and (candidate / "pyproject.toml").is_file():
            if 'name = "lambda-harness"' in (candidate / "pyproject.toml").read_text():
                try:
                    return agent_repository(candidate)
                except (ValueError, RuntimeError, OSError):
                    pass
    return None


class AgentLibrary:
    def __init__(self, directory, repository=None):
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repository = Path(repository).expanduser().resolve() if repository else None
        self.source = (
            self.repository / "agents"
            if self.repository
            else files("lambda_harness").joinpath("agents")
        )
        if not self.repository and not self.source.is_dir():
            self.source = Path(__file__).resolve().parents[2] / "agents"
        self.catalog = str(self.repository) if self.repository else "bundled"
        self.connection = sqlite3.connect(directory / "agent-settings.sqlite", timeout=30)
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS assignments "
            "(catalog TEXT NOT NULL, project TEXT NOT NULL, role TEXT NOT NULL, agent TEXT, "
            "PRIMARY KEY (catalog, project, role));"
            "CREATE TABLE IF NOT EXISTS sources "
            "(project TEXT PRIMARY KEY, repository TEXT NOT NULL);"
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    @property
    def writable(self):
        if self.repository is None:
            return False
        try:
            agent_repository(self.repository)
        except (ValueError, RuntimeError, OSError):
            return False
        return os.access(self.repository, os.W_OK)

    def saved_source(self, project):
        row = self.connection.execute(
            "SELECT repository FROM sources WHERE project = ?", (str(Path(project).resolve()),)
        ).fetchone()
        return row[0] if row else None

    def select_source(self, project, repository):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO sources VALUES (?, ?)",
                (str(Path(project).resolve()), str(agent_repository(repository))),
            )

    def _path(self, agent_id):
        if not isinstance(agent_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", agent_id):
            raise ValueError("Invalid agent ID")
        path = self.source / f"{agent_id}.json"
        if self.repository and (self.source.is_symlink() or path.is_symlink()):
            raise ValueError("Agent files must not be symbolic links")
        return path

    def get(self, agent_id):
        path = self._path(agent_id)
        if not path.is_file():
            raise ValueError(f"Unknown agent: {agent_id}. Open Agents to update your selection.")
        if len(content := path.read_bytes()) > 100_000:
            raise ValueError(f"Agent definition is too large: {agent_id}")
        definition = AgentDefinition.model_validate_json(content).model_dump()
        return {
            "id": agent_id,
            "version": digest([agent_id, definition]),
            "definition": definition,
            "path": f"agents/{agent_id}.json",
        }

    def list(self):
        if self.repository:
            agent_repository(self.repository)
        if not self.source.is_dir():
            return []
        return [
            self.get(path.name.removesuffix(".json"))
            for path in sorted(self.source.iterdir(), key=lambda path: path.name)
            if path.name.endswith(".json")
        ]

    def save(self, agent_id, definition, *, expected_version):
        if not self.writable:
            raise ValueError("Choose a writable agent repository on the Agents page before saving")
        definition = AgentDefinition.model_validate(definition).model_dump()
        if not definition["name"].strip() or not definition["instructions"].strip():
            raise ValueError("An agent needs a name and instructions")
        path = self._path(agent_id)
        # Git's common directory gives every process using this checkout the same lock.
        lock = Path(git(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        with (lock / "lambda-agents.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            current = self.get(agent_id) if path.exists() else None
            version = digest([agent_id, definition])
            if current and current["version"] == version:
                return current
            if (current["version"] if current else None) != expected_version:
                raise ValueError(
                    "This agent changed in another session. "
                    "Open its latest version and edit it again."
                )
            if current and current["definition"]["role"] != definition["role"]:
                raise ValueError(
                    "Keep this agent's role when editing. Add an agent for another role."
                )
            self.source.mkdir(exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=self.source, prefix=".agent-", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(json.dumps(definition, indent=2, ensure_ascii=False) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return self.get(agent_id)

    def assign(self, repository, role, agent_id):
        if role not in ROLES:
            raise ValueError("Unknown agent role")
        if agent_id is not None:
            profile = self.get(agent_id)
            if profile["definition"]["role"] != role:
                raise ValueError("Agent role does not match this assignment")
            if problem := assignment_problem(profile["definition"]):
                raise ValueError(problem)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO assignments VALUES (?, ?, ?, ?)",
                (self.catalog, str(Path(repository).resolve()), role, agent_id),
            )

    def assignments(self, repository):
        available = {profile["id"]: profile for profile in self.list()}
        selections = {role: name for role, name in DEFAULT_AGENTS.items() if name in available}
        selections.update(
            dict(
                self.connection.execute(
                    "SELECT role, agent FROM assignments WHERE catalog = ? AND project = ?",
                    (self.catalog, str(Path(repository).resolve())),
                )
            )
        )
        return selections

    def selected(self, repository, *, validate=True):
        available = {profile["id"]: profile for profile in self.list()}
        selections = self.assignments(repository)
        profiles = {}
        for role, agent_id in selections.items():
            if agent_id is None:
                continue
            profile = available.get(agent_id)
            if profile is None:
                if validate:
                    raise ValueError(
                        f"Agent {agent_id} was removed. Open Agents to choose another."
                    )
                continue
            if validate:
                if profile["definition"]["role"] != role:
                    raise ValueError(
                        f"Agent {agent_id} changed role. Update its project assignment."
                    )
                if problem := assignment_problem(profile["definition"]):
                    raise ValueError(problem)
            profiles["haskell_reviewer" if role == "reviewer" else role] = profile
        return profiles
