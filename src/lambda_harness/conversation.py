"""Keep interview responses in the conversation, including older saved tasks."""

import copy
import json
from collections import defaultdict
from pathlib import Path


def interview_message(specification, questions, turn):
    return {
        "stage": "interview",
        "role": "interviewer",
        "interview_turn": turn,
        "specification": copy.deepcopy(specification),
        "questions": list(questions or specification["open_questions"]),
    }


def conversation_messages(state):
    messages = _workflow_messages(state)
    path = Path(state["directory"]) / "task-chat.json"
    recorded = {m.get("task_chat_message") for m in messages}
    if path.exists():
        messages.extend(
            m for m in json.loads(path.read_text()) if m["task_chat_message"] not in recorded
        )
    return messages


def _workflow_messages(state):
    """Recover successful interview turns from journals without rewriting checkpoints."""
    messages = state["messages"]
    if state.get("task_kind", "feature") != "feature":
        return copy.deepcopy(messages)
    recorded = {m["interview_turn"] for m in messages if "interview_turn" in m}
    original = [m for m in messages if m.get("role") != "interviewer"]
    recovered = defaultdict(list)
    for path in sorted((Path(state["directory"]) / "steps").glob("*-interview.json")):
        try:
            turn = int(path.name.split("-", 1)[0])
            if turn in recorded or turn >= state["seq"]:
                continue
            saved = json.loads(path.read_text())
            if saved["task_id"] != state["task_id"] or saved["stage"] not in {
                "interview",
                "specification_review",
            }:
                continue
            preceding = [m for m in saved["messages"] if m.get("role") != "interviewer"]
            if preceding != original[: len(preceding)]:
                continue
            message = interview_message(
                saved["specification"], saved["prompt"].get("questions", []), turn
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A missing or incomplete journal must not hide the rest of the conversation.
            continue
        recovered[len(preceding)].append(message)
    result, position = [], 0
    for message in messages:
        result.extend(recovered.pop(position, []))
        result.append(message)
        if message.get("role") != "interviewer":
            position += 1
    result.extend(recovered.pop(position, []))
    return copy.deepcopy(result)
