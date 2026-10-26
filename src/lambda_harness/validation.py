"""Interpret Cabal test evidence separately from lint, build, and Nix failures."""

import re
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from string import Formatter
from xml.etree import ElementTree

from pydantic import Field, model_validator

from .process import ProcessError, run_process
from .schemas import Record


class ValidationConfig(Record):
    lint: list[str] | None = Field(default=None, min_length=1)
    build: list[str] = Field(
        default_factory=lambda: [
            "cabal",
            "build",
            "all",
            "--enable-tests",
            "--ghc-options=-Werror",
        ],
        min_length=1,
    )
    nix: list[str] = Field(default_factory=lambda: ["nix", "flake", "check"], min_length=1)
    test: list[str] = Field(
        default_factory=lambda: ["cabal", "test", "{package}", "--test-show-details=direct"],
        min_length=1,
    )
    junit_glob: str | None = None

    @model_validator(mode="after")
    def valid_commands(self):
        for command in (self.lint, self.build, self.nix, self.test):
            if command is None:
                continue
            if any(not arg or "\x00" in arg for arg in command):
                raise ValueError("Validation arguments must be nonempty text without NUL bytes")
        if not any("{package}" in arg for arg in self.test):
            raise ValueError("The test command must include {package}")
        for template in [*(self.lint or []), *self.nix]:
            if any(field is not None for _, field, _, _ in Formatter().parse(template)):
                raise ValueError("Placeholders are supported only in build, test, and junit_glob")
        for template in self.build:
            if template != "{package}" and any(
                field is not None for _, field, _, _ in Formatter().parse(template)
            ):
                raise ValueError("Build placeholders must be the whole argument {package}")
        for template in [*self.test, *([self.junit_glob] if self.junit_glob else [])]:
            for _, field, spec, conversion in Formatter().parse(template):
                if field not in {None, "package"} or spec or conversion:
                    raise ValueError("Only the {package} placeholder is supported")
        pattern = self.report_pattern("example")
        if pattern is None:
            return self
        path = PurePosixPath(pattern)
        if (
            not pattern
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or "\\" in pattern
            or "\x00" in pattern
        ):
            raise ValueError("JUnit report pattern must stay inside the worktree")
        return self

    def commands(self, packages):
        if self.lint is None:
            raise ValueError("Inspect the project's validation setup before running checks")
        build = [
            value for arg in self.build for value in (packages if arg == "{package}" else [arg])
        ]
        commands = [("lint", self.lint), ("build", build), ("nix", self.nix)]
        return commands + [
            (f"test:{package}", [arg.format(package=package) for arg in self.test])
            for package in packages
        ]

    def report_pattern(self, package):
        return self.junit_glob.format(package=package) if self.junit_glob is not None else None


def test_evidence(results, *, allow_cabal=False):
    failures, errors, executed, identities = [], [], 0, set()
    suites_executed = 0
    for name, result in results.items():
        if not name.startswith("test:"):
            continue
        xml_reports = result.get("junit_xml", [])
        if not xml_reports:
            if allow_cabal and result["exit_code"] == 0:
                # Cabal's suite summary proves a green run, but cannot identify red assertions.
                summaries = re.findall(
                    r"^(\d+) of (\d+) test suites \((\d+) of (\d+) test cases\) passed\.$",
                    result["stdout"],
                    re.MULTILINE,
                )
                if summaries and all(
                    int(passed) == int(total) > 0 and int(cases) == int(count) > 0
                    for passed, total, cases, count in summaries
                ):
                    suites_executed += sum(int(total) for _, total, _, _ in summaries)
                    continue
                raise ValueError(f"No complete passing Cabal test summary for {name}")
            raise ValueError(f"Missing JUnit evidence for {name}")
        command_failures = []
        command_executed = 0
        for xml in xml_reports:
            try:
                root = ElementTree.fromstring(xml)
            except ElementTree.ParseError as error:
                raise ValueError(f"Malformed JUnit evidence for {name}") from error
            for suite in root.iter():
                if suite.tag not in {"testsuite", "testsuites"}:
                    continue
                if int(suite.get("errors", "0")):
                    raise ValueError(f"JUnit suite reports runner errors for {name}")
                declared = int(suite.get("failures", "0"))
                if declared > sum(
                    case.find("failure") is not None for case in suite.iter("testcase")
                ):
                    raise ValueError(f"JUnit suite omits failing cases for {name}")
            for case in root.iter("testcase"):
                if case.find("skipped") is not None:
                    continue
                executed += 1
                command_executed += 1
                identifier = ".".join(
                    part for part in (case.get("classname", ""), case.get("name", "")) if part
                )
                if not identifier:
                    raise ValueError("JUnit testcase has no identity")
                identities.add(identifier)
                if case.find("error") is not None:
                    errors.append(identifier)
                if case.find("failure") is not None:
                    command_failures.append(identifier)
        if not command_executed:
            raise ValueError(f"No tests executed for {name}")
        if result["exit_code"] != 0 and not command_failures:
            raise ValueError(f"{name} failed without identified assertion failures")
        failures.extend(command_failures)
    return {
        "failures": sorted(set(failures)),
        "errors": errors,
        "executed": executed,
        "suites_executed": suites_executed,
        "identities": sorted(identities),
    }


def classify_validation(results, expected_failures, *, phase, allow_cabal=False):
    if phase not in {"red", "green"}:
        raise ValueError("Validation phase must be red or green")
    if not {"lint", "build", "nix"} <= results.keys() or not any(
        name.startswith("test:") for name in results
    ):
        return "blocked"
    if any(results[name]["exit_code"] != 0 for name in ("lint", "build")):
        return "blocked"
    try:
        evidence = test_evidence(results, allow_cabal=allow_cabal and phase == "green")
    except ValueError:
        return "blocked"
    if evidence["errors"] or not (evidence["executed"] or evidence["suites_executed"]):
        return "blocked"
    if phase == "green":
        return (
            "green"
            if not evidence["failures"] and all(r["exit_code"] == 0 for r in results.values())
            else "blocked"
        )
    if not expected_failures or set(evidence["failures"]) != set(expected_failures):
        return "blocked"
    return "expected_red" if results["nix"]["exit_code"] == 0 else "needs_red_review"


class ValidationInterrupted(RuntimeError):
    """Keep partial check output without treating a stop as a failed test."""

    def __init__(self, argv, error):
        super().__init__("Validation stopped")
        self.result = {
            "argv": argv,
            "exit_code": None,
            "stdout": error.stdout,
            "stderr": error.stderr,
            "cancelled": True,
        }


class LocalExecutor:
    def __init__(self, *, nix_flake=None, timeout=3600, cancel=None, cancellation=None):
        self.nix_flake = str(Path(nix_flake).expanduser().resolve()) if nix_flake else None
        self.timeout = timeout
        self.cancel = cancel
        self.cancellation = cancellation

    def __call__(self, argv, cwd, *, report_glob=None, use_nix=True, project_root=None):
        actual = (
            ["nix", "develop", self.nix_flake or str(project_root or cwd), "--command", *argv]
            if use_nix
            else argv
        )
        started = time.time_ns()
        scope = (
            self.cancellation.request(kind="validation")
            if self.cancellation
            else nullcontext(self.cancel)
        )
        try:
            with scope as cancel:
                code, stdout, stderr = run_process(actual, cwd, timeout=self.timeout, cancel=cancel)
        except ProcessError as error:
            if error.kind == "cancelled":
                raise ValidationInterrupted(actual, error) from error
            raise
        reports = []
        if report_glob:
            for path in sorted(cwd.glob(report_glob)):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(cwd.resolve())
                    and path.stat().st_mtime_ns >= started
                ):
                    reports.append(path.read_text())
        return {
            "argv": actual,
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
            "junit_xml": reports,
        }
