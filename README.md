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

- TOOLS are programmatic capabilities, including approved LangChain MCP tools.
- SKILLS are repo-scoped procedural knowledge loaded natively by Deep Agents.
- MEMORY is repo-scoped durable knowledge in the LangGraph Store.
- STATE is IssueThread-local workflow history and checkpoints.
- CONTEXT is immutable invocation authority, including repository identity.
- MODEL is the reasoning engine.
- DEEP AGENTS is the inner agent harness.
- LANGGRAPH is the durable orchestration/runtime.
- SWEFORGE owns the SWE-specific lifecycle and composition.

Every strict GitHub-triggered agent invocation receives its repository authority from
the persisted IssueThread/SourceEvent, never from model text or repository
configuration. Memory and skills use separate namespaces derived from the
stable GitHub repository ID. MCP discovery is filtered by a trusted
`RepoCapabilityRegistry`, and every MCP call is re-authorized by an interceptor.
The default task subagent inherits the same runtime context and filesystem
permissions. Strict GitHub execution requires a configured provider-neutral
sandbox backend and fails closed when none is available. Repository A therefore
cannot discover or access repository B's memory, skills, MCP tools, workspace,
or credentials in strict mode.

`LocalShellBackend` is retained only for the standalone development harness and
the explicit workflow `--unsafe-local-shell` escape hatch. It executes with
host permissions and does not enforce cross-repository isolation.

Shared memory and skills are read-only to ordinary task agents. Trusted
operator APIs/CLIs and the application-controlled post-publication learning
pass are the only mutation paths. Learning is evidence-backed and records
`UPDATED`, `NO_UPDATE`, or `FAILED` without invalidating an already successful
publication.

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
remains the ingestion mechanism. Writeback uses a separate narrowed
`contents/issues/pull_requests: write` permission profile.

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

## Durable planning workflow

IssueThread execution is gated by an application-owned workflow state machine.
In INTERACTIVE mode, an actionable root enters planning, SWEForge posts a
versioned plan to the original issue, and only the exact command
`@agent approve` creates a permit for that exact plan version. Other leading
`@agent` comments revise the plan. The planner uses a structurally read-only
filesystem backend: it cannot execute a shell or mutate the worktree.

An issue labeled `AUTO` follows the same plan-and-post sequence, then the
application re-checks the label and creates an AUTO permit. AUTO skips the
human wait; it does not skip planning or observability. Removing the label
before permit creation returns the workflow to interactive approval.

Comments must start with `@agent` to be actionable. While planning or
executing, persisted leading-invocation comments are injected before the next
model call using stable event-derived LangGraph message IDs. The delivery is
at-least-once and logically deduplicated. Control comments are consumed by the
workflow and cannot later become independent coding executions. Plan and
execution-summary comments use deterministic markers so publication retries do
not duplicate them. Repository memory remains read-only to both planner and
executor agents.

## Repository-scoped long-term memory

IssueThread conversation state and repository memory have separate lifetimes:

- Short-term/thread memory is the LangGraph checkpoint in
  `~/.sweforge/checkpoints.sqlite` and belongs to one IssueThread.
- Long-term/repository memory is the LangGraph SQLite Store in
  `~/.sweforge/memory.sqlite` and is shared by all IssueThreads for one
  repository.

Memory is keyed by the stable GitHub `repo_id`, not by an issue number, thread
ID, workspace path, or repository display name. IssueThreads in one repository
share `("sweforge", "repo", repo_id)`; another repository receives a separate
namespace.

The canonical file is `/memories/AGENTS.md`. It is initialized with a minimal
header when a repository first executes. Task agents can read it through
Deep Agents' native `memory=["/memories/AGENTS.md"]` loading, but native
filesystem permissions deny agent writes to `/memories/**`. Repository memory
is currently operator-managed; SWEForge does not automatically extract task
summaries or let untrusted issue text update shared memory.

Use the trusted operator CLI to inspect or seed memory. The repository must
already have been observed by the GitHub poller so its stable ID can be
resolved from `state.db`:

```bash
uv run sweforge-repo-memory \
  --state-db ~/.sweforge/state.db \
  --memory-db ~/.sweforge/memory.sqlite \
  --repo owner/repository show

uv run sweforge-repo-memory \
  --state-db ~/.sweforge/state.db \
  --repo owner/repository replace --file /tmp/repository-memory.md

uv run sweforge-repo-memory \
  --state-db ~/.sweforge/state.db \
  --repo owner/repository append --text "Run tests with mvn test."
```

Trusted repository skills are managed separately and are mounted at
`/skills/` through the repo-scoped Store namespace:

```bash
uv run sweforge-repo-skills --state-db ~/.sweforge/state.db \
  --repo owner/repository list
uv run sweforge-repo-skills --state-db ~/.sweforge/state.db \
  --repo owner/repository put build/SKILL.md --file /secure/operator/path/SKILL.md
```

The memory database is separate from `state.db` and the checkpoint database,
is never placed in a worktree, and is not exposed through the task shell
environment. Operator writes are trusted inputs and should not contain
credentials, secrets, conversation history, chain-of-thought, or temporary
debug output.

## Development-stage IssueThread execution

The one-shot executor extends that boundary to:

GitHub poll → `SourceEvent` → `IssueThread` → persistent local worktree →
Deep Agent + LangGraph thread checkpoint.

```bash
uv run sweforge-github-execute \
  --db ~/.sweforge/state.db \
  --checkpoints ~/.sweforge/checkpoints.sqlite \
  --memory-db ~/.sweforge/memory.sqlite \
  --workspace-root ~/.sweforge/workspaces \
  --lock-root ~/.sweforge/locks \
  --repo-path owner/repository=/Users/me/repository \
  --model provider:model
```

Execution is explicitly one-shot: one invocation claims at most one routed
event. `--repo-path` mappings are trusted local checkouts. Worktrees
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

### Crash-safe GitHub writeback

### Execution review and repair

Workflow execution is split into provider-neutral model roles:

- `planning_model`: use a high-quality model for planning and replanning.
- `execution_model`: a lower-cost model may perform the approved implementation.
- `review_model`: use a high-quality model to inspect the cumulative workspace.

The durable flow is `plan -> execute -> review -> repair/review loop -> ACCEPT -> publish`.
Providers remain interchangeable. Reviewers are structurally read-only, and
publication is unavailable until the latest successful attempt has an exact
`ACCEPT` review.

After a successful execution, publish one result with the App credentials:

```bash
uv run sweforge-github-publish --db ~/.sweforge/state.db \
  --lock-root ~/.sweforge/locks
```

Writeback durably records commit, push, pull request, comment, and completion
states. It commits only non-empty IssueThread changes using the deterministic
message `sweforge: address issue #N`, pushes only the IssueThread branch, and
refuses divergent remote branches. Existing pull requests and comments are
reconciled by repository/branch/base and the stable marker
`<!-- sweforge:publication:<event-key> -->`; ambiguous matches fail safely.
Use `--retry EVENT_KEY` for a publication recorded as `FAILED`.

Git HTTP authentication uses a short-lived installation token through a
temporary `GIT_ASKPASS` helper. The token is never placed in a remote URL,
Git config, SQLite state, or command-line argument. The publisher derives the
credential-free HTTPS remote from the configured GitHub API host and does not
trust an arbitrary credential-bearing remote. Publication failures preserve
the workspace and durable progress for retry.
## Human-input routing

An `IssueThread` is the durable conversation identity; each planning, execution,
review, publication, and learning sequence is a cycle within that thread.
Unsolicited `@agent` input is planning input while planning and is a durable
next-cycle follow-up once execution, review, repair, or publication is active.
It is never injected into execution or `REVIEW_EXECUTION`.

An execution agent may instead use the application-owned clarification tool when
specific missing information prevents safe continuation. The tool performs a
native LangGraph checkpoint interrupt; no later tool/action in that run proceeds
before the answer. SWEForge persists the request, enters `WAITING_FOR_INPUT`,
and routes the question back to its issue, PR conversation, or inline review
thread. A restart reconciles an open request with the interrupted execution and
retries clarification posting idempotently. An unambiguous, provenance-matched
answer resumes the same checkpoint and cycle.

Resume is selected by interrupt occurrence, not by clarification ordering. Each
`request_clarification` call carries its tool-call id as an `occurrence_key`,
and the answer handed back to LangGraph is the one persisted for the occurrence
that is actually pending in the checkpoint. Two clarifications in one cycle
therefore each receive their own answer. When the pending occurrence has no
answer, SWEForge does not invoke the graph at all: invoking a graph that holds
a pending interrupt makes LangGraph reuse the previous `Command(resume=...)`
value, so running would feed that interrupt a stale answer. Review repairs are
not given the clarification sink, so the tool is never registered for a repair
run and a repair cannot strand a pending interrupt on the shared checkpoint. A scope-changing or ambiguous
answer does not reuse the old authorization. Mixed answers retain a distinct,
durable residual follow-up identity for the next planning cycle. Repair runs do
not expose the clarification tool; review findings remain internal repair input.

`SourceEvent` is immutable external GitHub provenance, while a logical workflow
input is the application-owned actionable unit consumed by planning and a
cycle. Ordinary inputs use their event key as their logical identity; residual
follow-ups use their durable `deferred_id`. One SourceEvent may therefore
produce multiple logical inputs, such as a clarification answer and a residual
task. Each cycle persists both the originating `root_event_key` and the
selected logical root identity so restart and later execution retain the same
provenance and input selection.

## Lifecycle identity

`SourceEvent` is provenance, not the unique identity of any work. Every layer
below it is scoped to the exact lifecycle it belongs to:

| Layer | Question it answers | Identity |
| --- | --- | --- |
| SourceEvent | Which GitHub event originated this? | `root_event_key` / `source_event_key` |
| Logical input | What actionable task was this? | `root_input_id` (event key, or a durable `deferred_id`) |
| Cycle | Which workflow lifecycle? | `thread_id` + `cycle_id` |
| Execution | Which execution? | `execution_id` |
| Publication | Which output/idempotency state? | `publication_id` |
| Memory learning | Which post-publication learning? | `learning_id` |

`execution_id`, `publication_id` and `learning_id` are all deterministic over
`(thread_id, cycle_id, root_input_id)`, so a restart or retry recomputes the
same identity and never invents a second one.

One SourceEvent may back several cycles. A mixed human reply can answer an open
clarification for the running cycle and leave a residual follow-up that becomes
the next cycle, and one event can queue several residual tasks. Those cycles
each get their own execution, publication, execution-summary comment and
learning record, while all of them retain the same `source_event_key`:

```
SourceEvent S
  ├── logical input D1 → cycle A → execution E1 → publication P1 → learning M1
  └── logical input D2 → cycle B → execution E2 → publication P2 → learning M2
```

Publication is authorized from the current cycle only. Eligibility proves that
this exact thread, cycle, logical input, plan version, execution, attempt and
ACCEPT review all agree; an ACCEPT for `D1` therefore cannot authorize `D2`, and
`D2` cannot reuse `D1`'s publication. Finalization is a single guarded
mutation: it marks that cycle's plan `EXECUTED`, creates that cycle's learning
record and returns the thread to `IDLE`, all under exact
thread/cycle/logical-input predicates. A stale publication fails closed — it
stays readable and reconcilable, but it can never finalize, publish for, or
otherwise mutate a newer cycle. Queued sibling logical inputs stay queued and
are discovered by `next_workflow_input`.

The execution-summary comment marker is scoped to `publication_id`, so retrying
one publication finds and reuses its own comment while a different lifecycle
from the same SourceEvent legitimately posts its own. Publication comments use
the same scoping. Repository-memory learning belongs to the completed
lifecycle: it is only ever written through its own `learning_id`, and the
`IDLE` retry finishes the oldest unfinished record for the thread before the
next logical input is selected, so a stalled record from an earlier cycle can
neither block nor overwrite a newer one and can never mutate workflow state.

## Locking and crash recovery

Three cross-process locks, all `flock`-based so a crashed holder releases them:

| Lock | Key | Guards | Duration |
| --- | --- | --- | --- |
| IssueThread | `thread_id` | one thread's execution, review, repair, recovery and publication | a whole run, including model calls |
| Repository Git | authoritative `repo_id` | shared Git administration: creating a thread's worktree and its branch | a few local Git commands |
| Repository memory | authoritative `repo_id` | appending to one repository's `AGENTS.md` | one read-modify-write |

**The one legal order is IssueThread lock → repository Git lock → release.** No
path may take an IssueThread lock while holding a repository Git lock;
`thread_lock` raises `LockOrderError` rather than deadlocking, so an inversion
is a test failure instead of a hang.

The repository Git lock is deliberately narrow. Per-worktree work — `status`,
`diff`, `commit` on the thread's own branch — is not shared state, and each
thread owns a distinct branch, so Git's own per-ref locking already covers it.
Only `ThreadWorkspace.create` needs serialization, because it is check-then-
create over the repository's worktree registry and branch namespace. Two
workers racing the same thread's worktree used to leave *both* failed, with the
loser's cleanup deleting the winner's directory and a branch surviving with no
worktree — a permanently unusable thread. Creation now runs whole under the
lock: one winner, and the loser gets the ordinary fail-closed error. A branch
left behind by a crash mid-`worktree add` is reattached rather than recreated,
so a crash no longer wedges the thread either.

The lock never spans a model call, a test run, review, memory learning or
GitHub API traffic, so long-running work in different IssueThreads of the same
repository stays fully concurrent. Publication takes only the IssueThread lock —
it performs no shared Git administration — so publishing is never serialized
behind another thread's worktree setup and cannot participate in a lock cycle.

### Orphaned executions

If a worker dies mid-execution the thread is left `EXECUTING` with a `RUNNING`
attempt. Recovery runs under the IssueThread lock, because holding it is the
only trustworthy proof that no worker is still alive; if the lock is taken the
answer is BUSY and nothing is mutated. With the lock held: a durably `SUCCEEDED`
execution is reconstructed into review without re-running anything, and a
genuine orphan is handed back for retry under the *same* cycle, plan, permit,
branch, workspace and logical execution identity. Nothing ever infers success
from a dirty worktree, and partial work is preserved rather than reset.

Retries are bounded by the attempt's existing `retry_count`, which is committed
immediately before each run — so a hard crash consumes budget instead of
resetting it. After three executions of one INITIAL attempt the cycle fails
closed to `REVIEW_BLOCKED` with the attempt `FAILED`: no publication, no plan
completion, no solved-issue record, and no further model spend.

## Two kinds of memory

SWEForge keeps two deliberately separate forms of durable knowledge. They
inform each other but never merge.

**Repository memory** answers *what should future agents know about how this
repository works?* It is compact, broadly loaded into every execution agent as
`/memories/AGENTS.md`, and only ever written by the application's
evidence-backed validator.

> Integration tests run with `./gradlew integrationTest`.

**Resolved-issue memory** answers *have we solved something like this before?*
It is structured case history, repository-scoped, and retrieved rather than
loaded.

> #142 — bulk discount failed above $100 because subtotal cents were divided
> before `DiscountPolicy`; removed the division; pricing tests passed.

The distinction is a trust boundary. A case is what one lifecycle diagnosed at
the time it was fixed; it is a clue for future work, never repository truth.
"#88 was fixed by bypassing cache X" must never become "cache X should always
be bypassed". A historical case may motivate a repository-memory proposal, but
it is never sufficient evidence on its own: durable repository knowledge is
admitted only when grounded in current repository lines that the application
reads and verifies itself.

### Nominating repository knowledge

Curation after publication sees the cumulative diff, so on its own it can only
cite files the task changed, and only their first window of lines. Knowledge
found while *reading* — build and test commands, conventions, configuration
locations, dependency relationships — used to be unreachable.

Execution agents can now call `propose_repo_memory(category, fact,
durability_reason, path, start_line, end_line)`. The tool writes nothing. The
model nominates only *where* the evidence is; SWEForge reads those lines from
the authoritative worktree and derives the excerpt and hash itself, so a model
can never assert repository content it did not find. Paths that are absolute or
escape the worktree, inverted or out-of-range line spans, empty spans and
secret-looking evidence are all refused. Proposals are stored per lifecycle
with a deterministic identity, then re-verified against the live worktree after
publication and put through the same validator, deduplication and repository
lock as diff-derived candidates. Because proposals are validated on their own,
the gap closes even when no curator model is configured.

Repository memory stays write-denied to the agent: `/memories/**` and
`/skills/**` remain deny rules, and the validator is still the only writer.

### How a case is created and used

Historical cases are automatic, with no model discretion over whether the job
exists. Finalization creates the record in the same transaction that marks the
plan executed, so only genuinely finalized lifecycles produce one — failed,
review-blocked, unapproved or incomplete work never claims to have solved
anything. Identity is `stable(thread_id, cycle_id, root_input_id)`, matching
execution, publication and learning, so one issue worked in three cycles yields
three independent cases while `issue_number` and `thread_id` still group them.
The issue's canonical title and body are snapshotted during ingestion from
payloads the poller already holds, so learning never needs its own network call.

Retrieval is local and deterministic: SQLite FTS5/BM25 over the case rows,
always filtered by authoritative `repo_id`, so one repository can never read
another's history. The index is derived and rebuildable; the rows are
authoritative. Planning automatically receives the top few relevant cases,
capped at one per IssueThread so a single noisy issue cannot flood context, and
the main agent can call `search_issue_memory` for deeper read-only research.
Cases are always framed as clues to verify against current code, and the
approved plan remains the only thing that authorizes work.

Both learning lanes are optimizations, never authorization inputs. Each claims
its attempt durably *before* invoking a model, so a hard crash consumes budget
instead of retrying forever; each is bounded at three attempts and then left
FAILED with its error while the thread proceeds to its next logical input. A
lifecycle that ran with no curator configured is recorded distinctly from one
that curated and found nothing.

Learning never gates delivery. Repository memory is an optimization, not an
authorization input, so a curator that keeps failing is retried a bounded
number of times and then left FAILED with its error for diagnosis while the
thread proceeds to its next logical input. A lifecycle that completed with no
curator configured is recorded explicitly rather than as an ordinary
"nothing durable found" result.

Databases written before this model are migrated in place. An event-keyed
publication or learning row is an ordinary lifecycle, so its logical input is
its event key; every field, status, SHA, PR, comment id and proposal is
retained, the SourceEvent stays as provenance, and reopening is idempotent.
Event-key lookups survive as diagnostics (`publication_for_event`,
`repo_memory_learning`) and fail closed when a SourceEvent turns out to back
more than one lifecycle.
