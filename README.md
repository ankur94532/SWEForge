# SWEForge

SWEForge is a GitHub-native software-engineering agent. An engineer files an
issue that mentions `@agent`; SWEForge plans the change, gets it approved,
implements it in an isolated per-issue worktree, validates it, publishes a
commit and pull request, and then keeps handling follow-up feedback on the same
issue — all under deterministic application control.

**Core principle: the model chooses what to do inside a state; application code
chooses which states and which capabilities are reachable.** Phases, task
scheduling, approvals, permits, validation evidence, publication and
configuration are owned by SQLite transactions in SWEForge. Nothing the model
emits — plan prose, a tool argument, or issue text — can move the workflow,
widen a capability set, or authorize an output.

## What SWEForge is

- **One durable `IssueThread` per GitHub issue.** `github:{repo_id}:issue:{n}`
  is the stable identity for every plan, execution, validation, publication and
  revision belonging to that issue.
- **A persistent per-issue branch and worktree.** `sweforge/issue-{n}`, created
  once from a frozen base commit and reused by every later task and revision.
- **[Deep Agents](https://github.com/langchain-ai/deepagents) as the inner agent
  harness**, with SWEForge middleware supplying the phase-scoped tool set,
  phase model and phase skills before every model call.
- **LangGraph checkpoints for durable model continuation.** Conversation state,
  summarization and native interrupts live in a checkpointer keyed by the
  IssueThread. Checkpoints are not a second workflow state machine.
- **A deterministic, application-owned workflow lifecycle** expressed as a
  declarative task/phase specification the operator installs.
- **Repo-scoped skills, registered script tools, MCP allowlists, secrets and
  memory**, all installed by an operator as immutable configuration
  generations — never read from the target repository's own files.
- **GitHub App integration.** Polling for input; short-lived, repository-scoped
  installation tokens for output. The model never holds a credential.
- **Two interaction modes.** `MANUAL` requires a human `@agent approve` at the
  plan and at the result. `AUTO` performs the same work and records the same
  durable authority records automatically.

## Architecture

```
GitHub issue / issue comments / PR conversation / inline review / submitted review
        │  polling (no webhooks)
        ▼
GitHubPoller ──► durable SourceEvents ──► routing (issue number, or PR→thread map)
        ▼
IssueThread  (github:{repo_id}:issue:{n})     ← durable identity
        ▼
DeclarativeWorkflowController                  ← application authority
        │   selects exactly one active task, owns every transition
        ▼
WorkflowRuntime (SQLite: cycles, task runs, plans, permits,
                 executions, validations, results, approvals)
        ▼
DeepAgentWorkflowDriver ──► root Deep Agent (one per cycle, checkpointed)
        │        WorkflowPolicyMiddleware: phase tools, phase model, phase skills
        │        WorkflowSkillsMiddleware: one eager skill, or a catalog
        ▼
skills / built-in tools / registered scripts / MCP / sandbox / bounded investigator
        ▼
run_validation (deterministic diff + execution evidence) ──► finish_validation ACCEPT
        ▼
GitHubPublisher: one commit, one push, one PR, one publication comment
        ▼
post-publication learning  ·  revision feedback loops (same thread/branch/PR)
```

Deterministic application authority: which task is active, which phase it is
in, which tools exist in that phase, whether a plan is authorized, whether
execution may run, whether validation evidence is sufficient, whether
publication is eligible, and what gets posted to GitHub.

Model reasoning: how to investigate the repository, what the plan says, how the
code is changed, what the validation report says, and whether a piece of user
feedback concerns the current approval scope.

## Core invariants

1. Only an application-owned lifecycle gateway (`submit_plan`,
   `finish_execution`, `finish_validation`) ends a phase. Plan-shaped prose is
   not phase completion; the driver nudges twice and then fails closed.
2. Exactly one task is active per cycle. Dependencies decide eligibility;
   `active_task_id` decides ownership. Waiting states retain ownership, so no
   peer task starts while a task waits for approval or input.
3. Planning and validation are structurally read-only. Built-in mutation and
   `effect: mutate` script tools are filtered out of the model's tool list and
   rejected again at call time, even if a stale specification lists them.
4. Every execution-phase root tool call revalidates the exact uninvalidated
   permit for the current plan version.
5. `@agent approve`, exactly and alone, is the only deterministic approval. It
   must target the current pending occurrence and come from a user with proven
   `admin`, `maintain` or `write` permission.
6. Agent working memory is not workflow state. Deep Agents TODOs decompose an
   already-authorized plan during `EXECUTING`; they never advance a phase,
   finish a task, or widen what the plan authorizes.
7. Repository authority comes from operator-installed configuration
   generations. Files inside the target repository are never treated as
   workflow, skill, tool, MCP or credential configuration.
8. A cycle is bound to one immutable workflow specification digest (and, when
   configured, one repository configuration generation). Restart rehydrates
   that exact specification.
9. Fail closed. Missing sandbox, missing skill, missing credential, missing
   evidence, stale approval, ambiguous GitHub comment match — all stop the
   work rather than degrade it.

## Quick start

```bash
uv sync
```

Requires Python 3.12. Models are LangChain provider strings —
`anthropic:claude-sonnet-5`, `openai:gpt-5`, `google_genai:gemini-3-pro` — with
the corresponding provider package and credentials installed.

### GitHub App setup

A GitHub App is the supported production credential. Install it on each
repository you will name explicitly; SWEForge never discovers repositories.

| Variable | Purpose |
| --- | --- |
| `SWEFORGE_GITHUB_APP_ID` | App ID (used as the JWT issuer when no Client ID is set) |
| `SWEFORGE_GITHUB_APP_CLIENT_ID` | Optional Client ID; preferred JWT issuer when present |
| `SWEFORGE_GITHUB_CLIENT_ID` | Legacy alias for the Client ID |
| `SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH` | Path to the private key, kept outside any repository |
| `SWEFORGE_GITHUB_API_URL` | REST base URL (default `https://api.github.com`) |
| `SWEFORGE_GITHUB_API_VERSION` | `X-GitHub-Api-Version` override |
| `SWEFORGE_GITHUB_TOKEN` | Legacy PAT fallback, used only when no App credentials are present |

Authentication flow: SWEForge signs a short-lived App JWT (RS256, 9-minute
expiry), looks up the repository's installation, and mints a repository-scoped
installation token for a named permission profile — `contents/issues/pull_requests:
read` for polling, and a separate `contents/issues/pull_requests: write` profile
for writeback. JWTs and installation tokens live only in process memory and are
refreshed before expiry. The private key is never accepted on a command line,
logged, or written to SQLite. Both App credentials are required together; App
credentials and the legacy token are never combined.

### Run the server

`sweforge-serve` runs polling and workflow advancement in one process, behind a
host-local singleton lock on the state database.

```bash
SWEFORGE_GITHUB_APP_ID=123456 \
SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH=~/.sweforge/credentials/sweforge.pem \
SWEFORGE_SECRET_MASTER_KEY_FILE=~/.sweforge/credentials/secret.key \
uv run sweforge-serve \
  --repo-path owner/repository=/srv/checkouts/repository \
  --planning-model anthropic:claude-opus-4-5 \
  --execution-model anthropic:claude-sonnet-5 \
  --review-model anthropic:claude-opus-4-5 \
  --db ~/.sweforge/state.db \
  --checkpoints ~/.sweforge/checkpoints.sqlite \
  --memory-db ~/.sweforge/memory.sqlite \
  --workspace-root ~/.sweforge/workspaces \
  --lock-root ~/.sweforge/locks \
  --sandbox-provider my-sandbox \
  --workers 4 \
  --poll-interval 30
```

| Option | Meaning |
| --- | --- |
| `--repo-path OWNER/REPO=PATH` | Required, repeatable. Trusted local checkout for one repository. |
| `--model` | Default for planning/execution/review when a role model is not given. |
| `--planning-model` / `--execution-model` / `--review-model` | Per-phase models. All three must resolve, directly or through `--model`. |
| `--memory-model` / `--resolution-model` / `--clarification-model` | Learning and clarification models; default to review / memory / execution respectively. |
| `--db`, `--checkpoints`, `--memory-db`, `--workspace-root`, `--lock-root` | Durable state locations (all default under `~/.sweforge/`). |
| `--sandbox-provider NAME` | Selects a backend registered under the `sweforge.sandbox_backends` entry-point group. |
| `--unsafe-local-shell` | Development escape hatch: run commands directly on the host instead of a sandbox. |
| `--workflow-spec PATH` | Operator YAML workflow used for repositories that have no installed configuration. |
| `--capabilities-config PATH` | Operator JSON MCP registry used the same way. |
| `--once` | Poll once, drain discovered work, exit. |
| `--workers`, `--max-ticks`, `--poll-interval`, `--initial-lookback-minutes` | Dispatch bounds (defaults 4, 20, 30s, 10 minutes). |
| `--ready-file PATH` | Written after the singleton lock is held and the first poll ran. |
| `--debug-agent`, `--debug-agent-tools` | Bounded, redacted tracing (see below). |

Worker failures are persisted with bounded exponential backoff, so restarting
does not create a retry storm. After three consecutive dispatcher failures for
a thread, its active task is terminally failed rather than retried forever.

## Repository onboarding

Everything SWEForge trusts for a repository — its workflow, skills, registered
tools, MCP allowlist and credential references — is installed by an operator as
an immutable **configuration generation**. Nothing is read from the target
repository's checkout. See
[Adding a repository to SWEForge](docs/adding-a-repository.md) for the full
guide and [`examples/repo-config`](examples/repo-config) for a complete working
bundle, explained in [`examples/README.md`](examples/README.md).

The repository must already have been observed by the poller (so its stable
GitHub `repo_id` is known) before configuration can be installed.

### Initial configuration

```bash
sweforge repo init owner/repo                              # writes ./owner-repo-sweforge
sweforge repo validate owner/repo ./owner-repo-sweforge    # validate a candidate bundle
sweforge repo configure owner/repo ./owner-repo-sweforge   # install atomically
sweforge repo show owner/repo                              # safe metadata only
```

`repo init` writes a starter template and installs no authority. `repo validate`
with a path checks a candidate directory; with no path it re-validates the
currently installed generation. `repo configure` validates the entire bundle and
installs the next generation in one transaction. `repo show` prints generation
number, digest, workflow id and task ids, skill names and descriptions, script
names/effects/credential status, and MCP servers — never skill bodies, script
sources or secret values. All commands take `--state-db` (default
`~/.sweforge/state.db`).

### Bundle layout

```
owner-repo-sweforge/
  workflow.yaml            declarative tasks, phases, capability grants
  skills/
    <name>/SKILL.md        YAML frontmatter (name, description) + procedural body
    <name>/*.md            optional supporting files, loaded on demand
  tools/
    scripts/<dir>/tool.yaml     registered script definition
    scripts/<dir>/<entrypoint>  python or shell entrypoint
    mcp/servers.yaml            trusted MCP servers and per-tool allowlist
```

### Incremental workflow / skill / tool / MCP updates

Rebuilding a whole bundle to change one file is unnecessary:

```bash
sweforge workflow set owner/repo ./workflow.yaml

sweforge skill add owner/repo ./skills/reporting
sweforge skill add owner/repo ./skills/reporting --replace
sweforge skill remove owner/repo reporting

sweforge tool add owner/repo ./tools/scripts/deploy
sweforge tool add owner/repo ./tools/scripts/deploy --replace
sweforge tool remove owner/repo prod_deploy

sweforge mcp set owner/repo ./tools/mcp/servers.yaml
```

Each command materializes the complete current bundle **from the installed
immutable generation** — not from the operator source directory, which may have
changed or disappeared — applies exactly one staged change, validates the whole
resulting bundle, and installs the next generation atomically. A validation
failure leaves the current generation untouched.

Names are authoritative, not directory names: a skill's name comes from the
`name:` in `SKILL.md` frontmatter and a script's from `tool.yaml`. Adding a name
that already exists fails unless `--replace` is given. A skill or tool the
workflow still references cannot be removed.

```console
$ sweforge skill add owner/repo ./skills/reporting
Repository: owner/repo
Generation: 4
Digest: 9f2c...
Added skill: reporting
```

**Generation binding.** An IssueThread binds the repository's current generation
when it is first ingested. Later updates affect new issues only: existing
IssueThreads — including their restarts, revisions, skills, scripts and MCP
allowlists — keep the generation they were created with. Repositories with no
installed configuration fall back to the server's `--workflow-spec` /
`--capabilities-config`, or to a built-in single-task `implementation` workflow.

### Repository secrets

Runtime credentials for registered scripts and MCP servers are encrypted at
rest with Fernet, under an operator-owned master key:

- `SWEFORGE_SECRET_MASTER_KEY` — the key value directly, or
- `SWEFORGE_SECRET_MASTER_KEY_FILE` — a path to the key; its permissions must be
  `0600` or stricter, or loading fails.

```bash
sweforge secret set owner/repo DEPLOY_API_TOKEN      # hidden prompt
sweforge secret set owner/repo DEPLOY_API_TOKEN --stdin
sweforge secret list owner/repo                      # names only
sweforge secret delete owner/repo DEPLOY_API_TOKEN
sweforge secret check owner/repo                     # exits 1 if a reference is unconfigured
```

Names must match `[A-Z_][A-Z0-9_]*` and values must be at least 8 characters.
`secret check` compares the `secret_env` / `secret_headers` references in the
installed generation's manifest against what is configured, and lists what is
missing.

Configuration is referenced by name, so:

- ciphertext, never plaintext, is stored in the state database;
- **reference names are part of the configuration digest; values are not**, so
  rotating a value takes effect on the next call from existing issues without
  creating a new generation;
- values are resolved only at the execution boundary — the registered script's
  minimal process environment, or the MCP connection's env/headers;
- a missing required value fails closed with a message naming the secret;
- values are not placed in prompts, model tool schemas, skills, checkpoints,
  memory, worktrees, the generic `execute` environment, or traces, and exact
  values are redacted from script stdout/stderr and error text.

Scripts and local stdio MCP servers use `secret_env` (process environment name →
repository secret name). Remote MCP servers use fixed `headers` plus
`secret_headers` (header name → secret name); a server declaring
`secret_headers` must use `https://`, and SWEForge connects with redirects
disabled so credentials stay on the configured origin.

## Workflow model

### Tasks, phases and deterministic scheduling

A workflow specification is operator-owned YAML. It is schema-versioned,
canonicalized, hashed, and rejected before any execution for unknown fields,
malformed or duplicate ids, missing dependencies, dependency cycles, unknown
tool names, or references to skills that do not exist in the bundle.

```yaml
version: 1
workflow_id: release-readiness
tasks:
  - id: model
    depends_on: []
    planning:
      skill: domain-model
      tools: [ls, read_file, glob, grep, search_issue_memory]
    execution:
      skill: domain-model
      tools: [ls, read_file, glob, grep, edit_file, execute]
    validation:
      skill: domain-model
      tools: [read_file, glob, grep, run_validation]

  - id: reporting
    depends_on: [model]
    planning:
      skill: reporting
      skills: [domain-model]                 # discoverable on demand
      tools: [ls, read_file, glob, grep]
    execution:
      skill: reporting
      tools: [ls, read_file, glob, grep, edit_file, execute, write_readiness_report]
    validation:
      skill: reporting
      tools: [read_file, glob, grep, run_validation, propose_repo_memory]
```

Tool names come from three namespaces: built-ins (`ls`, `read_file`, `glob`,
`grep`, `write_file`, `edit_file`, `execute`, `run_validation`,
`request_clarification`, `search_issue_memory`, `propose_repo_memory`),
registered script names from `tool.yaml`, and approved MCP tools as
`<server_id>_<tool_name>`. A phase's `tools` list is the complete set of
capabilities reachable in that phase; anything absent is unreachable.

Scheduling is deterministic and serialized:

- a task is eligible when every task in `depends_on` is `DONE`;
- among eligible tasks, **declaration order** decides;
- exactly one `active_task_id` is persisted per cycle;
- the active task keeps ownership while planning, waiting for plan approval,
  executing, validating, waiting for result approval, waiting for input,
  repairing or replanning — a ready peer does not start;
- tasks are serialized even when the dependency graph has several ready nodes.

If the dispatcher fails a task terminally, the task and its cycle become
`FAILED`: no publication, no plan completion, no further model spend.

### Three levels of planning

Three separate things are called "planning", and keeping them apart is what
makes the system safe to run:

| Level | Owner | Decides | Authoritative? |
| --- | --- | --- | --- |
| Workflow DAG (`WorkflowSpec`) | operator / application | which tasks exist and in what order | yes — deterministic scheduling |
| SWEForge PLANNING phase | root agent, then a human or AUTO policy | **what** work is authorized | yes — `submit_plan` + exact approval |
| Deep Agents TODOs (`write_todos`) | model | **how** the authorized work gets carried out | no |

`write_todos` is LangChain's native `TodoListMiddleware` tool, supplied by the
Deep Agents harness. SWEForge does not reimplement it; it authorizes it, and
only during `EXECUTING`:

```
PLANNING     configured planning tools + planning skills + submit_plan + task
EXECUTING    configured execution tools + execution skills + finish_execution
             + task + write_todos
VALIDATING   configured validation tools + validation skills + run_validation
             + finish_validation + task
```

Because the rule is phase-based, it applies to revision cycles for free, and a
repository's `workflow.yaml` never has to name `write_todos` to get ordinary
execution decomposition. The name is deliberately not part of the operator tool
vocabulary — a workflow that lists it is still rejected as an unknown tool.

TODOs are adaptive working memory for a non-trivial execution: *inspect the
implementation, reproduce, make the smallest fix, update tests, run tests,
review the diff*. The model is expected to revise and reorder them as it learns
more, and to skip them entirely for trivial work.

They are never workflow authority. Writing, updating or completing every TODO
does not advance a phase, finish a task, approve a plan or result, expand what
the approved plan authorizes, affect scheduling or dependencies, or substitute
for `finish_execution` — which remains the only way out of `EXECUTING`. A
`write_todos` call still passes the normal policy path, including the
execution-permit revalidation every execution-phase root call gets, so a stale
or de-authorized execution cannot keep writing todos. The bounded read-only
investigator never receives the tool at all.

Todo state lives in the native `todos` graph channel, so it persists across the
IssueThread's checkpoint. Deep Agents marks that channel `OmitFromInput`, so
there is no supported external way to reset it and SWEForge deliberately does
not invent its own todo storage to fake one. Instead, the execution prompt
carries the current task/plan/attempt identity and requires the model to
replace any TODO that does not belong to it. Revision cycles get a separate
checkpoint identity, so they start from an empty list regardless.

### MANUAL mode

```
PENDING
  └─ PLANNING                    root agent calls submit_plan
       └─ plan comment published (versioned, digest-bound marker)
            └─ WAITING_FOR_PLAN_APPROVAL      native LangGraph interrupt
                 └─ exact authorized `@agent approve`
                      └─ EXECUTING            exact permit revalidated per call
                           └─ VALIDATING      run_validation + finish_validation
                                └─ ACCEPT → result comment published
                                     └─ WAITING_FOR_RESULT_APPROVAL
                                          └─ exact authorized `@agent approve`
                                               └─ DONE
```

`NEEDS_FIXES` returns the same task to execution. A scope-changing validation
verdict invalidates the permit and returns the task to planning. Validation
`ACCEPT` publishes the exact validated result but does **not** finish a manual
task — only exact result approval makes it `DONE`.

### AUTO mode

An `AUTO` label (case-insensitive) present on the issue **at the moment the
IssueThread is first created** persists `interaction_mode=AUTO` on that thread.
That is the only time the label is examined: adding or removing it later does
not change the mode of an existing issue, and neither do restarts or later
cycles. Databases predating this field migrate to `MANUAL`.

AUTO runs the same lifecycle:

- the plan is still built and still published as a comment;
- application policy records an exact plan permit with `AUTO` provenance;
- execution runs under the same revalidated permit;
- validation runs and must still produce `run_validation` evidence and `ACCEPT`;
- the validated result is still published as a comment;
- application policy records exact result acceptance with `AUTO` provenance.

AUTO comments say SWEForge will proceed automatically; they never instruct a
user to type `@agent approve`. AUTO does not bypass validation, publication,
lifecycle authority, deterministic task scheduling, or the publication
eligibility proof — which independently checks that permit and result-approval
provenance match the thread's recorded interaction mode.

## Skills and capabilities

Skills are repo-scoped, operator-installed **procedural knowledge**. They are
not a security mechanism: what a phase can actually do is decided by middleware
and the phase tool allowlist, never by skill text.

`skills/<name>/SKILL.md` uses Agent Skills-compatible YAML frontmatter:

```markdown
---
name: reporting
description: Render backward-compatible readiness reports with stable field ordering.
---

# Reporting
...
```

The `name` must match the installed skill directory and the `description` is
bounded (300 characters after whitespace normalization). Malformed frontmatter
fails closed; a legacy file with no frontmatter gets a deterministic fallback
description.

Disclosure is phase-authorized and progressive:

- when a phase authorizes exactly one skill, its full body is loaded eagerly
  into the system prompt;
- when a phase authorizes several (`skill` plus `skills`), the model receives a
  bounded **catalog** of names, descriptions and canonical load paths, and reads
  full bodies on demand with `read_file` (which the phase must authorize);
- only the canonical path `/skills/<name>/SKILL.md` of a skill authorized for
  the current task and phase is readable. Non-canonical, encoded, traversal or
  inactive-skill paths raise `PermissionError`.

Skill and memory trees are write-denied to every agent (`/skills/**` and
`/memories/**` deny rules).

## Tools and MCP

**Built-in tools** are Deep Agents filesystem/search tools plus SWEForge's own
`run_validation`, `request_clarification`, `search_issue_memory` and
`propose_repo_memory`.

**Registered script tools** are trusted operator scripts declared in
`tool.yaml`:

```yaml
version: 1
name: validate_release
description: Validate a release-policy JSON file and return structured diagnostics.
runtime: python                 # python | shell
entrypoint: validate_release.py # fixed; traversal and escape are rejected
effect: read                    # read | mutate
timeout_seconds: 30             # bounded (max 600)
env:
  RELEASE_REGION: example-region
secret_env:
  RELEASE_POLICY_TOKEN: RELEASE_POLICY_TOKEN
args_schema:
  type: object
  properties:
    config_path: {type: string}
  required: [config_path]
  additionalProperties: false
```

The model sees only the declared JSON schema. It cannot choose the entrypoint,
the interpreter, the environment, the timeout or the credential. Files are
staged from the installed generation into a temporary directory inside the
worktree, arguments are validated against the bounded schema subset and passed
as JSON on standard input, output is size-bounded, and the timeout is enforced.
Under strict execution the script runs through the sandbox backend; without a
sandbox and without `--unsafe-local-shell` it fails closed.

**`effect: mutate` tools are rejected in planning and validation phases** —
filtered out of the model's tool list and refused again at call time — so a
read-only phase can never be handed a writing capability.

**MCP capabilities** are repo-scoped and allowlisted in
`tools/mcp/servers.yaml`. Both local `stdio` servers (SWEForge spawns the
process with a minimal environment: `PATH`, `LANG`, the fixed `env`, and
resolved `secret_env`) and remote HTTP/streamable-HTTP servers are supported.
Remote servers carry fixed non-secret `headers` plus secret-backed
`secret_headers`; credential-bearing remote connections require HTTPS and run
with `follow_redirects=False`.

Only servers approved for the authoritative `repo_id` are loaded, only
allowlisted tool names are exposed (as `<server_id>_<tool_name>`), and an
interceptor re-authorizes **every** MCP invocation against the registry —
rejecting any call whose runtime context is missing or whose capability is not
approved for that repository, and stripping caller-supplied `workspace_root`,
`repo_path` and `tenant` fields. The model cannot invent a server, a URL, a
header or a capability.

## GitHub interaction model

Ingestion is **polling**, not webhooks, over four streams: issues, issue
comments, PR review comments, and submitted PR reviews. Each observation
becomes an immutable `SourceEvent` with a deterministic event key.

Supported surfaces: `ISSUE`, `PR_CONVERSATION`, `PR_INLINE_REVIEW` (with path,
line, side, diff hunk and anchor commits preserved) and `PR_REVIEW` (submitted
review body, with its review state recorded).

Actionability differs by surface: an **issue body** is actionable if it contains
a standalone `@agent` mention anywhere; every **comment or review body** must
*begin* with `@agent`.

Routing:

- an issue event resolves to `github:{repo_id}:issue:{n}`, creating the
  IssueThread on first sight;
- a pull-request event resolves only through a **persisted PR→IssueThread
  mapping**, which SWEForge writes itself when it creates or reconciles the
  PR for that issue;
- a human-created pull request that SWEForge did not map is not routed. Writing
  `@agent` on an unmapped PR does not create a workflow — the durable identity
  is the IssueThread, not the PR.

Feedback that reaches a mapped PR (conversation, inline review thread, or
submitted review body) is routed to the owning IssueThread. A GitHub `APPROVED`
or `CHANGES_REQUESTED` review state is recorded as provenance and carries **no**
SWEForge authority by itself.

### Approval semantics

- The only deterministic approval is the exact comment `@agent approve`
  (leading/trailing whitespace and case are tolerated; nothing else may be
  present). `@agent approve please`, `@agent approved` and `LGTM` are not
  approvals.
- It must arrive on the same surface and subject as the cycle's root input —
  and, for an inline review thread, in the same thread — after the plan or
  result comment was posted, and it must match the exact pending interrupt
  occurrence for the current plan or result.
- The approver's permission is checked against the repository: `admin`,
  `maintain` or `write`. An unauthorized exact approval is consumed as `STALE`
  and approves nothing.
- An exact approval with no matching pending occurrence is recorded as
  `STALE_APPROVAL`. It fails closed and never becomes new work.
- Any other `@agent …` comment is semantic feedback, not approval.

## Feedback and revision loops

Input that arrives while a model call is running is never injected into that
call. Every unsolicited actionable input is durably queued for a safe boundary
and gets one deterministic acknowledgement comment, posted through a
marker-reconciled outbox so retries cannot duplicate it.

**Feedback on the current plan or result.** While a task waits for plan or
result approval, an `@agent …` comment on the same surface opens a bounded
*feedback review*. In that review the model has only research tools and two
gateways, and decides one thing:

- `replan_current_feedback` — the feedback materially concerns the current
  approval scope. The same task replans, supersedes the old plan version, and
  requires a fresh exact permit against the new occurrence.
- `defer_current_feedback_to_revision` — the feedback is separate. The active
  plan or result is left untouched, a deterministic pushback comment explains
  that and repeats what approval would do, and the input is durably deferred to
  the revision loop. The original approval occurrence is retained, so the
  pending `@agent approve` still works.

**Generic revision workflow.** The structured, operator-declared workflow runs
**once** per issue. After the initial workflow completes, later independent
steering is handled by a derived single-task `revision` workflow that reuses the
same IssueThread, worktree, branch and pull request.

Its capability envelope is a deterministic **phase-wise** union of what the
original workflow was trusted with in that same phase:

| Revision phase | Skills | Tools |
| --- | --- | --- |
| PLANNING | every task's planning skills | every task's planning tools, minus mutation |
| EXECUTING | every task's execution skills | every task's execution tools |
| VALIDATING | every task's validation skills | every task's validation tools |

plus the trusted research tools the original workflow already used. Capabilities
do not leak across phases: a skill the original workflow trusted only during
execution is not advertised — or readable — while the revision is planning. A
skill legitimately named in two original phases appears in both derived sets,
because each phase is derived independently. Ordering follows task declaration
order and then each phase's own skill order, deduplicated first-occurrence-wins,
so the derivation is stable. The revision model chooses freely within that
envelope, exposed through the same progressive skill catalog.

Batching:

- when a revision cycle starts, **all currently pending revision inputs are
  batched into it** with their immutable provenance;
- inputs that arrive after that wait for the next revision cycle;
- one exception is deliberate: while a revision cycle is waiting for plan or
  result approval, newly queued *unsolicited steering* is batched into that same
  revision and triggers a replan. Deferred plan/result feedback is not, and
  stays queued for the next cycle;
- publication is blocked while a revision input is pending, so an issue
  publishes once, cumulatively, rather than per revision.

**Revision context** is assembled deterministically by the application, not
inherited from the initial conversation:

- the original issue request, marked untrusted;
- previously accepted lifecycle material for every earlier cycle — accepted plan
  text, final execution summary, and final `ACCEPT` validation summary per task
  — rendered in cycle and declaration order and **deterministically truncated**
  under a hard character bound (oldest summaries are dropped with an explicit
  marker, the first accepted identity is always retained);
- the current batched revision inputs with their immutable GitHub provenance,
  marked untrusted;
- the current revision's own repair/replan history;
- the actual current code, because it is the same worktree.

A revision does not simply inherit the raw initial model conversation: revision
cycles run under their own LangGraph checkpoint identity
(`{thread_id}:revision:{workflow_cycle_id}`).

**Clarification.** During execution the agent may call `request_clarification`
when specific missing information blocks safe continuation. That performs a
native checkpoint interrupt, the task enters `WAITING_FOR_INPUT`, and the
question is posted back to the originating surface. Resume is selected by
interrupt occurrence, so two clarifications each receive their own answer, and
an exact `@agent approve` is not accepted as an answer.

### Untrusted input handling

Every model-facing GitHub string is labelled as untrusted content in the prompt
— "Root request (untrusted)", "Original issue request (untrusted)", "untrusted
user requests", "untrusted user content" for feedback under review — and inline
review comments carry bounded, immutable provenance (author, path, lines, side,
diff hunk, anchor commits) rather than free-floating text.

That labelling is defence in depth only. The actual enforcement is structural:
application-owned workflow state, middleware phase allowlists, per-call tool
reauthorization, permit revalidation, repository and worktree boundaries,
root-only lifecycle gateways, and the sandbox boundary. Prompt-injected text
cannot reach a capability the phase does not have.

## Subagents and delegation

There is exactly one workflow-owning root Deep Agent per cycle. It may delegate
to a single explicitly configured **bounded read-only investigator**, which
replaces Deep Agents' default unrestricted worker so delegation cannot be used
to escape policy.

The investigator inherits repository, worktree, workflow, cycle, active task,
phase, MCP, filesystem and skill boundaries, and is restricted to research
tools (`ls`, `read_file`, `glob`, `grep`, `search_issue_memory`, plus
`effect: read` script tools) intersected with the phase's configured tools. It
cannot own active task or workflow state, call any lifecycle gateway, approve
anything, mutate the worktree, execute commands, broaden its authority, or
delegate further.

## Workspaces, validation and publication

**Workspace.** On the IssueThread's first workspace creation, SWEForge takes the
repository Git lock, fetches `origin main`, resolves
`refs/remotes/origin/main` to a SHA, persists that SHA as the issue base, and
creates `sweforge/issue-{n}` at exactly that commit under
`{workspace-root}/{repo_id}/issue-{n}` — without switching or pulling the source
checkout. That base is then **frozen for the life of the IssueThread**. Later
movement on `main` never silently mutates an existing issue workspace;
reopening, revising and restarting all reuse the same worktree, branch and pull
request, and a later issue simply starts from a newer base. Every reopen
verifies the persisted branch, path and that the frozen base is still an
ancestor of `HEAD`; a mismatch fails closed. A crash mid-`worktree add` is
recovered by reattaching the orphaned branch rather than refetching a new base.

**Validation.** `finish_execution` records the model's report together with
application-captured sandbox command observations and enters `VALIDATING` — it
never means `DONE`. Validation must call the application-owned `run_validation`,
which returns the cumulative Git diff against the frozen base plus the durable
execution records; `finish_validation` cannot accept without that evidence, and
its verdict is constrained to `ACCEPT` / `NEEDS_FIXES` / `REPLAN` / `BLOCKED` by
the tool schema. On `ACCEPT` SWEForge publishes one bounded, idempotently marked
result comment bound to the exact execution and validation ids. In `MANUAL`,
`ACCEPT` still requires human result approval before the task is `DONE`.

**Publication.** A final commit, push and pull request happen only after every
declared task is `DONE`, no task is active, and an independent eligibility proof
re-checks, for every task: the plan is `APPROVED` and its stored digest still
matches its text; an uninvalidated permit exists for that exact plan version
with provenance matching the thread's interaction mode; the recorded execution
succeeded under that permit and carries tool observations; the latest validation
is `ACCEPT` for that plan and attempt with `run_validation` evidence; a result
row ties plan/execution/validation together; and a non-stale result approval
matches all four identities and the recorded mode.

Publication itself is a single cumulative output for the IssueThread: it commits
only non-empty changes with the deterministic message
`sweforge: address issue #N`, pushes only the IssueThread branch, refuses a
diverged remote branch rather than force-pushing, reconciles existing pull
requests and comments by repository/branch/base and stable markers
(`<!-- sweforge:publication:<publication-id> -->`), and treats ambiguous matches
as failures. Git HTTP auth uses a short-lived installation token through a
temporary `GIT_ASKPASS` helper — never in a remote URL, Git config, SQLite, or a
command-line argument — and the credential-free HTTPS remote is derived from the
configured API host rather than trusted from the checkout. A failed publication
preserves the workspace and durable progress for retry; each publication has a
deterministic id, so a crash and retry reuse the same commit, PR and comment
instead of creating a second one.

### Locking

Three `flock`-based cross-process locks, so a crashed holder releases them: the
IssueThread lock (one thread's whole run, including model calls), the repository
Git lock (only shared Git administration — creating a thread's worktree and
branch), and the repository memory lock (one read-modify-write of `AGENTS.md`).
The only legal order is IssueThread → repository Git → release; an inversion
raises `LockOrderError` instead of deadlocking. The Git lock never spans a model
call, test run, or GitHub request, so different issues in one repository stay
fully concurrent.

## Memory

Two deliberately separate lifetimes:

**Short-term / thread memory** is the LangGraph checkpoint in
`~/.sweforge/checkpoints.sqlite`, keyed by IssueThread (and by revision cycle
for revision work). It holds conversation, model/tool continuation,
summarization and pending interrupts.

**Long-term / repository memory** is the LangGraph SQLite Store in
`~/.sweforge/memory.sqlite`, keyed by the stable GitHub `repo_id`. Namespaces:
`("sweforge","repo",repo_id,"memory")` and
`("sweforge","repo",repo_id,"skills"[,generation_id])`. One repository can never
read another's memory, skills, workspace or credentials.

The canonical file `/memories/AGENTS.md` is created with a minimal header on
first execution and loaded into the agent through Deep Agents' native `memory=`
mechanism. **Agents cannot write it**: `/memories/**` is a filesystem deny rule
and the application's evidence-backed validator is the only writer.

Repository memory is **not** operator-managed only. After a successful
publication, an application-controlled learning pass runs automatically:

- **Repository-memory curation.** Candidates come from the cumulative diff and
  from `propose_repo_memory(category, fact, durability_reason, path,
  start_line, end_line)`, which writes nothing — the model nominates only
  *where* the evidence is, and SWEForge reads those lines from the authoritative
  worktree and derives the excerpt and hash itself. Absolute or escaping paths,
  inverted or out-of-range spans and secret-looking evidence are refused.
  Candidates are re-verified against the live worktree after publication and put
  through the same validator, deduplication and repository lock. The outcome is
  recorded as `UPDATED`, `NO_UPDATE` or `FAILED`.
- **Resolved-issue memory.** Finalization creates a structured case record in the
  same transaction that marks the plan executed, so only genuinely finalized
  lifecycles produce one. Retrieval is local and deterministic (SQLite
  FTS5/BM25), always filtered by authoritative `repo_id` and capped at one
  result per IssueThread, exposed to authorized phases as `search_issue_memory`.

Both lanes are optimizations, never authorization inputs. Each claims its
attempt durably before invoking a model, is bounded to three attempts, and is
left `FAILED` with its error rather than blocking delivery. A case is what one
lifecycle diagnosed at the time; it is a clue to verify against current code,
never repository truth.

For inspecting or seeding repository memory directly, the trusted operator CLI
remains:

```bash
uv run sweforge-repo-memory --state-db ~/.sweforge/state.db \
  --memory-db ~/.sweforge/memory.sqlite --repo owner/repository show
uv run sweforge-repo-memory --state-db ~/.sweforge/state.db \
  --repo owner/repository append --text "Run tests with mvn test."
```

## Debugging and observability

```bash
sweforge-serve ... --debug-agent
sweforge-serve ... --debug-agent-tools    # implies --debug-agent
```

`--debug-agent` writes bounded, redacted, human-readable workflow/model/tool
events to stderr: model start/end, newly completed assistant-visible text, tool
and bounded-investigator boundaries, lifecycle gateways, phase transitions,
scheduler selection, validation verdicts, approval interrupts and resumes, AUTO
versus HUMAN authorization, skill catalog and skill reads, repo-config
generation binding, secret resolution counts, revision queuing/batching/replan,
publication blocks, and server dispatch. Every line carries thread, cycle, task
and phase identity where available, so concurrent workers stay distinguishable.

`--debug-agent-tools` additionally includes sanitized, size-bounded tool
arguments and results.

Bounds and redaction apply to all trace content: known credential environment
values, `Authorization`/`Bearer` headers, authenticated URLs, private-key
blocks and obvious token shapes are replaced with `[REDACTED]`, and payloads are
truncated. The tracer emits only assistant-visible message text exposed by
normal callbacks — it deliberately ignores reasoning blocks and never prints
accumulated messages, graph state or hidden chain-of-thought.

Tracing is observer-only. It rides the existing durable `invoke()` and
`Command(resume=...)` path without adding model or tool calls, changing prompts,
mutating workflow state or writing checkpoints, so it can never become workflow
authority or a new failure mode.

```text
[thread=github:1350417130:issue:12] [cycle=2] [task=A] [phase=PLANNING] [model=openai:gpt-5] MODEL START: planning_model=openai:gpt-5
[thread=github:1350417130:issue:12] [cycle=2] [task=A] [phase=PLANNING] LIFECYCLE START: submit_plan
[thread=github:1350417130:issue:12] [cycle=2] [task=A] [phase=PLANNING] WORKFLOW: A PLANNING -> WAITING_FOR_PLAN_APPROVAL
```

## Security model

- **Issue, comment and review content is untrusted.** It is labelled as such in
  prompts and, more importantly, cannot reach any capability the current phase
  does not authorize.
- **The model holds no GitHub credentials.** It invokes structured lifecycle
  behaviour; SWEForge formats and persists the authorized output, and an
  application-owned GitHub client posts comments and creates pull requests using
  GitHub App installation credentials.
- **Repository authority is immutable invocation context.** Every strict
  GitHub-triggered invocation takes its `repo_id`/`repo_full_name` from the
  persisted IssueThread and SourceEvent (`RepoAgentContext`), never from model
  text or repository files.
- **Isolated worktree per issue**, on a frozen base, under a repository-scoped
  workspace root.
- **Strict execution requires a configured sandbox provider** registered under
  the `sweforge.sandbox_backends` entry-point group. With no provider and no
  explicit opt-out, execution fails closed.
  `--unsafe-local-shell` / `LocalShellBackend` runs with host permissions and
  enforces no cross-repository isolation; it is a development escape hatch only.
- **Capability filtering at three layers**: the phase tool allowlist in the
  bound specification, middleware filtering before each model call, and
  reauthorization immediately before each tool call (plus permit revalidation
  for execution-phase calls and registry reauthorization for every MCP call).
- **Lifecycle gateways are root-only.** The delegated investigator never
  receives them.
- **Repository secret isolation.** Values are encrypted at rest, resolved only
  at the execution boundary for the exact declared reference mapping, audited,
  and redacted from output and errors.
- **No automatic cross-repository capability leakage.** Memory, skills, MCP
  approvals, secrets, workspaces, locks and configuration generations are all
  keyed by the authoritative `repo_id`.
- **GitHub App installation-token scope.** Tokens are short-lived,
  repository-scoped and split into read and write permission profiles.
- **Fail closed everywhere** required authority or evidence is absent.

## Acceptance status

SWEForge V1 was put through a live end-to-end acceptance campaign against a real
GitHub repository, a real sandbox provider and real model calls. The full
evidence — environment, per-feature results, the six product defects found and
fixed, and every remaining gap — is in
[`CAMPAIGN-FINAL-REPORT.md`](CAMPAIGN-FINAL-REPORT.md).

**Verdict: PASS WITH BLOCKED EXTERNAL COVERAGE.**

Onboarding, generation binding, incremental configuration, skills, phase tool
authority, registered scripts, secrets, stdio MCP, manual and AUTO workflows,
approval security, feedback and revision loops, clarification, all GitHub input
surfaces, base freezing, restart durability, publication, issue-resolution
memory and root-versus-investigator boundaries passed live. The named blocked or
integration-only items are:

- cross-repository *live* isolation — BLOCKED, no second safe GitHub fixture was
  available (the local integration matrix passed);
- remote HTTPS MCP authentication — local-integration pass only, no disposable
  trusted-TLS service was available (TLS was not weakened);
- same-repository concurrent mutation at the exact `EXECUTING` instant;
- a live *accepted* repository-memory candidate (the real curator returned
  `NO_UPDATE`).

The repository's own checks at campaign close: `uv run pytest -n auto` — 1195
passed, 12 skipped; `uv run ruff check .` and `ruff format --check .` clean.

### Test layers

```bash
uv run pytest -q -n auto                          # every offline layer
uv run python -m acceptance.runner.cli run S1 --layer L1
uv run python -m acceptance.runner.cli campaign S1 S2 --repetitions 3
uv run python -m acceptance.runner.cli check-exit  # campaign exit conditions
```

Deterministic scenarios under `tests/scenarios/` run with scripted model doubles
and report the named invariant that failed rather than a traceback; an invariant
that holds only over an empty set reports `VACUOUS` rather than `PASS`.
Frozen-fixture conformance replays captured review artifacts against the live
reviewer. Live GitHub targets are fail-closed: a run is refused unless the
repository is named in `SWEFORGE_ACCEPTANCE_REPOS`, and anything in
`SWEFORGE_PRIMARY_REPOS` is refused first and independently.

## Single-step and standalone CLIs

These predate `sweforge-serve` and remain available for development,
diagnostics and migration. The server is the production path and does not use
them.

```bash
# Poll only: record durable SourceEvents/IssueThreads without running an agent.
uv run sweforge-github-poll --repo owner/repository --db ~/.sweforge/state.db

# Inspect and recover execution records.
uv run sweforge-github-execution --db ~/.sweforge/state.db status
uv run sweforge-github-execution --db ~/.sweforge/state.db recover-stale
uv run sweforge-github-execution --db ~/.sweforge/state.db retry EVENT_KEY
uv run sweforge-github-execution --db ~/.sweforge/state.db skip EVENT_KEY

# Publish one eligible publication, or retry a FAILED one.
uv run sweforge-github-publish --db ~/.sweforge/state.db \
  --lock-root ~/.sweforge/locks [--retry PUBLICATION_ID]

# Advance one IssueThread's workflow a single step.
uv run sweforge-github-workflow --help

# Operator repository memory (see "Memory") and the pre-bundle skills CLI,
# which `sweforge skill add/remove` supersedes for configured repositories.
uv run sweforge-repo-memory --help
uv run sweforge-repo-skills --help

# Reviewer fixture capture/replay.
uv run sweforge-review-freeze --help
uv run sweforge-review-replay --help
```

`recover-stale` only marks a dead worker's `RUNNING` row `INTERRUPTED`, after an
age threshold and only when the host-local lock is free; it never retries
automatically. Retries preserve the worktree and LangGraph checkpoint.

### Standalone local harness

A single-process, non-GitHub harness also exists for local experimentation:

```bash
export SWEFORGE_MODEL=anthropic:claude-sonnet-5
uv run sweforge /path/to/git/repository "Fix the failing tests"
```

It creates a temporary worktree, runs one Deep Agent task, and prints the
response, changed files, base commit and diff. The worktree is retained by
default (`--discard-worktree` removes it). **This harness has no sandbox, no
workflow lifecycle, no approvals and no repository scoping** — Deep Agents'
`LocalShellBackend` executes on the host with the process user's permissions.
Do not point it at untrusted tasks or repositories.

## Conceptual boundaries

- **TOOLS** are programmatic capabilities, including approved MCP tools.
- **SKILLS** are repo-scoped procedural knowledge, not security authority.
- **MEMORY** is repo-scoped durable knowledge in the LangGraph Store.
- **STATE** is IssueThread-local workflow history and checkpoints.
- **CONTEXT** is immutable invocation authority, including repository identity.
- **MODEL** is the reasoning engine.
- **DEEP AGENTS** is the inner agent harness.
- **LANGGRAPH** is the durable orchestration/runtime.
- **SWEFORGE** owns the SWE-specific lifecycle, composition and authority.

## Further documentation

- [Adding a repository to SWEForge](docs/adding-a-repository.md) — onboarding,
  incremental maintenance and credentials, in detail.
- [`examples/README.md`](examples/README.md) — what the reference bundle
  demonstrates and how to adapt it.
- [`examples/repo-config`](examples/repo-config) — a complete, valid bundle:
  four tasks, five skills, two registered scripts, stdio and remote MCP.
- [`CAMPAIGN-FINAL-REPORT.md`](CAMPAIGN-FINAL-REPORT.md) — the V1 live
  acceptance evidence and verdict.
- [`docs/acceptance/`](docs/acceptance) — acceptance design
  ([`PLAN.md`](docs/acceptance/PLAN.md),
  [`ROADMAP.md`](docs/acceptance/ROADMAP.md)), gap analysis
  ([`COVERAGE-GAPS.md`](docs/acceptance/COVERAGE-GAPS.md)) and outcome
  ([`FINAL-STATUS.md`](docs/acceptance/FINAL-STATUS.md)).
