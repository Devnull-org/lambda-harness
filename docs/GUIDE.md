# Lambda Harness user guide

The website is the interface for conversations, approvals, phases, saved work,
backend selection, and project memory. Open **Workflow guide** in the sidebar
for an in-app walkthrough.

The sidebar names the **active project** and shows its checkout path. **Memory**
and **Settings** belong to that project, with separate pages at
`/projects/<project-id>/memory` and `/projects/<project-id>/settings`. The project ID
uses the resolved checkout path, so repositories with the same name remain distinct.
Old `/project-memory` and `/project-settings` links still open the active project's
page. A bookmark for a different project is not silently opened against the active one.
Click the project name to collapse or expand the sidebar menu, including saved
work and the shared Agents link.

Open **Projects** to add an existing local Git checkout, open it, or remove it
from the list. The list survives server restarts. Use **Switch project** in the
sidebar to change projects on the same server. Each browser tab stays attached to
its own project, including while work is running in another tab.

Removing a project requires confirmation and is blocked while that project's work
is running. Removal only hides the entry: source files, saved tasks, memory,
settings, and worktrees remain on disk. Re-add the same checkout to return to them.
Removing the last project leaves the Projects page ready for another checkout.

**Agents** is a shared catalog. Definitions can be reused across projects; agent
assignments, conversations, and memory remain project-specific. Its URL includes
the project when opened from the sidebar so assignments retain their context.
The old `/agents` link still uses the launcher's initial project, when supplied.

The project's **Settings** page saves its preferred backend across restarts.
Choose backends and response models on the website; there is no `--backend` startup
option. With no project preference, the user configuration
supplies the default. Automatic backend discovery chooses a backend for the session
without overwriting the project's preference. Provider definitions and logins are
reusable across projects. If a saved backend is removed from your configuration,
the page explains the fallback and lets you select a replacement.

Use **Workspace**, a saved task, or your browser's Back button to return to a
conversation. Switching pages within a project keeps unsent messages and memory
drafts. Switching projects opens a fresh page; the browser warns about unsent text
before leaving. Refreshing or closing the tab can still discard unsaved text.

## Start the local server

From any directory, with Nix and Git installed:

```sh
nix run --refresh github:Devnull-org/lambda-harness -- \
  --state-dir ~/.local/state/lambda-harness
```

`--state-dir` is required on every launch. Choose a persistent directory outside
your project checkouts for the project list, conversations, memory, logs, and worktrees.
The path above is an example, not an automatic default. If you already have saved
work, use its existing state directory. The server prints the resolved path at startup.

Open the printed URL, usually `http://127.0.0.1:8765`, and add a checkout from
**Projects**. To register and open a particular checkout at launch, append
`--repo "/path/to/project"`. Keep the server process running. Closing the browser
leaves active work running. Ctrl+C in the server's terminal stops the server and
cancels active work while keeping submitted messages and completed steps saved.

If an assistant started the server for you, it can be running in the assistant's
background session even when your terminal is idle. The browser connects to that
local process; it cannot operate on the repository without the server.

There is no terminal workflow client or separately installed `lambda-harness`
command. Nix supplies a small website launcher. Task names and responses belong
in the page, with no `serve`, `start`, or `resume` subcommand in the launch line.

### From a clone

```sh
git clone git@github.com:Devnull-org/lambda-harness.git
cd lambda-harness
nix run . -- --state-dir ~/.local/state/lambda-harness --repo "/path/to/project"
```

Nix resolves `.` before reading `--repo`. It must point at the harness clone's
flake. From elsewhere, use the GitHub reference or an absolute path to the clone.
A `could not find a flake.nix file` error occurs before the harness starts.

### Without Nix

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), clone
the repository, and run:

```sh
uv run --locked --no-dev python -m lambda_harness \
  --state-dir ~/.local/state/lambda-harness --repo "/path/to/project"
```

uv installs the pinned Python dependencies and starts the same local website.
Install your chosen model provider's CLI separately and sign in using its normal
login flow. Nix is optional for exploration, interviews, and reviews; code validation needs
the target project's Nix environment.

### Server options

All options go after `--` with `nix run`. There are no workflow subcommands.

| Option | Purpose |
| --- | --- |
| `--repo PATH` | Optional initial Git checkout. Registers it and opens it by default. Omit to start on Projects. |
| `--agents-repo PATH` | Git checkout containing `agents/*.json`; overrides the saved catalog selection. Source launches detect the harness checkout. Installed packages otherwise show the bundled catalog. |
| `--port PORT` | Loopback port, default 8765; 0 chooses a free port. |
| `--tailscale` | Start private HTTPS access for the Tailscale account signed in on this computer. Requires Tailscale installed and connected. |
| `--tailscale-port PORT` | Tailscale HTTPS port, default 443. Choose another, such as 8443, if already used; requires `--tailscale`. |
| `--state-dir PATH` | **Required.** Project list, conversations, memory, logs, and worktrees; outside all registered checkouts. Reuse the same path on every launch. |
| `--config FILE` | User-owned backend and validation configuration. |
| `--nix-flake PATH` | Dependency flake override for the initial `--repo` only; other projects use their own task worktrees. Requires `--repo`. |
| `--model ID` | Model override for all agents. |
| `--agent-timeout SECONDS` | Override the backend request timeout. |
| `--check-timeout SECONDS` | Validation command timeout, default 3600. |
| `--unsigned-commits` | Disable signing for harness commits only. |
| `--help` | Describe server startup options. |

Existing tasks keep their worktrees and checkpoints. Storage is never selected,
moved, or merged automatically. Keep passing `--state-dir /path/to/existing/state-root`
and any existing `--config` override. The previous default configuration and browser
theme are still reused when no new setting exists.
Keep the same target checkout path; do not move registered worktrees. Restart the
server to load an updated version. Keep the same state root for your registered
project list. Explicitly launching with `--repo` registers that checkout again even
if you previously removed it; omit `--repo` to keep it out of the list.

## Work from your phone

Install and connect Tailscale on the computer and phone using the same account.
Then start the harness with:

```sh
nix run --refresh github:Devnull-org/lambda-harness -- \
  --state-dir ~/.local/state/lambda-harness --tailscale
```

Keep your usual `--repo` and `--state-dir` options when updating an
existing launch. Open the printed **Phone** HTTPS address in your phone's browser,
or scan the QR code under **Open on your phone** in the website's sidebar. The QR
code is generated locally and contains only the HTTPS address. You can add it to
your phone's home screen. Expand the project name to navigate on a small screen.
Both screens use the same tasks, messages, approvals, and project memory; code and
agents run on the computer. Keep it awake with the harness and Tailscale running.
Closing the phone's browser does not stop an active task.

The launcher discovers the existing Tailscale CLI, including the macOS app's bundled
CLI. If Tailscale asks you to enable HTTPS, follow the printed Tailscale setup link
and launch again. If port 443 is occupied, add `--tailscale-port 8443`; existing
Serve services are not replaced. Stopping the harness removes its temporary Serve
session. To return to local-only access, launch without `--tailscale`.

Access requires the same Tailscale user account as the computer. The harness checks
Serve's identity headers before serving its page or API, and still validates browser
origins and session tokens. Other tailnet users, tagged devices without a user identity,
and public Funnel requests are rejected. The app listens on loopback and uses
[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) as its HTTPS proxy.
This connects your devices to this computer's workspace; use handoff export/import
when transferring work to another developer.

## Your agent catalog

Open **Agents** in the sidebar, or visit `/agents`. Browse portraits, roles,
languages, and expertise. **View agent** shows the full instructions. Search by
name, language, or expertise, and filter by role.
Each agent gets a distinct portrait from its saved ID and chosen avatar style.
It stays consistent across projects, repository clones, and name changes.

**Add agent** starts a guided chat. Describe the help you need; you do not need to
know the language's best practices. The builder proposes suitable expertise,
explains tradeoffs, and asks focused questions. Ask for refinements, including a
different avatar, until the definition fits. **Save to repository** writes the
reviewed definition. **Edit** opens a new conversation about an existing agent;
its saved definition remains unchanged until you save the revision. You can stop
model generation and revise instructions, just as in other conversations.

Definitions live in `agents/*.json` in a Git checkout, including the avatar name.
The harness repository includes Haskell specialists, language-neutral roles, and
a Rust reviewer. To make changes when launching directly from GitHub, clone it:

```sh
git clone git@github.com:Devnull-org/lambda-harness.git
```

On the Agents page, expand **Agent repository** and enter that clone's path.
You can also select another team's Git checkout containing an `agents/` directory.
An empty catalog is supported. When running from the harness source checkout,
it is detected automatically. The bundled catalog in an installed/Nix package
is read-only until you select a writable checkout.

Saving creates an ordinary working-tree change. Review the diff, commit it, and
push it or open a PR using your team's usual process. Teammates pull the repository
and refresh the catalog to receive updates. The website does not commit or push
agent definitions automatically. Git provides their history. Draft conversations
and project selections are local; definitions are not stored in a local database.

Use **Agents for this project**, or **Use for this project** on a profile, to select
an agent for a workflow role. New tasks pin complete definitions, including their
version and avatar. Editing a file or pulling an updated catalog does not change
existing tasks. A deleted or incompatible selected definition blocks new tasks
until you update the assignment. Role permissions, backend choice, approval gates,
and validation remain controlled by the harness.

The coding toolchain currently supports Haskell and Nix. Rust and other languages
can already have chat, exploration, and review specialists. You can draft coding
agents for other languages, but they cannot be assigned to test-writing,
implementation, or refactoring until a matching toolchain is supported.

Agent conversations appear separately on the Agents page, so they do not clutter
your project task list. **Save to repository** does not automatically assign a
new agent. Assignments are personal to this project; sharing definitions does not
force teammates to use them.

### Fresh context for each agent

Each workflow role gets its own conversation. Agents share the task request,
requirements, project memory, code, approved tests, and recorded check results.
They do not receive other agents' discussions, exploration conclusions, or patch
summaries. Findings you select for repair are passed to the coding agents.

Follow-up questions keep that agent's own conversation. Code, security, and
documentation reviewers cannot see one another's findings in their prompts; a
new review pass also leaves out earlier review conclusions. The website retains
the complete history for you. Older answers without a recorded author remain
visible but are left out of agent prompts when their owner cannot be determined.

This separates prompt context, not filesystem access. Agents still inspect the
same task worktree and are instructed not to look up other agents' reports.

## Choose a starting point

**General chat** is an ongoing conversation for questions, explanations, or brainstorming.
It uses the selected repository revision and saved project context when relevant,
without an interview, specification, or approval step. Send follow-up messages,
leave, and return to the saved chat whenever you like. **Stop model** and revised
instructions work here too. Chat does not edit files or start a coding workflow.
Its transcript is also saved as `chat.md` in the report directory.

After sending a question, a **Thinking…** indicator appears in the conversation
until the response arrives. It also shows sending, stopping, or connection loss.
Use **Stop model** beside **Send** below the chat to interrupt it.
Open **Activity log** below the message box for detailed progress.
Your conversation is saved; edit your instructions or retry when ready. During
validation, the same button becomes **Stop checks**. This saves partial logs and
pauses the task without triggering repairs. Use **Recheck** to run the checks again.

All task conversations support model selection, including explorations, interviews,
reviews, and the agent workshop. Use **Model for this reply** when starting a task,
or **Model for the next response** above the message box. The picker remains
available between phases and when retrying a stopped request. Choices come from
the selected backend's installed CLI.
With automatic backend selection, the list groups models by installed provider;
choosing one also selects that provider for the request. **Refresh models** reloads
the catalog after signing in or changing accounts. Listing models sends no chat prompt.

The picker keeps your choice within that conversation while the page is open.
General chat also records the backend and requested model with each reply, plus
the actual model when the provider reports it, and restores that choice on reopening.
Older replies without this information are left unlabelled. **Use configured default**
follows the launcher and backend configuration; a provider's **Default** option
clears the launcher model override for that reply. A catalog is not a quota or
entitlement check: providers can still refuse a request, and the saved chat remains
resumable. Missing catalogs do not prevent using the configured default.

**Explore** reads code and delivers findings, options, references, and follow-up
answers. It does not start coding. **Finish exploration** saves the result; you
can stop there or start a requirements interview later.

**Plan a feature or fix** starts an interview. Answer questions and revise the
specification until you are satisfied, then explicitly approve it.
Each interviewer response stays between your replies in the conversation,
including earlier questions and specification drafts. Older task responses are
restored from saved journals when available, without rerunning the model.

Choose a unique task name and a base branch, tag, or commit before starting.
Task names can contain spaces, such as **Review documentation**. Use up to 64
letters, digits, spaces, underscores, or hyphens; start with a letter or digit
and leave no spaces at either end. The name stays visible in saved work and
handoffs; the harness replaces spaces with hyphens in its Git branch name.
Each task gets its own worktree from that exact commit. Uncommitted changes in
your original checkout stay there. Local and remote-tracking branch names must
already exist in your checkout; GitHub PR URLs are fetched as described below.

Open a saved task to see its **Task worktree** panel above the conversation.
Use **Copy path** to open that folder in your editor or terminal. This is where
the agent's files live, on the computer running the harness. The path stays
available when a task is paused or finished.

### Review an existing change

Choose **Review a PR or branch**. Enter a branch, commit, or GitHub PR URL and
choose **Code**, **Security**, **Documentation**, **Code + security**, or all three.
The code reviewer uses the
agent selected for this project, with a Haskell reviewer provided by default. Add a focus if you want to
direct their attention. No requirements interview or code-writing phase is needed.

The documentation reviewer checks README files, guides, API docs, examples, and
comments against the implementation. It looks for inaccurate claims, missing setup
steps, stale commands, broken local references, and confusing explanations. It can
also identify missing documentation for changed behavior. It reads examples without
running them and reports concrete reader impact rather than cosmetic preferences.
Choose or create a specialist for the **Documentation reviewer** role on **Agents**.
These review choices apply to standalone reviews; the feature workflow retains its
code and security review steps.

For a local branch or commit, select its comparison base (usually the target
branch). The review uses the diff from their merge base to the selected head,
excluding unrelated changes made later on the base branch. Both revisions are
pinned when the task starts; the review worktree contains the head revision.

A GitHub PR URL includes its title and description. Leave the comparison base
empty to use the PR's target branch, or enter a local branch, remote-tracking ref,
tag, or commit to override it. This changes only the review comparison, not the PR.
It must belong to a GitHub remote configured in the selected project. The harness
reads metadata through `gh` and fetches the base and PR head without changing
branches or working files. The Nix launcher includes `gh`; without Nix, install
the GitHub CLI separately. Use its existing login (`gh auth login` if needed),
and ensure Git can read the project's remote. URL support is currently for
`github.com`; for other hosts, fetch the branch yourself and use its local ref.

Read the findings, ask follow-up questions, or revise the review's focus.
For large reviews, the harness keeps the complete diff and an index of per-file
patches in the task's reports. The prompt includes a compact preview; agents read
the remaining patches as needed. Large diagrams are not silently dropped. The
review summary must state any files or visual details the reviewer could not inspect.
**Finish review** saves the result; it does not fix code, run builds, post comments,
or approve the PR on GitHub. Reports include `review.md` and `review.json`.
To publish a finding, choose **Post comment on GitHub** on its card. This explicit
request posts that finding as an inline comment using your existing `gh` login.
The harness checks that the PR still has the reviewed head and that the line is
in its diff. It records the returned link and checks for an existing comment on
retry. It does not submit an approval or a request-changes review.

**Check documentation links** checks links and anchors in changed Markdown, MDX,
and HTML files at the pinned revision. It uses lychee, included in the Nix launcher
(install it separately without Nix). Results appear in task history and
`documentation-links.json`. HTTP 404/410 responses and missing anchors are distinguished
from access restrictions, timeouts, and site routes that need a rendered-site check.
Missing source files need verification because a docs build may generate them. A
GitHub link to a new file in this PR may also start working only after merging.
This does not build the site, execute repository scripts, or use repository lychee
configuration. Private addresses, credentials in URLs, and paths outside the worktree
are excluded. It checks source links, so it cannot establish that generated routes
and JavaScript-driven pages work.

**Start a fresh attempt** reviews the same pinned revision. To review newer commits,
create a new task with the updated branch or PR URL.

### Write tests without implementation

Choose **Write tests**. Select a base, name the Cabal packages (comma-separated),
describe the test scope, and list behaviors to test, one per line. Choose:

- **Failing regression test (TDD)** for a bug or behavior that is not implemented yet.
- **Passing coverage for existing behavior** to cover code that already works.

The test author writes tests, fixtures, and necessary Cabal test wiring. Lint,
strict compilation, Nix checks, and Cabal tests run before your review. Named tests
must appear in fresh JUnit evidence; passing unrelated tests is not enough.
Use **Ask a question**, **Revise instructions**, or **Run checks again**, then
**Approve tests** to commit the validated patch. Approval finishes this task.
There is no implementation phase attached to it. Inspect the worktree and use your
normal Git process to take the commit into your project.

## Continue at your pace

After specification approval, choose the step that fits the task:

- **Explore** delivers an assessment. **Requirements interview** revisits the scope.
- **Write failing tests** follows TDD. Ask questions and approve the exact tests
  before choosing **Implement**.
- **Implement using existing tests** or **Refactor using existing tests** reuses
  current coverage for changes that do not need new tests. The harness first runs
  the existing suite, lint, strict compilation, and Nix checks. All must pass before
  coding, and every subsequent patch must stay green. Existing tests remain protected.

Coding agents can request permission for a proposed patch by explaining why a
decision is needed. Changes to tests, protected Cabal settings, or build and
validation configuration automatically request permission. A card in the conversation shows the reason,
affected files, and complete proposed diff before any part of the patch is applied.
Choose **Approve changes and continue** to apply that exact patch and validate it,
**Deny and revise** to continue without the denied changes, or **Request changes**
to give the coding agent revised instructions. **Ask a question** also accepts edits:
ask it to add comments, rename something, or otherwise change the proposal, and the
agent returns a revised patch in the diff preview. Informational questions receive
an explanation without changing the proposal. Neither approves the patch; review
the latest diff before approving it. This also works for previously saved blocked tasks.
Approval applies only to that patch; changed worktrees invalidate it and later
protected changes need another decision. Main-library `build-depends` edits can
proceed without a permission stop. Passing baseline checks do not mean a proposed
implementation has been applied or tested.

Implementation reports applied patches and validation results in the conversation.
If automatic repairs reach their limit, the task pauses with the failed checks,
diagnostics, and an attention bell. Use **Ask a question** to ask what happened or
request another fix. Repair requests continue from the current files and run
validation again; protected changes still require approval. Ordinary questions
leave the work and retry count unchanged. Existing blocked tasks support this
flow without restarting the task. **Run checks again** only validates the current
files; it does not ask the coding agent to change them first.

If the test author concludes that no test patch is appropriate, its explanation
appears as a scope decision. Ask questions, revise the request, or use **Choose
another step**. This also works for older failed test-generation attempts when
no patch was applied and the task worktree is clean. It never starts coding
automatically or discards proposed test changes. Assessment-only requirements
should go to exploration or a new interview before requesting implementation.

After implementation, choose **Refactor**, **Code review**, and **Security review**.
Resolve findings before handoff.

The optional **Code walkthrough** asks questions about the finished code. You can
require it when starting a task, or choose it later. **Prepare local handoff**
writes reports and a PR description; it does not push or open a PR.

Enter adds a newline. **Send** or Ctrl/Command+Enter sends the message. Use **Stop
model** to interrupt generation, **Edit previous message** to revise it, or
**Retry unchanged** to repeat it. Only an approval button approves work.

Codex can also request runtime access, such as permission to read a network
source or run a command outside its default sandbox. The request appears in this
task's conversation with the reason, command or destination, and requested access.
Choose **Approve request** or **Deny request** to let the same model turn continue.
The permission bell rings when a new request arrives, including in another task.
**Stop model** cancels a pending request. These decisions do not approve a code
patch, and approving a patch does not grant runtime access. No permanent access
rule is saved. Other providers without a native approval connection still report
their restrictions as part of the response.

Saved tasks appear in the sidebar. **Resume** returns to an unfinished phase or
its pending gate. **Start a fresh attempt** preserves the old task and creates a
new worktree from its original pinned base.

To remove a task, use the **×** beside its name in **Saved work**, or **Delete task**
at the bottom of its conversation. This works for completed reviews and other
saved tasks. Type the task name to confirm permanent deletion of its conversation,
reports, local branch, and worktree, including uncommitted work. Export a handoff
first if you want a copy. The source checkout, project memory, other tasks, and
published GitHub comments are kept. Stop running work before deleting a task.

## Share work with another developer

Open **Settings → Export handoff** to select up to 30 saved tasks, or use
**Export handoff** at the bottom of a task to select that task. Download the JSON
file and send it through your team's usual sharing channel. Export does not move
or delete your local work. Uncheck all tasks to share project memory alone.

The recipient adds their clone in **Projects**, opens its **Settings → Import
handoff**, and chooses the file. The preview shows incoming tasks, name changes,
and how memory will be handled. **Import paused work** restores each task into a
new worktree. Open an imported task, review its conversation and files, then
choose **Continue from saved step**. This restores the saved gate or phase;
it does not call a model. Further work uses the recipient's own backend login.

The handoff contains saved conversation history, specifications, test plans,
reviews, validation evidence, pinned agent definitions, reports, project memory
versions, task commits, and uncommitted edits. The recipient needs the original
base commit in their checkout; fetch it from the team's Git repository if the
preview reports it missing. Task commits after that base travel in the file, so
they do not have to be pushed first. Original commit IDs are retained.

Existing tasks are never overwritten. Name collisions get an `-import-N` suffix.
Existing project memory remains current; incoming versions are available in
**Memory** and imported tasks retain their pinned versions. For a project without
memory, the exported current version becomes current. Agent definitions remain
pinned to the imported tasks; import does not edit the shared agent catalog or
change project assignments. Backend preferences and validation commands come from
the recipient's configuration.

All imported tasks pause for review, including tasks exported from a continuous
workflow. Previous approvals and check results remain historical evidence.
Pending test approval requires **Recheck** on the recipient's machine before
**Approve tests** becomes available. Import itself runs no model, build, or test.

Files are limited to 32 MB. Only stable review gates and completed phases can be
exported; stop running work first. If files changed outside the saved gate, use
the task's recheck/recovery actions before exporting. Agent design conversations
are excluded; share their saved definitions in Git. Ignored files, build outputs,
provider logins, backend settings, and raw backend diagnostics are not exported.
Conversation text, notes, source references, and code are included, so review the
file before sharing it. This is a point-in-time handoff, not live synchronization
or an ownership lock; agree who will continue the task.

**Prepare local handoff** in the coding workflow still prepares reports and a PR
description. **Export handoff** transfers saved work to another harness instance.

## Project memory

Open **Project memory** in the sidebar to see the current notes and saved source
files. Add or edit architecture notes, invariants, decisions, and terminology,
then choose **Save memory**. Notes support plain text or Markdown and are included
in full (up to 32,000 UTF-8 bytes). No model call or repository edit is involved.

In **General chat**, you can say “Save this to project memory: every job must be
safe to retry,” or ask to remember a clear point from the conversation. The agent
returns a note for the harness to append to current memory. The conversation shows
**Saved to project memory**, the exact text, and a link to its saved version only
after the write succeeds. That chat uses the new version immediately; other tasks
keep theirs. Ordinary discussion does not request a memory write. If “this” is
ambiguous, the agent asks what to save. Editing or deleting existing notes still
uses the editor. A failed save shows its reason and keeps the proposed note.

Reference paths are relative to the selected repository, one per line. Adding a
path captures **all listed files** from committed HEAD. Editing notes or removing
a path keeps the retained files' saved contents. **Refresh sources & save** reads
them again from committed HEAD; uncommitted edits are excluded. Limits are 24
tracked regular text files, 1 MB each and 3 MB total. Expand **Saved source contents**
to read the full copies. To clear current memory, empty both fields and save.

**What agents receive** previews the saved notes, source excerpts, and stale-source
flags. Each matching source contributes up to 4,000 characters and 100 symbols;
changed or missing sources have no excerpt. The project preview compares against
the main checkout. **Preview selected task's memory** uses that task's pinned
version and actual worktree. Repository instructions and conversation history
are separate from this project memory.

Every edit creates an immutable version. **Version history → View version** reads
an older one; **Make this current** restores it for future tasks without deleting
history. The editor preserves drafts across polling and panel changes, and
rejects a save if another session has changed current memory. Copy any edits you
want to keep before using **Discard draft & load current** to resolve that conflict.
Unsaved drafts remain only in the current browser tab and are lost on page reload.

New tasks pin the current version. Existing tasks keep their version until you
choose **Use viewed version for selected task**. This is allowed between completed
phases or between general chat messages, never during model work or a pending
review. Saving or restoring project memory does not change existing task history.
Memory is local to this project's harness state directory; Git does not sync it.
Export/import handoffs transfer saved memory versions between developers.

## Backends and recovery

Custom backends can expose a model picker by setting `models_command` to an argument
array (use `{executable}` for the configured backend executable) and leaving
`models_format = "json"`. Return an object like
`{"models":[{"id":"fast","name":"Fast"}],"current":"fast"}`; `current` and
`default` are optional. The command runs outside the project with a timeout and the
backend's usual environment filtering. Configure `model_args` or JSON input so the
selected model reaches the provider. Built-in adapters use the providers' own
catalog and initialization interfaces; no model names are hard-coded.

Automatic selection checks available executables, existing login probes, and a
small generation request. It sends no project data during discovery. If several
backends work, choose one on the page. Authentication and usage allowances belong
to each provider; being logged in does not establish remaining quota.

Project settings lets you select another backend. **Check backends and resume**
checks availability again and continues the selected task. Provider failures
pause work and retain diagnostics; they never bypass approvals or trigger an
automatic provider fallback. See [backend configuration](../BACKENDS.md).

## Validation and local reports

[Validation and commits](VALIDATION.md) covers Cabal, strict GHC builds, HLint,
Nix checks, JUnit reports, expected failures, and custom commands.

The displayed report directory contains the current specification, exploration,
validation logs, patch series, independent reviews, finding dispositions, and
handoff draft. Review these and use your team's normal publishing process.
Agent review is advisory and does not establish that code is free of defects.

The server binds only to `127.0.0.1`. Model processes use your configured provider
and its access. The harness checks proposed changes and unexpected worktree edits;
this is not an operating-system sandbox. Use trusted backend configuration.
