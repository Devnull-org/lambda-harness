"""Provider failures and malformed replies stay inside a controlled workflow."""

import json
from pathlib import Path

import pytest

from lambda_harness.backends import Backend, BackendResponseError, CommandAgent
from lambda_harness.failures import BackendError

from .conftest import SOURCE, SPECIFICATION, reach_test_review
from .helpers import executable


def test_invalid_reply_is_corrected_before_presenting_the_human_gate(rig):
    harness, agent, _ = rig
    agent.responses["interviewer"].extend(
        [
            {"specification": SPECIFICATION, "questions": "not a list"},
            {"specification": SPECIFICATION, "questions": ["Which inputs?"]},
        ]
    )
    view = harness.start("repair", "Fix increment")
    assert view["stage"] == "interview"
    assert view["prompt"]["questions"] == ["Which inputs?"]
    assert len(agent.calls) == 2
    repair = agent.calls[1][1]["response_repair"]
    assert "questions" in repair["validation_error"]
    assert "not a list" in repair["previous_response"]
    assert agent.calls[0][1]["messages"] == agent.calls[1][1]["messages"] == []
    assert view["prompt"]["actions"] == ["answer"]
    assert list((harness.state_dir / "repair/backend-responses").glob("*/stdout.txt"))


def test_repeated_invalid_output_stops_after_one_correction_and_keeps_the_answer(rig):
    harness, agent, _ = rig
    agent.responses["interviewer"].append(
        {"specification": SPECIFICATION, "questions": ["Which inputs?"]}
    )
    harness.start("bounded", "Fix increment")
    calls = []

    def invalid(role, context, cwd):
        calls.append(context)
        raise BackendResponseError("Empty backend response", "", "provider diagnostic")

    harness.agent = invalid
    view = harness.respond("bounded", "answer", text="Negative integers too.")
    assert len(calls) == 2
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["backend_failure"]["kind"] == "invalid_response"
    assert calls[-1]["messages"][-1]["text"] == "Negative integers too."
    directory = Path(view["prompt"]["diagnostics"])
    assert (directory / "attempt-2/stderr.txt").read_text() == "provider diagnostic"
    assert not (harness.state_dir / "bounded/proposals/0003-interviewer.json").exists()
    harness.agent = agent
    view = harness.respond("bounded", "retry")
    assert view["stage"] == "specification_review"
    assert agent.calls[-1][1]["messages"][-1]["text"] == "Negative integers too."


@pytest.mark.parametrize(
    "message,kind",
    [
        ("ActionRequiredError: You've hit your usage limit Get Cursor Pro", "quota"),
        ("insufficient_quota", "quota"),
        ("Credit balance is too low", "quota"),
        ("Not logged in", "authentication"),
        ("429 Too Many Requests", "rate_limit"),
        ("Connection refused", "provider"),
    ],
)
def test_provider_failures_are_classified_and_never_automatically_retried(
    rig, tmp_path, message, kind
):
    harness, _, _ = rig
    calls = tmp_path / "calls"
    script = executable(
        tmp_path,
        "import sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('call\\n')\n"
        f"print({message!r}, file=sys.stderr)\nsys.exit(1)\n",
    )
    harness.agent = CommandAgent(Backend(command=[script]), name="selected")
    view = harness.start("provider-failure", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["backend_failure"] == {"backend": "selected", "kind": kind}
    assert calls.read_text() == "call\n"
    directory = Path(view["prompt"]["diagnostics"])
    assert (directory / "stderr.txt").read_text() == message + "\n"
    assert (directory / "stderr.txt").stat().st_mode & 0o777 == 0o600
    assert view["prompt"]["guidance"]


def test_zero_exit_error_envelope_preserves_the_provider_reason(rig, tmp_path):
    harness, _, _ = rig
    envelope = {"is_error": True, "result": "Your usage limit has been reached"}
    script = executable(tmp_path, f"print({json.dumps(envelope)!r})\n")
    harness.agent = CommandAgent(Backend(command=[script], response_fields=["result"]))
    view = harness.start("error-envelope", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["backend_failure"]["kind"] == "quota"
    assert json.loads(Path(view["prompt"]["diagnostics"], "stdout.txt").read_text()) == envelope


def test_timeout_preserves_partial_logs_and_does_not_start_a_correction(rig, tmp_path):
    harness, _, _ = rig
    script = executable(
        tmp_path,
        "import sys, time\nprint('Inspecting code', file=sys.stderr, flush=True)\ntime.sleep(5)\n",
    )
    harness.agent = CommandAgent(Backend(command=[script]), timeout=1)
    view = harness.start("timeout", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["backend_failure"]["kind"] == "timeout"
    assert "Inspecting code" in Path(view["prompt"]["diagnostics"], "stderr.txt").read_text()
    assert not Path(view["prompt"]["diagnostics"], "attempt-2").exists()


def test_malformed_json_is_corrected_through_the_command_transport(rig, tmp_path):
    harness, _, _ = rig
    capture = tmp_path / "prompts.json"
    reply = json.dumps({"specification": SPECIFICATION, "questions": []})
    script = executable(
        tmp_path,
        "import json, sys\nfrom pathlib import Path\n"
        f"path = Path({str(capture)!r})\n"
        "prompts = json.loads(path.read_text()) if path.exists() else []\n"
        "prompts.append(sys.stdin.read())\npath.write_text(json.dumps(prompts))\n"
        f"print('not json' if len(prompts) == 1 else {reply!r})\n",
    )
    harness.agent = CommandAgent(Backend(command=[script]))
    view = harness.start("transport-repair", "Fix increment")
    assert view["stage"] == "specification_review"
    assert view["prompt"]["actions"] == ["approve", "question", "revise"]
    prompts = json.loads(capture.read_text())
    assert len(prompts) == 2
    assert "Correct its JSON/schema" in prompts[1]
    assert '"previous_response": "not json\\n"' in prompts[1]
    assert "Do not invent developer answers" in prompts[1]


def test_oversized_invalid_output_is_saved_without_an_oversized_correction_request(rig):
    harness, _, _ = rig
    calls = []

    def oversized(role, context, cwd):
        calls.append(role)
        raise BackendResponseError("Invalid JSON", "x" * 64_001, "")

    harness.agent = oversized
    view = harness.start("oversized", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert calls == ["interviewer"]
    assert len(Path(view["prompt"]["diagnostics"], "stdout.txt").read_text()) == 64_001


def test_failed_review_question_keeps_its_gate_after_automatic_correction(rig):
    harness, agent, _ = rig
    harness.start("question", "Fix increment")
    agent.responses["interviewer"].extend([{}, {"answer": "The criterion covers incrementing."}])
    view = harness.respond("question", "question", text="What does the criterion cover?")
    assert view["stage"] == "specification_review"
    assert view["prompt"]["answer"] == "The criterion covers incrementing."
    assert view["prompt"]["actions"] == ["approve", "question", "revise"]
    assert agent.calls[-1][1]["question"] == "What does the criterion cover?"
    assert all(role == "interviewer" for role, _, _ in agent.calls)


def test_correction_is_skipped_when_combined_context_would_be_too_large(rig):
    harness, _, _ = rig
    calls = []

    def invalid(role, context, cwd):
        calls.append(role)
        raise BackendResponseError("Invalid JSON", "x" * 50_000, "")

    harness.agent = invalid
    view = harness.start("large-context", "x" * 360_000)
    assert view["stage"] == "backend_blocked"
    assert calls == ["interviewer"]


@pytest.mark.parametrize("resume", [False, True])
def test_backend_failure_during_test_review_cannot_skip_the_question_by_rechecking(rig, resume):
    harness, agent, _ = rig
    reach_test_review(harness)

    def unavailable(role, context, cwd):
        raise BackendError("Usage limit reached", kind="quota")

    harness.agent = unavailable
    view = harness.respond("increment", "question", text="Which criterion does this test cover?")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["actions"] == ["retry", "question"]
    with pytest.raises(ValueError, match="not available"):
        harness.respond("increment", "recheck")
    harness.agent = agent
    view = (
        harness.resume("increment", retry_backend=True)
        if resume
        else harness.respond("increment", "retry")
    )
    assert view["stage"] == "test_review"
    assert view["prompt"]["answer"]
    assert agent.calls[-1][1]["question"] == "Which criterion does this test cover?"
    assert not any(role == "implementer" for role, _, _ in agent.calls)


def test_missing_executable_is_an_unavailable_backend(rig, tmp_path):
    harness, _, _ = rig
    harness.agent = CommandAgent(Backend(command=[str(tmp_path / "missing")]))
    view = harness.start("missing", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["backend_failure"]["kind"] == "unavailable"


@pytest.mark.parametrize(
    "failure", [BackendResponseError("Invalid output", "not json", ""), RuntimeError("Failed")]
)
def test_a_failed_agent_cannot_change_files_and_then_get_an_automatic_correction(rig, failure):
    harness, agent, _ = rig
    original_source = (harness.repository / SOURCE).read_text()
    calls = []

    def invalid(role, context, cwd):
        calls.append(role)
        (cwd / SOURCE).write_text("-- Unexpected agent edit\n")
        raise failure

    harness.agent = invalid
    view = harness.start("changed", "Fix increment")
    assert view["stage"] == "agent_error"
    assert "changed the checkout" in view["prompt"]["error"]
    assert calls == ["interviewer"]
    assert not (harness.state_dir / "changed/proposals").exists()
    harness.agent = agent
    view = harness.respond("changed", "retry")
    assert view["stage"] == "agent_error"
    assert agent.calls == []
    (Path(view["worktree"]) / SOURCE).write_text(original_source)
    view = harness.respond("changed", "retry")
    assert view["stage"] == "specification_review"
    assert len(agent.calls) == 1


def test_provider_schema_failure_is_visible_in_task_status(rig):
    harness, _, _ = rig
    reason = "Invalid schema: coverage.properties is required"

    def fail(*args):
        raise BackendError(reason, backend="codex")

    harness.agent = fail
    view = harness.start("schema-error", "Fix increment")
    assert view["stage"] == "backend_blocked"
    assert view["prompt"]["provider_message"] == reason
    assert reason in view["outcome"]["detail"]
    assert "retry" in view["prompt"]["actions"]
