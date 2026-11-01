"""Classify backend failures without treating them as model replies."""

import json
import re


class AgentInterrupted(RuntimeError):
    """The developer stopped generation; no response may be applied."""

    def __init__(self):
        super().__init__("Model request stopped")


DESCRIPTIONS = {
    "quota": "reported a usage limit",
    "authentication": "requires authentication",
    "rate_limit": "is rate limited",
    "timeout": "did not finish before the timeout",
    "unavailable": "could not be started",
    "invalid_response": "returned an invalid response",
    "provider": "could not complete the request",
}

GUIDANCE = {
    "quota": (
        "The provider refused this request because of its usage allowance. Login alone "
        "does not confirm available usage. Retry after resolving the allowance, or select "
        "another backend. The harness does not retry quota failures automatically."
    ),
    "authentication": (
        "Sign in through this backend's CLI, then retry here. "
        "The harness will not start a login flow automatically."
    ),
    "rate_limit": "Wait for the provider's rate limit to clear, then retry here.",
    "timeout": "Inspect the saved diagnostics, then retry or increase --agent-timeout.",
    "unavailable": "Check the configured executable and its dependencies, then retry here.",
    "invalid_response": (
        "The response could not be validated. No proposal was applied. "
        "Inspect the saved diagnostics before retrying."
    ),
    "provider": "Inspect the saved provider diagnostics, resolve the issue, then retry here.",
}


def failure_kind(text):
    """Recognize explicit provider errors; leave unknown failures unclassified."""
    text = text.casefold()
    if re.search(r'"(?:loggedin|logged_in)"\s*:\s*false\b', text):
        return "authentication"
    patterns = {
        "quota": (
            "usage limit",
            "usage_limit",
            "insufficient_quota",
            "quota exceeded",
            "quota_exceeded",
            "credit balance is too low",
            "credit balance too low",
            "out of credits",
            "billing hard limit",
        ),
        "authentication": (
            "not logged in",
            "not authenticated",
            "authentication required",
            "unauthenticated",
            "invalid api key",
            "invalid_api_key",
            "token expired",
        ),
        "rate_limit": ("rate limit", "rate_limit", "too many requests"),
    }
    for kind, fragments in patterns.items():
        if any(fragment in text for fragment in fragments):
            return kind
    return "provider"


def provider_message(value):
    """Extract readable provider errors, including JSON nested in RPC messages."""
    for _ in range(8):
        if isinstance(value, dict):
            nested = value.get("message") or value.get("error")
            if nested is None:
                break
            value = nested
        elif isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                break
            if not isinstance(parsed, (dict, str)):
                break
            value = parsed
        else:
            break
    return str(value)[:2000]


class BackendError(RuntimeError):
    """Carry a provider failure and its raw diagnostics to the workflow."""

    def __init__(self, message, stdout="", stderr="", *, backend="embedded", kind="provider"):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr
        self.backend, self.kind = backend, kind
        self.diagnostics = None

    def prompt(self):
        return {
            "error": f"Backend {self.backend!r} {DESCRIPTIONS[self.kind]}. Your task is paused.",
            "guidance": GUIDANCE[self.kind],
            "backend_failure": {"backend": self.backend, "kind": self.kind},
            "diagnostics": self.diagnostics,
            "provider_message": provider_message(str(self)),
        }


class BackendResponseError(BackendError, ValueError):
    """Keep an invalid response available for correction and local diagnostics."""

    def __init__(self, message, stdout, stderr, *, backend="embedded"):
        super().__init__(message, stdout, stderr, backend=backend, kind="invalid_response")
