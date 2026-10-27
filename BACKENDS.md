# Choosing a backend and model

The workflow depends on an `Agent` callable, not a particular model service. A
backend receives a role, workflow context, and the target worktree. It returns a
validated proposal or review. Only the harness applies patches, runs checks, and
commits. All backend choices share the same developer approval gates.

The command transport supports agent CLIs and small bridges to hosted model APIs.
Prompts, response schemas, validation, process timeouts, and workflow persistence
remain in the harness. Adding a command profile does not require changing Python.

Large standalone reviews include a `review_changes` index alongside a partial inline
diff. Backends must provide read access to the local patch files named in that index
(under the task's report directory), as well as the worktree. A bridge to a hosted
model needs file-reading tools for this. The prompt explicitly requires reporting
unread changes; a partial preview is not a complete review. Small diffs stay inline.

## Choose on the website

The default is `default_backend = "auto"`. When work starts, the website checks
configured executables and login probes, then sends one short availability request
in a temporary Git directory. No project context or source is included. Generation
checks consume provider usage and time out after at most 60 seconds; login probes
have a separate 30-second timeout.

One usable backend is selected automatically. If several work, choose on the page.
If none work, the page explains the failures. **Project settings & context** lets
you select an explicit backend. **Check backends and resume** repeats discovery
for a saved task. Switching providers keeps the conversation and approval gates.

| Profile | Behavior |
| --- | --- |
| `local` | Saved-session CLI integration with read-only inspection. |
| `codex` | Saved CLI authentication, read-only shell access, ephemeral sessions, and native permission requests in task chat. |
| `claude-code` | Read/search tools, plan mode, structured output, and no inherited MCP servers. |

The default Nix launcher and development shell include all three providers.
Without Nix, install your chosen CLI separately; executables are resolved through
`PATH`. Sign in through the provider's normal login flow. The harness does not
perform login automatically or change your credentials.

Choose the backend in **Project settings** and the model in the conversation's
**Model for the next response** picker. There is no `--backend` startup option.

There are no per-provider harness packages or terminal workflow commands.
An explicit backend or named configuration default bypasses discovery.
Automatic selection applies one chosen profile to every role for the server session.
Use a named configuration default without an explicit backend choice on the website
to retain per-role routing.

The Codex web adapter uses the local CLI's
[app-server protocol](https://learn.chatgpt.com/docs/app-server) with ephemeral
sessions, a read-only sandbox, live web search, and `on-request` approvals directed
to the user. It passes the selected model explicitly and disables provider model
fallback. Native command, network, and additional-access requests appear in the
same task's conversation. Approve or deny returns a decision to the waiting model
turn; it does not start another generation. Grants apply to the pending request
(or the current turn for additional access), never a saved session-wide rule.
Stopping the model cancels its pending request. Direct file-edit requests are
rejected because the harness applies structured patches through its review gates.

Runtime access and patch approval are separate. Approving a patch does not change
network permissions. The provider may read public sources with its web tool without
needing shell network access. App-server loads the normal CLI configuration; the
harness explicitly sets its model, sandbox, approval policy, and reviewer for each
thread. Provider-enforced restrictions still apply.

Backend availability checks without a browser approval handler retain the one-shot
`exec` transport: `--ephemeral`, `--sandbox read-only`, `--ask-for-approval never`,
`--ignore-user-config`, and `--ignore-rules`. Both transports keep the existing home
directory and `CODEX_HOME` for authentication and omit API-key/access-token
overrides from the child environment. Personal gateway requirements need a custom
profile. The status probe checks login; it does not establish remaining quota.
Custom Codex profiles can opt into native approvals with
`approval_command = ["{executable}", "app-server", "--listen", "stdio://"]`.
This transport requires a CLI supporting the current app-server protocol; an
unsupported protocol reports a backend error rather than silently removing the
approval channel.

The Claude Code adapter uses headless structured output. LangGraph owns durable
workflow state, so switching backend does not require migrating provider sessions.

## User configuration

Pass a file with `--config FILE`, or create
`$XDG_CONFIG_HOME/lambda-harness/config.toml` (falling back to the current user's
`~/.config/lambda-harness/config.toml`). When this file is absent, an existing
configuration from the previous product name is reused. The file is optional; its parent directory
does not need to exist. A missing file explicitly selected with `--config` is an
error. Configuration is never automatically read from the target repository.

The same file accepts a `[validation]` table for the project's lint, build, Nix,
and test commands and its JUnit report pattern. See
[validation and commits](docs/VALIDATION.md) for defaults and examples.
New tasks save these settings so resuming them does not silently change their checks.

For example, use a different model for security reviews:

```toml
version = 1
default_backend = "claude-code"

[roles.security_reviewer]
model = "opus"
```

Choose a different backend for a role by adding `backend = "profile-name"` to that
role's table. Names are the prompt names under `src/lambda_harness/prompts`, excluding
`common`. Model IDs are passed to the selected backend without a hard-coded model
catalogue or a silent fallback.

Precedence is explicit:

1. A backend selected on the website applies to all roles for that session,
   overriding role-specific backend and model choices. A choice saved in Project
   settings is reused on future sessions.
2. Without a session or project backend choice, each role can override the configured
   default backend/model.
3. `--model` overrides the model for every role. `--agent-timeout` overrides profile timeouts.

A named profile in your configuration replaces that profile as a whole. Arguments,
credential rules, and response parsing from another provider are never merged into it.

## Connect another CLI or hosted service

For a command that reads a prompt on stdin and returns the role's JSON object:

```toml
default_backend = "team-model"

[backends.team-model]
command = ["team-agent", "--read-only", "--json"]
model_args = ["--model", "{model}"]
model = "your-model-id"
timeout = 600
```

Use real flags supported by your selected tool. Commands are argument arrays,
executed directly without a shell. Each invocation runs in the target checkout or
isolated worktree. The harness never runs a login command automatically.

Profile fields:

| Field | Meaning |
| --- | --- |
| `command` | Required executable and arguments; the executable is resolved through `PATH` or an explicitly configured path. |
| `input_format` | `prompt` (default) for plain prompt text; `json` for the bridge protocol below. |
| `response_fields` | Optional ordered envelope keys, such as `["structured_output", "result"]`. The first present, non-null field holds the response. With no keys, stdout is the response itself. |
| `model_args` | Arguments appended only when a model is selected. `{model}` is replaced with the exact model ID. |
| `model` | Optional default model ID for this profile. |
| `timeout` | Positive process timeout in seconds; default 600. |
| `unset_env` | Environment variable names to omit, for example to prevent API credentials from overriding a saved CLI login. No credential values belong here. |
| `probe` | Optional non-generating authentication/status command for discovery. `{executable}` uses the resolved backend executable. |
| `probe_reject_patterns` | Case-insensitive output strings that make a probe fail even if its exit code is zero. |

`command` and `model_args` also support the whole-argument tokens `{schema}` and
`{role}`. `{schema}` is the response schema as JSON. Tokens are replaced only when
they occupy an entire argument; literal JSON arguments are left intact. A prompt
backend with a selected model must configure how to pass it; it cannot silently
ignore a model override.

For a hosted API, use a bridge command. This keeps API credentials and provider
SDKs in that bridge's own environment:

```toml
default_backend = "service"

[backends.service]
command = ["company-model-bridge"]
input_format = "json"
model = "your-hosted-model"
```

The bridge reads one JSON request on stdin:

```json
{
  "version": 1,
  "role": "implementer",
  "model": "your-hosted-model",
  "prompt": "The complete role instructions and task context...",
  "schema": {"type": "object"},
  "context": {"request": "The requested change..."}
}
```

The real schema and context are supplied by the harness. The bridge writes exactly
one JSON object matching that schema to stdout, sends diagnostic logs to stderr,
and exits nonzero on failure. A bridge must provide read-only repository inspection
if its underlying model has no file tools: it runs in the selected worktree and can
read the files there. The harness does not automatically upload a repository to an
API or grant a plain language model file access.

General chat uses the `chat` role, configurable through `[roles.chat]`. Its response
contains `answer` (natural prose or Markdown) and `references` (repository paths
when relevant, otherwise an empty list). It has no specification or code changes.

Exploration uses the `explorer` role, configurable through `[roles.explorer]` like
other roles. Its response contains `summary`, `analysis` (completed Markdown
analysis), `references` (repository paths and lines), and `open_questions`.
It delivers findings and options; it does not produce a specification or file
changes. The harness supplies previous analysis and follow-up messages in context.

Envelope responses can contain a JSON object or JSON text in the configured field.
Introductory commentary followed by one JSON object, or one fenced JSON block with
surrounding commentary, is also accepted. For an unfenced response, the first
opening brace must begin the complete object and only whitespace may follow it;
the parser never falls back to an object nested inside malformed JSON. Multiple
objects or blocks are rejected. The extracted object must still match the complete
role schema. When the only validation errors are unexpected fields containing
`null`, `""`, `[]`, or `{}`, those fields are removed, their paths are reported,
and the result is validated again. Known fields are unchanged. Nonempty additions,
missing required fields, and type errors remain errors; `false` and `0` are not
treated as empty. Backend configuration remains strict as well.
The workflow requests one automatic correction after invalid JSON/schema output,
using the same role and backend with an additional `context.response_repair` object:
`validation_error` describes the failure and `previous_response` holds the invalid
output. Original context and human approval requirements remain in force. Bridges
using JSON input receive these fields in the versioned request's `context`.
Correction is skipped for output over 64 KB or combined context over 400 KB. Each invocation
has its own configured timeout. There is no automatic retry after a provider
failure, error envelope, process launch failure, or timeout.

Failures are saved in the task's `backend-responses` directory, including raw
stdout/stderr, partial output on timeout, and a `failure.json` record. A failed
correction uses an `attempt-2` subdirectory so it cannot overwrite the first
response. Before any correction, the workflow verifies that the checkout is
unchanged. A valid response continues to the original human gate; invalid output
never becomes a proposal.

The website shows progress while agents run. **Stop model** cancels a running
request. Unresolved failures pause at `backend_blocked` with the provider name,
reason, and diagnostic path. Categories include quota, authentication, rate limit,
timeout, unavailable executable, invalid response, and other provider errors.
Classification uses explicit error text; it does not estimate account balances.
After addressing the cause, retry on the page or select another backend. There
are no automatic provider fallbacks. Process groups are terminated on timeout;
changing backends does not weaken test protection or validation.

Configure required credentials through the selected CLI/service's normal mechanism.
All profiles remove inherited `ORCA_AGENT_*`, `ORCA_PANE_KEY`, `ORCA_TAB_ID`,
`ORCA_WORKTREE_ID`, and `ORCA_STRUCTURED_SESSION` variables from child processes.
This prevents desktop hooks from attributing a background agent to its parent's
session. It does not change the parent environment, saved login, or global hooks.
Backend session history may still be visible in desktop apps that scan it.
Other environment variables are inherited except for the profile's `unset_env`
list. Discovery verifies executable availability and runs the optional status probe;
without a probe it does not claim to have verified authentication or model access.

## Python integrations

Embedding applications may still pass any callable implementing `Agent` to `Harness`:

```python
def agent(role, context, cwd):
    # Invoke your runtime and return the schema-shaped proposal/review dictionary.
    ...
```

`CommandAgent` implements that protocol for one profile. `RoutedAgent` selects
profiles and models by role. For an auto configuration, call
`discovery.select_backend(agent, cwd)` before invoking it.
`Harness` independently validates the returned object,
checks the worktree, and enforces approval gates regardless of transport.
