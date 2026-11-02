"""Plan and apply a clean task baseline correction without touching the source checkout."""

from pathlib import Path

from .workspace import git, resolve_base


def plan_baseline(repository, workspace, base, checkout, revision=None):
    source = Path(checkout).expanduser().resolve()
    if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError("Choose the root of the intended Git checkout")
    common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    expected = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common.resolve() != expected.resolve():
        raise ValueError("That checkout belongs to another repository; select its project first")
    if git(source, "status", "--porcelain"):
        raise ValueError("The intended checkout has uncommitted changes; commit them first")
    if workspace.revision != base or git(workspace.path, "status", "--porcelain"):
        raise ValueError("The task has changes; preserve them before changing its baseline")
    target = resolve_base(source, revision or "HEAD")
    if target == base:
        raise ValueError("This task already uses the requested baseline")
    git(repository, "merge-base", "--is-ancestor", base, target)
    return {
        "checkout": str(source),
        "source_head": git(source, "rev-parse", "HEAD"),
        "from": base,
        "to": target,
        "worktree": str(workspace.path),
    }


def apply_baseline(repository, workspace, plan, *, already_authorized=False):
    # Pin both the task and source checkout to the exact plan the user reviewed.
    if (
        already_authorized
        and workspace.revision == plan["to"]
        and not git(workspace.path, "status", "--porcelain")
    ):
        return  # Recover a crash after the fast-forward but before checkpoint persistence.
    current = plan_baseline(repository, workspace, plan["from"], plan["checkout"], plan["to"])
    if current != plan:
        raise ValueError("The checkout changed since review; request a fresh baseline plan")
    git(workspace.path, "merge", "--ff-only", "--no-edit", plan["to"])
