<div align="center">

# λ Lambda Harness

### Build with agents. Keep the decisions yours.

A local workspace for functional programming teams, from conversation to reviewed patches.

[Get started](#open-your-workspace) · [User guide](docs/GUIDE.md) · [Model backends](BACKENDS.md) · [Contribute](docs/DEVELOPMENT.md)

</div>

---

Talk to one task orchestrator. It answers questions, delegates implementation,
checks and investigation to workers, and uses their results to keep working.
Your instructions determine the work; specification, test-writing and review
phases are no longer prerequisites for the default conversation.

Choose **New task → Work with an orchestrator**, select your repository, base,
model and effort, and describe the result you want. On an existing feature task,
send your next instruction in the same conversation to adopt its current worktree.
No restart is necessary. **Continue** asks the orchestrator to carry on; **Run all
checks and tests** asks it to discover and execute the relevant checks.

Workers edit directly in the selected task's isolated worktree. Failed checks
return to the orchestrator for diagnosis, repair and rechecking. Worker summaries
and actual command receipts appear in expandable conversation entries. The
orchestrator reports completion or explains the concrete blocker requiring your
input. It does not create commits or publish changes unless you ask.

Direct editing currently requires the **Codex backend**. The selected model and
reasoning effort apply to the orchestrator and its workers. Native requests for
additional permissions appear in the conversation with Approve and Deny buttons.
**Stop model** preserves completed edits and the conversation; send revised
instructions to continue from there.

Tasks keep separate conversations, worktrees, model choices, progress and stop
controls. Different tasks can run simultaneously. Project memory remains shared.
Existing standalone review, chat, agent-authoring and check tasks remain available;
the older phase APIs remain compatible for explicit integrations.

[User guide](docs/GUIDE.md) · [Phone access](docs/GUIDE.md#work-from-your-phone)

## Open your workspace

With Nix, run this from any directory:

```sh
nix run --refresh github:Devnull-org/lambda-harness -- \
  --state-dir ~/.local/state/lambda-harness
```

`--state-dir` is required: it chooses where your conversations, memory, and worktrees
live. Reuse the same directory each time; existing users should keep their current path.

Open the printed local URL and add a Git checkout from **Projects**.
Switch between projects from the sidebar; each keeps its own memory and saved work.
Keep the server running while using the website.

Choose **General chat**, **Explore**, **Plan a feature or fix**,
**Review a PR or branch**, or **Write tests**.
Describe what you need. The harness checks your existing model logins and lets you
choose when more than one backend is available.

**Light, dark, or your system theme.** A familiar editor, visible conversation
history, and a stop button when you want to change direction.

Prefer running without Nix? See the [user guide](docs/GUIDE.md#without-nix).

## Your project. Your tools.

Powered by LangGraph and independent of any one model provider. The coding workflow
currently supports Haskell and Nix; more functional languages are planned.
Use existing agent logins or connect your own backend. Save architecture
notes and invariants as shared project context for the team’s workflow.

The website runs locally. Model requests use your chosen provider. The harness
creates no pull requests and pushes no code automatically.

[Explore the workflow →](docs/GUIDE.md) · [Configure project checks →](docs/VALIDATION.md)

Use the single message box in a task's **Conversation** section to talk to the
agent handling that step. Exploration follow-ups go to the explorer; patch
requests go through the current coding agent and its existing approval flow.
Between steps, you can still ask the current agent for explanations or a commit
message without starting the next phase. Workflow buttons remain in **Task actions**.

Select the model above the message box. Your choice is remembered across page
reloads and saved per task when a request starts. Explicit choices override role
and provider defaults. Replies show the requested model and, when the provider
reports it, the actual model ID. An unavailable selection fails visibly instead
of silently switching to another model.
Use **Effort** beside the model picker to choose a level advertised by that model.
The selection is saved per task and model; **Provider default** clears the override.
Higher effort can take longer. Review receipts show each reviewer's requested and reported model and selected effort.
