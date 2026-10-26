"""Select a role's conversation without changing the saved task history."""

import copy

REVIEW_ROLES = {"haskell_reviewer", "security_reviewer", "documentation_reviewer"}
CODING_ROLES = {"test_author", "implementer", "refactorer"}
NODE_ROLES = {
    "plan_checks": "test_runner",
    "run_checks": "test_runner",
    "discover_validation": "validation_discovery",
    "agent_builder": "agent_builder",
    "chat": "chat",
    "explore": "explorer",
    "interview": "interviewer",
    "tests": "test_author",
    "implement": "implementer",
    "refactor": "refactorer",
    "review_haskell": "haskell_reviewer",
    "review_security": "security_reviewer",
    "standalone_review": "reviewers",
    "walkthrough": "walkthrough",
}
STAGE_ROLES = {
    "checks_plan": "test_runner",
    "checks_failed": "test_runner",
    "checks_complete": "test_runner",
    "validation_setup": "validation_discovery",
    "agent_builder": "agent_builder",
    "chat": "chat",
    "exploration": "explorer",
    "interview": "interviewer",
    "specification_review": "interviewer",
    "test_review": "test_author",
    "standalone_review": "reviewers",
}


def messages_for_agent(state, role, *, explain=False):
    """Keep own follow-ups; new review passes start without previous conclusions."""
    selected = []
    reviewer = role in REVIEW_ROLES
    for message in state["messages"]:
        if "reviews" in message:
            if explain and role in message["reviews"]:
                selected.append({"role": role, **message["reviews"][role]})
            continue
        if "answers_by_role" in message:
            if explain and role in message["answers_by_role"]:
                selected.append(
                    {
                        "question": message["question"],
                        **message["answers_by_role"][role],
                    }
                )
            continue
        if message.get("stage") == "review_action":
            # Tool evidence is shared; a posted finding's receipt belongs to its author.
            if reviewer and (message.get("action") == "check_links" or message.get("role") == role):
                selected.append(message)
            continue
        audience = (
            message.get("agent_role")
            or message.get("role")
            or STAGE_ROLES.get(message.get("stage"))
        )
        if not audience and state.get("task_kind") in {"chat", "agent_builder"}:
            audience = state["task_kind"]
        if audience != role and not (audience == "reviewers" and reviewer):
            # Older unlabelled answers can mix agents. Leave them in visible history only.
            continue
        if reviewer and not explain and not message.get("text"):
            continue
        selected.append(message)
    return copy.deepcopy(selected)
