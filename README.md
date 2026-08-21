# SWEForge

SWEForge V0 is a small local walking skeleton for software-engineering agents.
Its flow is: task → temporary Git worktree → Deep Agent → inspect/edit/test →
diff/result.

## Setup

```bash
uv sync
export SWEFORGE_MODEL=anthropic:claude-sonnet-4-6
export ANTHROPIC_API_KEY=...
```

Other provider model strings supported by LangChain can be used, for example
`openai:gpt-5.5` or `google_genai:gemini-3.6-flash`, with the corresponding
provider package and credentials installed.

## Run

```bash
uv run sweforge /path/to/git/repository "Fix the failing tests"
```

The temporary worktree is preserved by default and its path is printed for
human inspection. Use `--discard-worktree` to explicitly remove it after the
run. Failed runs also retain their worktree for debugging.

## Safety boundary

The worktree isolates changes from the primary checkout, but it is not a
security sandbox. Deep Agents' `LocalShellBackend` executes commands directly
on the host with the process user's permissions. Do not use this V0 CLI with
untrusted tasks or repositories.

## Conceptual boundaries

- TOOLS are what the agent can do.
- SKILLS are specialized procedural knowledge.
- MEMORY is previously learned repository knowledge.
- MODEL is the reasoning engine.
- DEEP AGENTS is the inner agent harness.
- LANGGRAPH is the durable orchestration/runtime.
- SWEFORGE owns the SWE-specific lifecycle and composition.

Future work may add GitHub writeback, per-thread sandboxes/workspaces,
repository-scoped memory/skills/tools, and multi-repository execution. Those
are planned boundaries, not V0 features.

## GitHub ingestion foundation

The next milestone polls GitHub repositories for `@agent` mentions; it is
polling-based rather than webhook-based and records durable `SourceEvent` and
`IssueThread` state without executing an agent.

```bash
SWEFORGE_GITHUB_APP_ID=... \
SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH=~/.sweforge/credentials/sweforge-dev.pem \
uv run sweforge-github-poll \
  --repo owner/repository \
  --repo owner/another-repository \
  --db ~/.sweforge/state.db
```

GitHub App authentication is the preferred model. Configure the App ID with
`SWEFORGE_GITHUB_APP_ID` and keep its private key outside repositories at the
path in `SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH`. A Client ID may optionally be
set with `SWEFORGE_GITHUB_APP_CLIENT_ID`; when present it is used as the JWT
issuer, otherwise the App ID is used. The legacy
`SWEFORGE_GITHUB_CLIENT_ID` name is accepted as a compatibility alias. The
private key contents are never accepted on a CLI argument, logged, or stored
in SQLite.

The App must be installed on each explicitly selected repository. Polling first
discovers that repository's installation, then mints a short-lived,
repository-scoped installation token with only `contents: read`, `issues: read`,
and `pull_requests: read`. JWTs and installation tokens are held only in
process memory and refreshed before expiry. Webhooks are not used; polling
remains the ingestion mechanism. Future write operations will use separate
narrowed permission profiles and are not implemented yet.

The REST API URL can be overridden with `SWEFORGE_GITHUB_API_URL`; the version
header can be overridden with `SWEFORGE_GITHUB_API_VERSION` or `--api-version`.
The default is the current documented GitHub REST API version. The default
SQLite database path can be overridden with `--db`. Do not commit keys, tokens,
or the state database.

For local development only, `SWEFORGE_GITHUB_TOKEN` remains a legacy fallback
when App credentials are absent. App credentials always take precedence, and
the two authentication modes are never combined.

The boundary is: GitHub → poller → durable `SourceEvent`/`IssueThread`;
execution comes later.

## Development-stage IssueThread execution

The one-shot executor extends that boundary to:

GitHub poll → `SourceEvent` → `IssueThread` → persistent local worktree →
Deep Agent + LangGraph thread checkpoint.

```bash
uv run sweforge-github-execute \
  --db ~/.sweforge/state.db \
  --checkpoints ~/.sweforge/checkpoints.sqlite \
  --workspace-root ~/.sweforge/workspaces \
  --lock-root ~/.sweforge/locks \
  --repo-path owner/repository=/Users/me/repository \
  --model provider:model
```

Execution is explicitly one-shot: one invocation claims at most one routed
event. `--repo-path` mappings are trusted local checkouts; authenticated clone,
GitHub writes, commits, pushes, and PR creation are not implemented. Worktrees
remain under `~/.sweforge/workspaces/{repo_id}/issue-{number}/`, while
checkpoints live in their separate SQLite file. The same deterministic
`IssueThread.thread_id` is the LangGraph `thread_id`, so follow-up events reuse
both the worktree and checkpointed conversation. Per-thread locks allow
different issues to execute independently; this SQLite checkpointer is for the
local milestone, not final production scale.

### Crash recovery

If a process dies after claiming an event, its `RUNNING` row can be recovered
only after the configured age threshold and only when the same host-local
`fcntl` lock is free. `recover-stale` marks it `INTERRUPTED`; it never retries
automatically. Operators may explicitly `retry` or `skip` failed/interrupted
events. Retries preserve the worktree and LangGraph thread/checkpoint, while
skipped events are resolved for per-thread ordering. LangGraph owns graph state;
the outer SQLite database owns SourceEvents and execution status. Tool side
effects remain potentially ambiguous if a process dies mid-operation, so
distributed workers require a different lease/lock mechanism.

```bash
uv run sweforge-github-execution --db ~/.sweforge/state.db status
uv run sweforge-github-execution --db ~/.sweforge/state.db recover-stale
uv run sweforge-github-execution --db ~/.sweforge/state.db retry EVENT_KEY
uv run sweforge-github-execution --db ~/.sweforge/state.db skip EVENT_KEY
```
