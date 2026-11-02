"""Recovery choices derived from the failed operation, independent of agent role."""

import json
import re
from pathlib import Path

COVERAGE_ERROR = "Tests must map every acceptance criterion to named test cases"


def coverage_gap(specification, proposal):
    criteria = specification["acceptance_criteria"]
    missing = [item for item in criteria if not proposal["coverage"].get(item["id"])]
    return {
        "total": len(criteria),
        "mapped": len(criteria) - len(missing),
        "missing": missing,
        "summary": proposal["summary"],
        "files": [change["path"] for change in proposal["changes"]],
    }


def saved_test_proposal(state):
    if proposal := state.get("rejected_test_proposal"):
        return proposal
    directory = Path(state["directory"]) / "proposals"
    for path in sorted(directory.glob("*-test_author.json"), reverse=True):
        try:
            proposal = json.loads(path.read_text())
            if proposal.get("changes") and "coverage" in proposal:
                return proposal
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def saved_coverage_gap(state):
    """Recover evidence for old checkpoints without changing their approval or retry state."""
    if state.get("retry_node") != "tests" or state.get("prompt", {}).get("error") != COVERAGE_ERROR:
        return None
    if gap := state["prompt"].get("coverage_gap"):
        return gap
    if proposal := saved_test_proposal(state):
        return coverage_gap(state["specification"], proposal)
    return None


def can_recover(state):
    return state.get("stage") in {"agent_error", "backend_blocked", "validation_blocked"}


def commit_failed(state):
    return state.get("stage") == "agent_error" and state.get("retry_node") == "commit"


def recovery_actions(state):
    actions = list(state.get("prompt", {}).get("actions", []))
    if state.get("stage") == "exploration":
        actions.append("start_implementation")
    if can_recover(state):
        actions.append("question")
    if can_recover(state) and saved_coverage_gap(state):
        actions.append("revise_tests")
    if commit_failed(state):
        actions.append("continue_without_commits")
    return list(dict.fromkeys(actions))


def recovery_prompt(state):
    prompt = dict(state["prompt"])
    prompt["actions"] = recovery_actions(state)
    if gap := saved_coverage_gap(state):
        prompt["coverage_gap"] = gap
    if commit_failed(state):
        prompt["guidance"] = (
            "The commit step failed. Your files are still in the task worktree. "
            "Fix the cause and Retry, ask for help or a different approach, or choose "
            "Continue without commits. That choice leaves this task's remaining changes "
            "uncommitted while validation and reviews continue."
        )
        error = prompt.get("error", "").lower()
        if "gpg" in error and any(word in error for word in ("tty", "passphrase", "pinentry")):
            match = re.search(r"KEY_CONSIDERED ([A-Fa-f0-9]{40,64})\b", prompt.get("error", ""))
            key = " --local-user " + match[1] if match else ""
            prompt.setdefault(
                "suggested_commands",
                [
                    {
                        "explanation": "Run in a terminal on the harness machine to unlock "
                        "the signing key, then click Retry. This signs a test message, "
                        "not a commit. "
                        "Unlocking is temporary; your GPG agent's cache settings apply.",
                        "command": 'export GPG_TTY="$(tty)"\n'
                        "gpg-connect-agent updatestartuptty /bye\n"
                        f"printf '%s\\n' 'Lambda Harness signing check' | gpg{key} "
                        "--clearsign > /dev/null",
                    }
                ],
            )
    else:
        prompt["guidance"] = (
            prompt.get("guidance", "") + " " + "Ask what failed, request commands to resolve it, "
            "or describe a different approach."
        ).strip()
    return prompt
