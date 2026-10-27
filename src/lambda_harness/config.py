"""Explicit, user-owned backend selection with optional routing by agent role."""

import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .backends import Backend, CommandAgent
from .paths import default_config_path
from .schemas import SCHEMAS, Record
from .validation import ValidationConfig


class Route(Record):
    backend: str | None = None
    model: str | None = None


class Settings(Record):
    version: Literal[1] = 1
    default_backend: str = "auto"
    backends: dict[str, Backend]
    roles: dict[str, Route] = Field(default_factory=dict)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)

    @model_validator(mode="after")
    def valid_routes(self):
        if "auto" in self.backends:
            raise ValueError("Backend name 'auto' is reserved for automatic selection")
        if self.default_backend != "auto" and self.default_backend not in self.backends:
            raise ValueError(f"Unknown default backend: {self.default_backend}")
        for role, route in self.roles.items():
            if role not in SCHEMAS:
                raise ValueError(f"Unknown agent role: {role}")
            if route.backend and route.backend not in self.backends:
                raise ValueError(f"Unknown backend for {role}: {route.backend}")
        return self


def load_settings(path=None):
    """Read built-in profiles and one user file; never auto-load target-repo configuration."""
    data = tomllib.loads(files("lambda_harness").joinpath("backends.toml").read_text())
    if path is None:
        path = default_config_path()
        if not path.exists():
            return Settings.model_validate(data)
    path = Path(path).expanduser().resolve()
    with path.open("rb") as stream:
        custom = tomllib.load(stream)
    # A named profile is replaced as a unit so old credential/argument rules cannot leak in.
    data["backends"].update(custom.pop("backends", {}))
    data.update(custom)
    return Settings.model_validate(data)


class RoutedAgent:
    def __init__(
        self,
        settings,
        *,
        backend=None,
        model=None,
        timeout=None,
        progress=None,
        activity=None,
        cancellation=None,
        approval=None,
    ):
        if backend not in {None, "auto"} and backend not in settings.backends:
            raise ValueError(f"Unknown backend {backend!r}; choose a profile in Project settings")
        self.settings = settings
        self.backend = backend
        self.model = model
        self.timeout = timeout
        self.progress = progress
        self.activity = activity
        self.approval = approval
        self.cancellation = cancellation
        self.last_generation = None

    @property
    def automatic(self):
        return (self.backend or self.settings.default_backend) == "auto"

    def resolve(self, role):
        route = self.settings.roles.get(role, Route())
        name = self.backend or route.backend or self.settings.default_backend
        if name == "auto":
            raise ValueError("Select a usable backend before running an agent")
        # A global backend override also drops role-specific models for another backend.
        model = self.model
        if model is None and self.backend is None:
            model = route.model
        return CommandAgent(
            self.settings.backends[name],
            name=name,
            model=model,
            timeout=self.timeout,
            progress=self.progress,
            activity=self.activity,
            cancellation=self.cancellation,
            approval=self.approval,
        )

    def __call__(self, role, context, cwd):
        agent = self.resolve(role)
        result = agent(role, context, cwd)
        self.last_generation = {
            "backend": agent.name,
            "model": agent.model,
            "reported_model": agent.reported_model,
        }
        return result

    def doctor(self, cwd):
        if self.automatic:
            results = []
            for name, backend in sorted(self.settings.backends.items()):
                try:
                    results.append(CommandAgent(backend, name=name).doctor(cwd))
                except RuntimeError as error:
                    results.append((False, f"Backend {name!r}: {error}"))
            return results
        checked = set()
        results = []
        for role in SCHEMAS:
            agent = self.resolve(role)
            if agent.name not in checked:
                checked.add(agent.name)
                results.append(agent.doctor(cwd))
        return results
