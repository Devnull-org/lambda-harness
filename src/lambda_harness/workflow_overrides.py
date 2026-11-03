"""Task-scoped workflow choices explicitly requested in the conversation."""

import copy
from pathlib import Path

from .schemas import WorkflowAction
from .workspace import save_json


class WorkflowOverrides:
    def _override_workflow(self, state, request):
        request = WorkflowAction.model_validate(request).model_dump()
        instruction = state.get("_workflow_message", "")
        if not instruction or request["user_instruction"] != instruction:
            raise ValueError("A workflow override must quote the current developer instruction")
        workspace = self._workspace(state)
        # Workflow choices cannot bless edits made outside the saved task state.
        expected = (
            state.get("phase_fingerprint")
            if state.get("next") == "end"
            else state.get("gate_fingerprint")
        )
        if expected and workspace.fingerprint() != expected:
            raise ValueError("Worktree changed outside this task; inspect it before continuing")
        if state.get("agent_integrity_fingerprint"):
            self._check_agent_checkout(state, workspace, state["agent_integrity_fingerprint"])
        skips = sorted(set(state.get("workflow_skips", [])) | set(request["skip"]))
        if not state.get("specification"):
            if "requirements" not in skips or not request.get("specification"):
                raise ValueError(
                    "Skipping requirements needs a working scope based on your instruction"
                )
            state["specification"] = request["specification"]
        self._worktree(state)
        state["workflow_skips"] = skips
        state.setdefault("workflow_overrides", []).append(copy.deepcopy(request))
        if request.get("automatic") is not None:
            state["mode"] = "full" if request["automatic"] else "phased"
        if "commits" in skips:
            state["commit_policy"] = "manual"
        if "walkthrough" in skips:
            state["walkthrough_enabled"] = False
        state.setdefault("test_paths", [])
        state.setdefault("expected_failures", [])
        state.setdefault("coverage", {})
        state.setdefault("approved_tests", workspace.test_manifest(state["test_paths"]))
        # No new coverage was approved: validate using the existing suite, without
        # pretending there is acceptance-test evidence for the requested change.
        if not state["coverage"]:
            state["test_strategy"] = "existing"
        for key in (
            "checking_baseline",
            "failed_reply",
            "retry_node",
            "baseline_plan",
            "baseline_resume",
            "baseline_resume_tests",
        ):
            state.pop(key, None)
        state["messages"].append(
            {
                "kind": "work_update",
                "agent_role": "interviewer",
                "answer": "Following your instruction. Skipping: "
                + ", ".join(s.replace("_", " ") for s in request["skip"])
                + ". Next: "
                + request["target"].replace("_", " ")
                + ".",
                "workflow_override": request,
            }
        )
        target = {
            "requirements": "interview",
            "code_review": "review_haskell",
            "security_review": "review_security",
            "handoff": "finish",
        }.get(request["target"], request["target"])
        if target in {"review_haskell", "review_security", "finish"}:
            self._prepare_override_reviews(state)
        state["selected_phase"] = target
        state["stage"], state["next"] = "workflow_directed", target
        state["prompt"], state["attempts"], state["patch_count"] = {}, 0, 0

    def _prepare_override_reviews(self, state):
        workspace = self._workspace(state)
        if state.get("review_fingerprint") != workspace.fingerprint():
            state.pop("haskell_reviewer", None)
            state.pop("security_reviewer", None)
        state["review_revision"] = workspace.revision
        state["review_fingerprint"] = workspace.fingerprint()
        state.setdefault("validation", {"status": "not_run", "commands": {}})

    def _skip_workflow_node(self, state, name):
        skips = state.get("workflow_skips", [])
        if name == "validate" and "validation" in skips:
            workspace = self._workspace(state)
            state["validated_fingerprint"] = workspace.fingerprint()
            state["validation"] = {
                "status": "skipped",
                "phase": state["phase"],
                "role": state["role"],
                "revision": workspace.revision,
                "commands": {},
                "reason": "Skipped by explicit developer instruction",
            }
            state["validations"].append(state["validation"])
            save_json(Path(state["report_directory"]) / "validation.json", state["validations"])
            self._validated(state)
            return True
        if (
            name == "implement"
            and "implementation" in skips
            and state.get("selected_phase") != "implement"
        ):
            state["next"] = "refactor"
            return True
        if name == "tests" and "test_writing" in skips and state.get("selected_phase") != "tests":
            state.setdefault("approved_tests", self._workspace(state).test_manifest())
            state.setdefault("test_paths", [])
            state.setdefault("expected_failures", [])
            state.setdefault("coverage", {})
            state.setdefault("test_strategy", "existing")
            state["next"] = "implement"
            return True
        if name == "refactor" and "refactor" in skips and state.get("selected_phase") != "refactor":
            self._prepare_override_reviews(state)
            state["next"] = "review_haskell"
            return True
        role, choice = {
            "review_haskell": ("haskell_reviewer", "code_review"),
            "review_security": ("security_reviewer", "security_review"),
        }.get(name, (None, None))
        if choice in skips:
            state[role] = {
                "summary": "Skipped by developer instruction; no review performed.",
                "findings": [],
                "revision": state["review_revision"],
                "fingerprint": state["review_fingerprint"],
                "skipped": True,
            }
            state["next"] = "review_gate"
            return True
        if name == "walkthrough" and "walkthrough" in skips:
            state["next"] = "finish"
            return True
        return False
