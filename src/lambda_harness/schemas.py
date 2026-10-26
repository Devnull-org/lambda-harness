"""Validate agent handoffs before they can affect workflow state."""

import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ValidationChecks(Record):
    lint: list[str] = Field(min_length=1)
    build: list[str] = Field(min_length=1)
    nix: list[str] = Field(min_length=1)
    test: list[str] = Field(min_length=1)
    junit_glob: str | None

    @model_validator(mode="after")
    def valid_commands(self):
        from .validation import ValidationConfig

        ValidationConfig.model_validate(self.model_dump())
        return self


class TestSuiteKnowledge(Record):
    package: str
    suites: list[str] = Field(min_length=1)
    framework: str = Field(min_length=1)
    execution: str = Field(min_length=1, description="Runner, prerequisites, and suite scope.")


class ValidationSource(Record):
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ValidationDiscovery(Record):
    summary: str = Field(min_length=1)
    suites: list[TestSuiteKnowledge]
    sources: list[ValidationSource] = Field(min_length=1)
    checks: ValidationChecks | None
    questions: list[str]


class Criterion(Record):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Specification(Record):
    problem: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    acceptance_criteria: list[Criterion] = Field(min_length=1)
    out_of_scope: list[str]
    constraints: list[str]
    packages: list[str] = Field(min_length=1)
    open_questions: list[str]
    assumptions: list[str]


class Interview(Record):
    specification: Specification
    questions: list[str]


class Exploration(Record):
    summary: str = Field(min_length=1, description="A direct answer to the research question.")
    analysis: str = Field(
        min_length=1,
        description="Completed analysis in Markdown: findings, concrete options, and trade-offs.",
    )
    references: list[str] = Field(
        description="Repository paths and line numbers supporting findings."
    )
    open_questions: list[str] = Field(
        description="Remaining uncertainties, without requiring a choice."
    )


class Change(Record):
    path: str = Field(min_length=1, max_length=1024)
    content: str | None = Field(description="Complete UTF-8 file content; null deletes the file.")


class Patch(Record):
    summary: str = Field(min_length=1)
    changes: list[Change]
    commit_message: str = Field(min_length=1, max_length=2000)
    complete: bool = True
    permission_request: str | None = Field(
        default=None,
        min_length=1,
        description="Reason to ask the developer before applying this exact patch; null otherwise.",
    )


class Tests(Record):
    summary: str = Field(min_length=1)
    changes: list[Change] = Field(min_length=1)
    expected_failures: list[str] = Field(min_length=1)
    coverage: dict[str, list[str]]
    commit_message: str = Field(min_length=1, max_length=2000)


class FeatureTests(Tests):
    changes: list[Change] = Field(
        description="Test changes, or [] when the scope needs a developer decision."
    )
    expected_failures: list[str] = Field(
        description="Named failures for a test patch; [] when no patch is proposed."
    )

    @model_validator(mode="after")
    def require_failures_for_changes(self):
        if bool(self.changes) != bool(self.expected_failures):
            raise ValueError(
                "A test patch needs named failures; a scope decision must contain neither"
            )
        return self


class Finding(Record):
    id: str = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low", "info"]
    location: str = Field(min_length=1)
    title: str = Field(min_length=1)
    conditions: str = Field(min_length=1)
    sequence: list[str]
    impact: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    confidence: Literal["confirmed", "suspected"]
    recommendation: str = Field(min_length=1)
    needs_test: bool


class PassingTests(Tests):
    expected_failures: list[str] = Field(max_length=0)


class Review(Record):
    findings: list[Finding]
    summary: str = Field(min_length=1)


class Question(Record):
    question: str = Field(min_length=1)
    reference: str = Field(min_length=1)


class Walkthrough(Record):
    questions: list[Question] = Field(min_length=3, max_length=5)


class Feedback(Record):
    feedback: str = Field(min_length=1)
    follow_up: str | None


class SuggestedCommand(Record):
    explanation: str = Field(min_length=1)
    command: str = Field(min_length=1)


class Explanation(Record):
    answer: str = Field(min_length=1)
    suggested_commands: list[SuggestedCommand] = Field(default_factory=list, max_length=8)


class RecoveryReply(Explanation):
    next_action: Literal["retry", "recheck", "continue_without_commits"] | None = None


class CheckCommand(Record):
    name: str = Field(min_length=1, max_length=120)
    argv: list[str] = Field(min_length=1, max_length=100)
    directory: str = "."
    environment: Literal["project", "nix"] = "project"
    purpose: str = Field(min_length=1)
    modifies_files: bool = False

    @model_validator(mode="after")
    def valid_command(self):
        from pathlib import PurePosixPath

        path = PurePosixPath(self.directory)
        if (
            path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or "\\" in self.directory
            or "\x00" in self.directory
            or not self.directory
        ):
            raise ValueError("Check directory must stay inside the worktree")
        if any(not arg or "\x00" in arg for arg in self.argv):
            raise ValueError("Command arguments must be nonempty text without NUL bytes")
        return self


class CheckPlan(Record):
    summary: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1, max_length=100)
    coverage: list[str] = Field(min_length=1, max_length=100)
    commands: list[CheckCommand] = Field(max_length=50)
    omissions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_plan(self):
        if not self.commands and not self.questions:
            raise ValueError("Provide commands or ask about missing prerequisites")
        if len({command.name for command in self.commands}) != len(self.commands):
            raise ValueError("Check names must be unique")
        checking = False
        for command in self.commands:
            if command.modifies_files and checking:
                raise ValueError("Run formatters before verification commands")
            checking |= not command.modifies_files
        return self


class PatchConversation(Explanation):
    proposal: Patch | None = Field(
        default=None,
        description="Complete patch when the developer requests changes; null for an "
        "informational question. When revising a pending current_proposal, retain its "
        "other still-needed changes.",
    )

    @model_validator(mode="after")
    def require_changes_for_revision(self):
        if self.proposal is not None and not self.proposal.changes:
            raise ValueError("A patch revision must include the revised file changes")
        return self


class ChatReply(Explanation):
    memory_note: str | None = Field(
        default=None,
        min_length=1,
        max_length=32_000,
        description="A standalone note to append to project memory, only when the latest user "
        "message explicitly requests saving or remembering it. Otherwise null. "
        "The harness performs the save and reports its result; do not claim it has saved yet.",
    )
    references: list[str] = Field(
        description="Repository references when relevant, otherwise empty."
    )


AgentRole = Literal[
    "test_runner",
    "chat",
    "explorer",
    "interviewer",
    "test_author",
    "implementer",
    "refactorer",
    "reviewer",
    "security_reviewer",
    "documentation_reviewer",
    "walkthrough",
]


class AgentDefinition(Record):
    name: str = Field(min_length=1, max_length=120)
    avatar: Literal["lambda", "robot", "fox", "owl", "crab", "shield"] = "robot"
    role: AgentRole
    languages: list[str] = Field(max_length=12)
    expertise: list[str] = Field(min_length=1, max_length=20)
    instructions: str = Field(min_length=1, max_length=20_000)


class AgentDraft(Explanation):
    definition: AgentDefinition | None = Field(
        description="The proposed reusable agent, or null while its purpose is unclear."
    )


SCHEMAS = {
    "test_runner": CheckPlan,
    "validation_discovery": ValidationDiscovery,
    "agent_builder": AgentDraft,
    "chat": ChatReply,
    "interviewer": Interview,
    "explorer": Exploration,
    "test_author": Tests,
    "implementer": Patch,
    "refactorer": Patch,
    "haskell_reviewer": Review,
    "security_reviewer": Review,
    "documentation_reviewer": Review,
    "walkthrough": Walkthrough,
    "walkthrough_feedback": Feedback,
}

REVIEWERS = {
    "haskell": ("haskell_reviewer",),
    "security": ("security_reviewer",),
    "documentation": ("documentation_reviewer",),
    "both": ("haskell_reviewer", "security_reviewer"),
    "all": ("haskell_reviewer", "security_reviewer", "documentation_reviewer"),
}


def schema_for(role, context):
    if context.get("intent") == "recover_operation":
        return RecoveryReply
    if context.get("intent") in {"discuss_patch", "discuss_implementation"} and role in {
        "implementer",
        "refactorer",
    }:
        return PatchConversation
    if context.get("intent") == "explain":
        return Explanation
    if role == "test_author" and context.get("task_kind", "feature") == "feature":
        return FeatureTests
    if (
        role == "test_author"
        and context.get("task_kind") == "tests"
        and context.get("test_expectation") == "green"
    ):
        return PassingTests
    return SCHEMAS[role]


def validate_response(role, context, value, *, notify=None):
    """Discard only empty unexpected fields; keep all other validation strict."""
    schema = schema_for(role, context)
    try:
        validated = schema.model_validate(value)
    except ValidationError as error:
        errors = error.errors()
        if not all(
            item["type"] == "extra_forbidden"
            and (
                item["input"] is None
                or isinstance(item["input"], (str, list, dict))
                and len(item["input"]) == 0
            )
            for item in errors
        ):
            raise
        cleaned = copy.deepcopy(value)
        paths = []
        for item in errors:
            parent = cleaned
            for part in item["loc"][:-1]:
                parent = parent[part]
            del parent[item["loc"][-1]]
            paths.append(".".join(map(str, item["loc"])))
        validated = schema.model_validate(cleaned)
        if notify:
            notify("Ignored empty extra response fields: " + ", ".join(paths))
    return validated.model_dump(exclude_unset=True)
