"""Write local reports from recorded analysis and workflow evidence."""


def chat_report(state):
    lines = [
        f"# Chat: {state['task_id']}",
        "",
        f"Repository revision: `{state['base']}`",
        "",
        "## You",
        "",
        state["request"],
    ]
    references = []
    for item in state["messages"]:
        if item.get("text"):
            lines += ["", "## You", "", item["text"]]
        elif item.get("role") == "chat":
            lines += ["", "## Assistant", ""]
            if generation := item.get("generation"):
                model = (
                    generation.get("reported_model")
                    or generation.get("model")
                    or "provider default"
                )
                lines += [f"Model: {generation['backend']} · {model}", ""]
            lines.append(item["answer"])
            references.extend(item["references"])
            if saved := item.get("memory_save"):
                title = (
                    "Saved to project memory" if saved["status"] == "saved" else "Memory not saved"
                )
                lines += ["", f"### {title}", "", saved["note"], ""]
                lines.append(
                    f"Memory version: `{saved['snapshot']}`"
                    if saved["status"] == "saved"
                    else saved["error"]
                )
    if references:
        lines += ["", "## References", "", *[f"- {ref}" for ref in dict.fromkeys(references)]]
    return "\n".join(lines) + "\n"


def exploration_report(state):
    result = state["exploration"]
    lines = [
        f"# Exploration: {state['task_id']}",
        "",
        f"Inspected revision: `{result['revision']}`",
        "",
        result["summary"],
        "",
        result["analysis"],
    ]
    if result["references"]:
        lines += ["", "## References", "", *[f"- {ref}" for ref in result["references"]]]
    if result["open_questions"]:
        lines += ["", "## Open questions", "", *[f"- {q}" for q in result["open_questions"]]]
    return "\n".join(lines) + "\n"


def standalone_review_report(state):
    source = state["review_source"]
    lines = [
        f"# Review: {state['task_id']}",
        "",
        state["request"],
        "",
        f"Base: `{source['base_ref']}` (`{source['base']}`)",
        f"Head: `{source['head_ref']}` (`{source['head']}`)",
        f"Diff starts at merge base: `{source['merge_base']}`",
        "",
        "Read-only inspection. No builds or tests were run. "
        "GitHub actions run only when explicitly requested and are recorded below.",
    ]
    for role, review in state["standalone_reviews"].items():
        lines += ["", f"## {role.replace('_', ' ').capitalize()}", "", review["summary"]]
        for finding in review["findings"]:
            lines += [
                "",
                f"### {finding['id']}: {finding['title']}",
                "",
                f"{finding['severity']} · {finding['confidence']} · {finding['location']}",
                "",
                finding["impact"],
                "",
                f"Conditions: {finding['conditions']}",
                f"Sequence: {' → '.join(finding['sequence'])}",
                f"Evidence: {finding['evidence']}",
                f"Recommendation: {finding['recommendation']}",
            ]
    for item in state.get("messages", []):
        if item.get("action") == "post_review_comment":
            lines += ["", f"Posted {item['finding_id']}: {item['url']}"]
        elif item.get("action") == "check_links":
            lines += [
                "",
                "## Documentation link check",
                "",
                item.get("checked_at", ""),
                item["summary"],
                "",
                item["limitations"],
            ]
            lines += [
                f"- {issue['source']}: {issue['url']} — {issue['status']}: {issue['detail']}"
                for issue in item["issues"]
            ]
    return "\n".join(lines) + "\n"


def pr_description(state):
    spec = state["specification"]
    lines = [spec["problem"], "", spec["expected_behavior"], "", "Acceptance criteria", ""]
    lines += [f"- {item['id']}: {item['description']}" for item in spec["acceptance_criteria"]]
    if spec["assumptions"]:
        lines += ["", "Assumptions", "", *[f"- {item}" for item in spec["assumptions"]]]
    if missing := state.get("test_coverage_gap", {}).get("missing", []):
        lines += [
            "",
            "Requirements without a named test in the initial draft",
            "",
            "These remain in scope. Assess their implementation and other verification "
            "evidence; passing the mapped tests alone does not establish completion.",
            "",
        ]
        lines += [f"- {item['id']}: {item['description']}" for item in missing]
    lines += ["", "Local patch series", ""]
    for patch in state["commits"]:
        lines.append(
            f"- `{patch['revision'][:12]}`: {patch['summary']} ({patch['validation']['status']})"
        )
        if patch["validation"].get("red_exception"):
            lines.append(f"  Developer's red Nix exception: {patch['validation']['red_exception']}")
    lines += ["", "Final validation", ""]
    if state.get("commit_policy") == "manual":
        lines += [
            "Changes remain uncommitted in the task worktree. Commit them manually when ready.",
            f"Handoff working-tree fingerprint: `{state['review_fingerprint']}`",
            "",
            *[
                f"- Uncommitted: {patch['summary']}"
                for patch in state.get("uncommitted_patches", [])
            ],
            "",
        ]
    if state.get("workflow_skips"):
        lines += ["Skipped by developer instruction: " + ", ".join(state["workflow_skips"]), ""]
    for name, command in state.get("validation", {}).get("commands", {}).items():
        lines.append(f"- {name}: `{' '.join(command['argv'])}`; exit {command['exit_code']}")
    reviewed = any(
        state.get(role) and not state[role].get("skipped")
        for role in ("haskell_reviewer", "security_reviewer")
    )
    revision_label = "Reviewed revision" if reviewed else "Handoff revision (not reviewed)"
    lines += ["", f"{revision_label}: `{state['review_revision']}`", "", "Review results", ""]
    for role in ("haskell_reviewer", "security_reviewer"):
        lines += [
            f"{role.replace('_', ' ').capitalize()}: "
            + state.get(role, {}).get("summary", "Not performed"),
            "",
        ]
        for finding in state.get(role, {}).get("findings", []):
            lines += [
                f"- {finding['id']} ({finding['severity']}, {finding['confidence']}): "
                f"{finding['title']} at {finding['location']}",
                f"  Conditions: {finding['conditions']}",
                f"  Sequence: {' → '.join(finding['sequence'])}",
                f"  Impact: {finding['impact']}",
                f"  Evidence: {finding['evidence']}",
                f"  Recommendation: {finding['recommendation']}",
            ]
    for item in state["dispositions"]:
        lines += ["", f"Developer disposition for {item['id']}: {item['reason']}"]
    lines += ["", "Agent reviews are advisory and do not establish absence of security defects."]
    if state.get("walkthrough_answers"):
        lines += ["", "Developer walkthrough", ""]
        for item in state["walkthrough_answers"]:
            lines += [f"- {item['question']}", f"  Developer: {item['answer']}"]
    return "\n".join(lines) + "\n"
