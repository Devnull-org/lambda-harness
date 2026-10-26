"""Read project evidence before choosing commands; never execute it during discovery."""

import platform
from pathlib import PurePosixPath

from .validation import ValidationConfig
from .workspace import digest


def validation_environment(executor):
    architecture = {"arm64": "aarch64", "AMD64": "x86_64"}.get(
        platform.machine(), platform.machine()
    )
    return {
        "system": f"{architecture}-{platform.system().lower()}",
        "nix_flake": getattr(executor, "nix_flake", None),
    }


def project_files(workspace):
    candidates = []
    for name in workspace.files():
        path = PurePosixPath(name)
        if (
            path.suffix in {".nix", ".cabal"}
            or path.name.lower()
            in {"agents.md", "claude.md", "justfile", "makefile", "flake.lock", ".gitlab-ci.yml"}
            or path.name.startswith("cabal.project")
            or name.startswith(".github/workflows/")
            or path.name.lower().startswith(("readme", "contributing"))
            or path.name
            in {
                "Main.hs",
                "Spec.hs",
                "TastyMain.hs",
                "package.yaml",
                "package.json",
                "pyproject.toml",
                "Cargo.toml",
                "go.mod",
                "tox.ini",
                "pytest.ini",
                "pnpm-workspace.yaml",
                "pom.xml",
                "build.gradle",
                "Gemfile",
                ".pre-commit-config.yaml",
            }
        ):
            candidates.append(name)
    return sorted(candidates)


def inventory_fingerprint(workspace):
    hashes = {}
    for name in project_files(workspace):
        try:
            workspace.safe_path(name)
        except ValueError:
            continue
        hashes[name] = workspace.file_hash(name)
    return digest(hashes)


def project_inventory(workspace):
    candidates = project_files(workspace)
    # Give the agent entry points, not generated artifacts or the entire repository.
    excerpts, remaining = [], 60_000
    for name in sorted(candidates):
        try:
            path = workspace.safe_path(name)
        except ValueError:
            continue
        if not path.is_file() or path.stat().st_size > 100_000:
            continue
        content = path.read_text(errors="replace")
        excerpt = content[: min(remaining, 3000)]
        if excerpt:
            excerpts.append({"path": name, "content": excerpt, "truncated": excerpt != content})
            remaining -= len(excerpt)
    return {"files": sorted(candidates)[:1500], "excerpts": excerpts}


def saved_overrides(state, defaults):
    if "validation_overrides" in state:
        return state["validation_overrides"]
    # Older tasks saved defaults as though a developer had chosen them.
    legacy = ValidationConfig(lint=["hlint", "."], junit_glob="test-reports/**/*.xml").model_dump()
    saved = state.get("validation_config", {})
    overrides = {key: value for key, value in saved.items() if value != legacy.get(key)}
    return {**defaults.model_dump(exclude_unset=True), **overrides}


def source_hashes(workspace, sources):
    hashes = {}
    tracked = set(workspace.files())
    for source in sources:
        name = source["path"]
        path = workspace.safe_path(name)
        if name not in tracked or not path.is_file():
            raise ValueError(f"Validation source is not a project file: {name}")
        hashes[name] = workspace.file_hash(name)
    return hashes


def knowledge_is_current(workspace, knowledge, packages, detailed, environment):
    if not knowledge or knowledge.get("version") != 1 or not knowledge.get("ready"):
        return False
    if knowledge["packages"] != packages or knowledge["detailed"] != detailed:
        return False
    if knowledge.get("environment") != environment:
        return False
    try:
        return knowledge["hashes"] == source_hashes(
            workspace, knowledge["sources"]
        ) and knowledge.get("inventory_fingerprint") == inventory_fingerprint(workspace)
    except (ValueError, OSError):
        return False


def prepare_knowledge(workspace, discovery, packages, overrides, *, detailed, environment=None):
    questions = list(discovery["questions"])
    config = None
    if discovery["checks"] is not None:
        config = ValidationConfig.model_validate({**discovery["checks"], **overrides})
        config.commands(packages)
        if detailed and not config.junit_glob:
            questions.append(
                "This phase needs named test results to verify the intended assertions. "
                "Which supported report or existing adapter should it use? "
                "No test runner changes have been made."
            )
    if set(packages) != {suite["package"] for suite in discovery["suites"]}:
        questions.append("How are tests run for each of the selected packages?")
    if config is None and not questions:
        questions.append("Which project commands should validate this change?")
    knowledge = {
        **discovery,
        "version": 1,
        "packages": packages,
        "detailed": detailed,
        "environment": environment,
        "checks": config.model_dump() if config else None,
        "questions": list(dict.fromkeys(questions)),
        "hashes": source_hashes(workspace, discovery["sources"]),
        "inventory_fingerprint": inventory_fingerprint(workspace),
        "revision": workspace.revision,
        "ready": config is not None and not questions,
    }
    knowledge["id"] = digest(knowledge)
    return knowledge
