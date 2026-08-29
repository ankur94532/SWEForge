# SWEForge Acceptance Framework — Implementation Roadmap

Supersedes the layer-count decision in `PLAN.md` §5. All twenty-six scenarios
receive exactly one isolated integration-level PASS. No code is implemented by
this document.

## 0. Corrections adopted

| # | Change | Effect on the design |
| --- | --- | --- |
| 1 | All S1–S26 keep an isolated integration-level acceptance run | `LIVE-GITHUB` / `LIVE-PROCESS` split replaces "11 required live runs" |
| 2 | S12 and S20 are mandatory live | S12 → LIVE-PROCESS, S20 → LIVE-GITHUB |
| 3 | Track first-pass **and** bounded-eventual conformance | Two thresholds, not one |
| 4 | Unconditional versioned fixture capture on every real review | Capture is production-path, not acceptance-gated |
| 5 | Permanent reviewer regression corpus | Passing fixtures are first-class corpus members |
| 6 | Probe MCP server for S4–S9 | Deterministic, durable, survives process restart |
| 7 | Fault seam cannot activate accidentally | Two-key gate: acceptance mode **and** explicit spec |
| 8 | Structured JSONL event log | Shared predicate substrate, offline and live |
| 9 | Named invariants as reusable assertion unit | Registry + per-scenario declaration |
| 10 | PRIMARY protection mechanically enforced | Allowlist check before every mutating call |
| 11 | S21 and S23 use corpora | Payload tables, not single cases |
| 12 | No live S1, no S2, no new issue, no mutation of #16 | Gate K below |

### Two findings that shape the work

**Guard problems are free text.** `src/sweforge/reviewer.py` contains 41
`problems.append("…")` sites across six guard families. Without stable codes
there is no rejection histogram, so "no previously unseen guard family fires"
is not checkable and the Class A / Class B split cannot be automated.
**Introducing stable guard codes is a prerequisite for conformance, not a
nicety.**

**`ReviewFinalizationError` is caught nowhere in `src/`.** It escapes
`WorkflowEngine.advance()` into `SWEForgeServer._worker_entry`, which records a
`dispatcher_failures` row. That backoff is durable and capped at 3600s of
*delay* but has **no terminal count** — a permanently failing review retries
forever at one attempt per hour. S17 and S26 pass conditions must be written
against the intended bound; see §J note.

## A. Architecture — files to add and modify

### A.1 Production `src/sweforge/` — six new modules, four modified

| File | Status | Responsibility | Why it must live in `src/` |
| --- | --- | --- | --- |
| `acceptance_mode.py` | new | `acceptance_enabled()`, `require_acceptance(feature)`. Single authority for the safety gate. | The gate must be evaluated by production startup paths |
| `faults.py` | new | Fault registry: `hit(point, **ctx)`, spec loading, drain assertions | Faults fire inside real dispatcher processes |
| `events.py` | new | `emit(kind, **fields)` → JSONL; field allowlist; redaction | Emission points are inside the workflow |
| `guard_codes.py` | new | `GuardCode` StrEnum + `GuardProblem(code, detail)` | Referenced by reviewer guards |
| `review_fixture.py` | new | Versioned fixture writer/reader, redaction, worktree snapshot | Capture happens on the real review path |
| `reviewer_models.py` | new | `ReviewerModelFactory` protocol + default | Removes 4-seam monkeypatching |
| `reviewer.py` | modify | 41 `problems.append` → `GuardProblem`; factory seam; 3 fault points | — |
| `workflow.py` | modify | Capture hook in `_run_review_locked`; ~18 event emissions; 4 fault points | — |
| `server.py` | modify | Ready-file write; acceptance-mode startup validation; 2 fault points | — |
| `server_cli.py` | modify | `--ready-file`; reject `SWEFORGE_FAULT_SPEC` without acceptance mode | — |

Total production surface: six small new modules plus mechanical edits. Nothing
changes workflow semantics.

### A.2 `tests/` — the deterministic layer

```
tests/conftest.py                    shared fixtures; replaces zero-conftest status quo
tests/harness/
  world.py          World: origin repo, checkout, 3 SQLite DBs, workspace/lock roots,
                    wired WorkflowEngine; tick() / drive(until=…)
  github_fake.py    ONE FakeGitHub over the GitHubClient Protocol + call ledger
                    (replaces the 3 duplicates in test_clarification_resume_identity,
                     test_github_poller, test_publication_identity)
  models.py         ScriptedModel · ReplayModel · RealModel · FaultyModel
  observation.py    Observation bundle: event log + store + git + GitHub ledger
  invariants.py     the named registry (§D)
  scenario.py       @scenario decorator, result records, campaign-status.json
  serve.py          spawn/kill/restart sweforge-serve; wait-for-ready
tests/acceptance/l1/     test_s01.py … test_s26.py   (deterministic representation)
tests/conformance/       runner tests + threshold assertions (no provider in CI)
```

### A.3 `acceptance/` — never installed, never imported by `src/`

```
acceptance/
  probes/server.py          MCP probe server (§H)
  probes/ledger.sqlite      durable probe call ledger (per run)
  runner/cli.py             sweforge-acceptance run S07 --layer live-process
  runner/allowlist.py       PRIMARY protection (§ mechanical enforcement)
  runner/reap.py            standalone cleanup for crashed runs
  scenarios/s01.py … s26.py integration-level scenario definitions
  fixtures/review/…         the frozen corpus (§E)
  reports/                  campaign-status.json, histograms, per-run evidence
```

`acceptance/` is excluded from the wheel (`pyproject.toml` already packages only
`src/sweforge`), so probe and fault tooling can never ship.

## B. Dependency graph

```
M0 guard codes ──────────┐
   (reviewer.py, 41 sites)│
                          ├──> M3 conformance runner ──> M6 GATE K ──> M7 live campaign
M1 fixture schema ───────┤         (§F)                    (§K)          S1…S26
   + capture hook         │
   + #16 replay           │
                          │
M2 event log ────────────┬┴──> M4 invariant registry ──> M5 L1 scenarios ──┘
   (events.py)           │          (§D)                    S1…S26
                         │
M1b acceptance_mode ─────┤
    + faults.py ─────────┴──> M4b fault-dependent L1 ──> M5
                         │
M1c probe MCP server ────┴──> M4c probe-dependent L1 ──> M5
```

Critical path to unblocking S1 is **M0 → M1 → M3 → M6**. The invariant registry
and L1 scenarios are parallel work that does not gate the #16 diagnosis.

| Milestone | Deliverable | Blocks |
| --- | --- | --- |
| **M0** | Stable guard codes; every guard emits `GuardProblem` | M3 |
| **M1** | Fixture schema v1, capture hook, replay CLI, **#16 frozen** | M3, M6 |
| **M1b** | `acceptance_mode` + `faults` + startup rejection | M4b |
| **M1c** | Probe MCP server + durable ledger | M4c |
| **M2** | `events.py` + emission points + JSONL schema | M4 |
| **M3** | Conformance runner, dual thresholds, histogram | M6 |
| **M4** | Invariant registry + `Observation` bundle | M5 |
| **M5** | 26 deterministic L1 scenarios, all PASS | M7 |
| **M6** | Gate K satisfied (§K) | M7 |
| **M7** | 26 integration-level acceptance runs | exit |

## C. Placement rules

| Belongs in | Rule | Examples |
| --- | --- | --- |
| `src/sweforge/` | Code that must execute on the real production path, or that production code imports | fixture capture, event emission, fault `hit()` call sites, guard codes, model factory |
| `tests/` | Code that imports `src/` and runs with no network and no provider | harness, fakes, L1 scenarios, conformance runner *tests* |
| `acceptance/` | Code that drives real processes, real GitHub, or real providers; never imported by `src/` or by CI unit tests | probe MCP server, scenario runner, allowlist, reaper, corpus |

The one deliberate asymmetry: **fixture capture and fault `hit()` sites live in
`src/`**, because both must fire inside a real dispatcher. Both are inert
without explicit configuration, and the fault gate is two-key (§G).

## D. Named invariant registry

### D.1 Shape

```python
@dataclass(frozen=True, slots=True)
class Invariant:
    id: str  # INV-ONE-INITIAL
    description: str
    scope: Literal["thread", "cycle", "repo", "campaign"]
    predicate: Callable[[Observation], InvariantResult]


@dataclass(frozen=True, slots=True)
class InvariantResult:
    ok: bool
    detail: str = ""  # what was actually observed
    evidence: dict | None = None
```

`Observation` is the single bundle every predicate reads, assembled identically
offline and live — which is what lets one predicate serve both layers:

```python
@dataclass(frozen=True, slots=True)
class Observation:
    events: list[dict]  # SWEFORGE_EVENT_LOG, parsed
    store: SQLiteGitHubStore  # durable state
    thread_ids: frozenset[str]  # this scenario's threads
    repo_ids: frozenset[int]
    git: GitFacts  # branches, commits, pushes, force-pushes
    github: GitHubFacts  # FakeGitHub ledger OR live REST readback
    probes: ProbeLedger | None
    faults: FaultLedger | None
```

`GitHubFacts` has two implementations with one interface: ledger-backed
offline, REST-readback live. Predicates never know which.

### D.2 Registry (30 invariants)

| ID | Asserts |
| --- | --- |
| `INV-ONE-ROOT` | Exactly one durable root event per scenario thread |
| `INV-PLAN-CANONICAL` | One canonical plan per (thread, cycle); no duplicate posted plan comment |
| `INV-PLAN-VERSIONED` | Revisions supersede, never fork |
| `INV-PERMIT-BOUND` | Permit binds exact plan id, version, thread, cycle, source |
| `INV-PERMIT-NONE` | No `ExecutionPermit` row exists |
| `INV-PERMIT-SOURCE` | Permit source is exactly USER or AUTO as the scenario requires |
| `INV-ONE-INITIAL` | Exactly one INITIAL attempt per cycle |
| `INV-NO-HOT-RETRY` | No further attempt after terminal `EXECUTION_FAILED` |
| `INV-RETRY-BOUNDED` | `retry_count` never exceeds its configured bound |
| `INV-ATTEMPT-TERMINAL` | Every attempt ends in a terminal status; none left RUNNING |
| `INV-EVIDENCE-CONTIGUOUS` | Execution evidence rows form one unbroken cycle-scoped sequence |
| `INV-EVIDENCE-TRUSTED` | Every review evidence id resolves to a persisted execution observation |
| `INV-REVIEW-GROUNDED` | Every accepted requirement cites a read-ledger entry or execution evidence id |
| `INV-REVIEW-LEDGER-FRESH` | A review retry starts an empty read ledger |
| `INV-REVIEW-NO-REPAIR-ON-INFRA` | Review infrastructure failure never enters `REVIEW_REPAIR` |
| `INV-ONE-PUBLICATION` | One commit, one non-force push, one PR, one completion comment |
| `INV-PUB-MAPPING` | PR ↔ IssueThread mapping exact and unique |
| `INV-PUB-AUTHORIZED` | Publication cites this cycle's ACCEPT review only |
| `INV-NO-PUBLICATION` | No commit, push, PR or comment exists |
| `INV-NO-EMPTY-COMMIT` | No commit for a no-change lifecycle |
| `INV-NO-FALSE-RESOLUTION` | No `IssueResolution` row for unfinalized work |
| `INV-NO-FALSE-MEMORY` | No repo-memory candidate accepted without verified worktree lines |
| `INV-LEARNING-ISOLATED` | Curator failure leaves publication COMPLETED |
| `INV-PROVENANCE` | `root_event_key` and `root_input_id` preserved across every layer |
| `INV-DEFERRED-PRESERVED` | Residual follow-up retains its own `deferred_id` |
| `INV-NO-INJECTION` | No live input delivered into execution or `REVIEW_EXECUTION` |
| `INV-THREAD-ISOLATION` | No state written outside the scenario's own thread ids |
| `INV-REPO-ISOLATION` | No cross-repo memory, skills, workspace or capability access |
| `INV-WORKTREE-CONFINED` | No filesystem effect outside the issue worktree |
| `INV-LOCK-ORDER` | Thread lock → repo git lock ordering never inverted |

### D.3 Declaration and reporting

```python
@scenario(
    "S16",
    layer=Layer.LIVE_PROCESS,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-PERMIT-BOUND",
        "INV-RETRY-BOUNDED",
        "INV-ATTEMPT-TERMINAL",
        "INV-PROVENANCE",
        "INV-THREAD-ISOLATION",
    ],
    faults=["execution.after_attempt_persist"],
)
def s16_sigkill_during_initial(world): ...
```

Report is per-invariant, never a bare pass/fail:

```
S16  FAIL
  INV-ONE-INITIAL          PASS  1 INITIAL attempt (attempt-a1f3)
  INV-PERMIT-BOUND         PASS  permit-77c bound to plan-4b2 v1
  INV-RETRY-BOUNDED        FAIL  retry_count=4, bound=3
  INV-ATTEMPT-TERMINAL     PASS
  INV-PROVENANCE           PASS
  INV-THREAD-ISOLATION     PASS
  + scenario predicate: orphan detected on restart   PASS
```

## E. Review fixture schema and replay

### E.1 Capture point

`WorkflowEngine._run_review_locked` builds the complete evidence dict before
calling `self.reviewer(...)`. Capture wraps exactly that call site: write the
input bundle before, append the outcome after — success or exception.

**Capture never fails a review.** Any capture error is swallowed, counted, and
logged; the review proceeds.

### E.2 Schema v1

```
acceptance/fixtures/review/v1/RF-016-inspector-authority/
  fixture.json            envelope: schema_version, fixture_id, captured_at,
                          sweforge_git_sha, review_model, capture_reason
  provenance.json         repo_full_name, repo_id, issue_number, thread_id,
                          cycle_id, root_event_key, root_input_id,
                          execution_id, attempt_id, attempt_kind,
                          review_iteration, plan_id, plan_version
  evidence.json           the exact dict passed to review_execution:
                            plan{id,version,text} · execution · attempt
                            current_head · base_head · execution_observations
                            changed_files · diff · dirty
                            source · source_request · previous_review?
  contract.json           review_requirement_contract(evidence) at capture time
  context.json            ReviewerContext fields: worktree (relative),
                          repo_context{repo_id,repo_full_name,thread_id},
                          memory_namespace, memory_snapshot?
  worktree.tar.zst        worktree snapshot sufficient for read_repo_file
  outcome.json            verdict · requirement_checks · findings ·
                          repair_instructions · inspection_report ·
                          challenge/semantic artifact
  ledger.json             read ledger entries (paths, offsets, returned_lines)
  diagnostics.json        on failure: exception type, message, GuardProblem[]
  expected.json           hand-authored where known: expected verdict class,
                          per-requirement status, repairability
```

Three deliberate choices:

- **`contract.json` is captured *and* re-derived on replay, then compared.**
  The contract is a pure function of evidence, so storing it turns
  contract-derivation regressions into fixture failures rather than silent drift.
- **`worktree.tar.zst` is mandatory.** `read_repo_file` resolves against
  `context.worktree`; evidence alone cannot reproduce a review.
- **`expected.json` is optional and human-authored.** Absent for pure
  regression fixtures, present for semantic-correctness fixtures.

### E.3 Redaction

Reuse `execution_evidence.sanitize()` (private keys, bearer tokens,
`name=value` secret patterns, live env secret values) and extend for fixtures:

- never write installation tokens, JWTs, `GIT_ASKPASS` paths, or `.env`
- worktree snapshot excludes `.git/config`, `.git/credentials`, `.env*`,
  `**/*.pem`, `**/id_*`
- `provenance.json` carries identifiers only, never issue author email
- a post-write scan re-runs the secret regexes over every file; a hit
  quarantines the fixture instead of publishing it

### E.4 Replay

```bash
uv run sweforge-review-replay acceptance/fixtures/review/v1/RF-016-* \
  --model anthropic:claude-sonnet-4-6 --runs 20 --report reports/rf016.json
```

Replay unpacks the worktree to a temp dir, rebuilds `ReviewerContext` against
it, re-derives and compares the contract, then calls the real
`review_execution`. `--offline --replay-cassette` swaps in `ReplayModel` for
CI regression use without a provider.

## F. Conformance runner

### F.1 Metrics

Per fixture, per run, classify the outcome:

| Class | Definition | Owner | Tolerance |
| --- | --- | --- | --- |
| **A** | Model output was semantically valid but a deterministic SWEForge guard rejected it | SWEForge | **zero** |
| **B** | Model failed to emit sufficient or correct semantic facts | model/prompt | counted against rate |
| **OK** | Accepted artifact | — | — |

Class A vs B is decided mechanically from the `GuardCode` emitted (M0), plus an
adjudication file for genuinely ambiguous codes. A code with no classification
is reported as `UNCLASSIFIED` and blocks the run — new guard families cannot
slip through unnoticed.

### F.2 Dual thresholds

```
FIRST-PASS CONFORMANCE      >= 95%   measured BEFORE review_execution's
                                     internal range(2) correction retry
BOUNDED EVENTUAL            = 100%   for fixtures with a known-valid expected
                                     outcome, after the allowed correction
CLASS A RATE                = 0%     any occurrence blocks the component
UNCLASSIFIED GUARD CODES    = 0      any occurrence blocks the run
```

`19/20` first-pass with `20/20` eventual passes. `12/20` first-pass with
`20/20` eventual fails — that is the reviewer-architecture signal the campaign
exists to surface. Instrumenting first-pass requires the runner to observe the
attempt boundary directly, so `review_execution` gains an optional
`attempt_observer` callback rather than the runner inferring it from logs.

### F.3 Output

```json
{"fixture":"RF-016-inspector-authority","runs":20,
 "first_pass":{"ok":17,"class_a":0,"class_b":3,"rate":0.85},
 "eventual":{"ok":20,"rate":1.00},
 "guard_histogram":{"IA-MISSING-DIRECT-CODE-OBS":2,"IA-OBS-PATH-MISMATCH":1},
 "verdict":"FAIL: first-pass 0.85 < 0.95"}
```

The histogram is the actionable artifact: it names which guard to loosen or
which prompt to fix, instead of "review failed".

### F.4 Seven conformance targets

reviewer-inspection · reviewer-specialists · reviewer-challenge ·
reviewer-finalize · planner · clarification-classifier · curators
(repo-memory and resolution measured together, thresholds separate).

## G. Fault registry and safety gating

### G.1 Two-key gate

```
SWEFORGE_ACCEPTANCE_MODE=1          key 1 — explicit acceptance/test mode
SWEFORGE_FAULT_SPEC=/path/spec.json key 2 — explicit named fault spec
```

Enforced rules, checked at startup and again at every `hit()`:

1. A fault spec **without** acceptance mode is a **startup failure**, not a
   warning and not a silent ignore.
2. Acceptance mode without a spec is legal and injects nothing.
3. Acceptance mode is refused when GitHub App credentials resolve to a
   repository on the PRIMARY allowlist (§ mechanical enforcement).
4. `hit()` is a no-op returning immediately when either key is absent — the
   production cost is one module-level boolean check.
5. Acceptance mode is recorded in every event-log envelope, so any run that had
   faults available is identifiable forever after.

### G.2 Spec and API

```json
{"version": 1,
 "scenario": "S26",
 "faults": [
   {"point": "review.before_inspector", "action": "raise",
    "exc": "RuntimeError", "message": "injected review infra failure",
    "on_calls": [1], "max_fires": 1}
 ]}
```

```python
faults.hit("review.before_inspector", thread_id=..., cycle_id=...)
```

Every fault is **named, deterministic** (`on_calls` by index, never
probabilistic), **bounded** (`max_fires`, default 1), and **observable** (each
firing emits a `FAULT_FIRED` event and appends to a fault ledger).

### G.3 No silent persistence

- The registry is process-local and dies with the process.
- Scenario teardown asserts the fault ledger **drained**: every declared fault
  fired exactly its expected number of times. An undrained fault fails the
  scenario, so a fault that quietly never fired cannot produce a false PASS.
- A scenario declaring no faults asserts the ledger is empty.

### G.4 Injection points

| Point | Serves |
| --- | --- |
| `execution.before_attempt_persist` / `.after_attempt_persist` | S16 (precise SIGKILL instant) |
| `execution.during_tool_call` | S8 timeout, S16 |
| `orphan.recover` | S17 exhaustion |
| `review.before_inspector` / `.before_finalize` | S26 |
| `publish.before_push` / `.after_push_before_pr` | S1 idempotency, publication retry |
| `curator.repo_memory` | S24 |
| `curator.resolution` | S25 |
| `probe.timeout` | S8 |

`execution.after_attempt_persist` supports `action: "block_on_fifo"` so the
scenario runner knows the exact instant the attempt row is durable and can
`SIGKILL` deterministically rather than racing a sleep.

## H. MCP probe server

### H.1 It needs no production change

`RepoCapabilityRegistry` loads operator-owned MCP servers from
`--capabilities-config`, and `load_repo_mcp_tools` prefixes tools with
`{server_id}_`. Registering server id **`acceptance`** with tools
`retryable_probe`, `nonretryable_probe`, `warning_probe`, `fatal_probe`,
`timeout_probe`, `identity_echo` yields exactly the names already referenced in
`tests/test_execution.py` — `acceptance_retryable_probe` and siblings. The
probe server is pure acceptance tooling.

```json
{"servers": {"acceptance": {"command": "uv",
   "args": ["run", "python", "acceptance/probes/server.py"],
   "transport": "stdio",
   "env": {"SWEFORGE_PROBE_LEDGER": "/run/s04/ledger.sqlite"}}},
 "repositories": {"901234567": {"acceptance": [
   "retryable_probe","nonretryable_probe","warning_probe",
   "fatal_probe","timeout_probe","identity_echo"]}}}
```

### H.2 Durable, deterministic behaviour

State lives in `SWEFORGE_PROBE_LEDGER` (SQLite), not process memory, so
invocation counts survive the process boundaries S15–S17 require.

| Tool | Behaviour | Determinism |
| --- | --- | --- |
| `retryable_probe(operation_id)` | Fails retryably on call 1 for an `operation_id`, succeeds on call 2 | Keyed by `operation_id`, persisted |
| `nonretryable_probe(operation_id)` | Always permanent failure | Ledger proves exactly one call |
| `warning_probe(operation_id)` | Returns success carrying a warning payload | Distinct from both success and failure |
| `fatal_probe(operation_id)` | Raises a hard execution failure | — |
| `timeout_probe(operation_id, seconds)` | Sleeps past the configured bound | Deadline read from the ledger |
| `identity_echo()` | Returns the `repo_id` / `repo_full_name` it actually received | See below |

### H.3 `identity_echo` is the S9 assertion surface

`repo_scope_interceptor` injects `repo_id` and `repo_full_name` from the
authoritative `RepoAgentContext` and strips `workspace_root`, `repo_path`,
`tenant` from model-supplied args. `identity_echo` takes **no arguments** and
returns what it received. S9 then asserts mechanically: whatever the issue body
or model output claims, the echoed identity equals the trusted context — and
any model-supplied `repo_id` was discarded.

Every probe call appends `{ts, tool, operation_id, repo_id, repo_full_name,
args_received, outcome}` to the ledger. `ProbeLedger` in the `Observation`
bundle makes call-count assertions ordinary invariants.

### H.4 Sync/async note

`tests/test_execution.py` already records that an async-only `StructuredTool`
raises `NotImplementedError: StructuredTool does not support sync invocation`
when reached through delegation. The probe server must expose tools invocable
on the executor's actual call path, and S4's L1 representation must pin that
path so the probes cannot mask a regression the existing test already caught.

## I. Event-log schema

### I.1 Envelope

```json
{"v":1,"ts":"2026-08-24T10:15:03.221Z","seq":47,
 "run_id":"s16-20260824-a1f3","acceptance_mode":true,
 "kind":"EXECUTION_STARTED",
 "thread_id":"…","cycle_id":3,"repo_id":901234567,
 "data":{…}}
```

Written to `SWEFORGE_EVENT_LOG` as JSONL, append-only, one line per durable
transition, flushed before the transition's transaction commits so a crash
cannot lose the event that explains it.

### I.2 Event kinds

```
ROOT_INGESTED          PLAN_CREATED           PLAN_POSTED
PLAN_REVISED           PLAN_SUPERSEDED        APPROVAL_OBSERVED
APPROVAL_REJECTED      PERMIT_CREATED         PERMIT_VALIDATED
EXECUTION_STARTED      EXECUTION_SUCCEEDED    EXECUTION_FAILED
EXECUTION_ORPHANED     EXECUTION_RECOVERED    CLARIFICATION_REQUESTED
CLARIFICATION_ANSWERED INPUT_DEFERRED         INPUT_DELIVERED
REVIEW_ATTEMPT         REVIEW_ACCEPTED        REVIEW_NEEDS_FIXES
REVIEW_BLOCKED         REVIEW_INFRA_FAILED    REPAIR_AUTHORIZED
PUBLICATION_STARTED    COMMIT_CREATED         BRANCH_PUSHED
PR_CREATED             PR_REUSED              COMMENT_POSTED
PUBLICATION_COMPLETED  LEARNING_STARTED       LEARNING_RESULT
PHASE_CHANGED          LOCK_ACQUIRED          LOCK_CONTENDED
FAULT_FIRED            FIXTURE_CAPTURED
```

### I.3 Redaction is structural, not filtered

`data` accepts an **allowlist of scalar fields per event kind** — identifiers,
counts, statuses, SHAs, PR numbers, comment ids, durations. Free-form text,
model messages, reasoning, plan bodies, issue bodies, diffs and tool output are
**not representable** in the schema. A disallowed key is a programming error
that fails in tests, so the log cannot leak by accident.

## J. Scenario matrix S1–S26

`DET` deterministic L1 · `CONF` frozen-fixture conformance ·
`LP` LIVE-PROCESS · `LG` LIVE-GITHUB. Every scenario has exactly one
integration-level run.

| # | Scenario | DET | CONF | Integration | Faults / probes | PASS conditions |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | Happy path & publication | ● | reviewer-finalize | **LG** | — | `INV-ONE-ROOT` `INV-PLAN-CANONICAL` `INV-PERMIT-BOUND` `INV-ONE-INITIAL` `INV-REVIEW-GROUNDED` `INV-ONE-PUBLICATION` `INV-PUB-MAPPING` `INV-PROVENANCE` |
| S2 | Clarification gauntlet | ● | clarification | **LG** | — | 4 answer types each resume the same cycle; `INV-DEFERRED-PRESERVED`; unrelated input does not answer |
| S3 | Scope change invalidates auth | ● | clarification | **LP** | — | old permit stale; `INV-PERMIT-NONE` for old plan; new plan version; fresh approval required |
| S4 | Retryable probe | ● | — | **LG** | `retryable_probe` | exactly 2 probe calls, 1 attempt; no duplicate side effect; `INV-ONE-INITIAL` |
| S5 | Non-retryable probe | ● | — | **LP** | `nonretryable_probe` | exactly 1 probe call; `INV-NO-HOT-RETRY` |
| S6 | Warning-class result | ● | — | **LP** | `warning_probe` | exactly 1 call; warning observable; execution continues |
| S7 | Fatal execution failure | ● | — | **LP** | `fatal_probe` | terminal `EXECUTION_FAILED`; `INV-NO-HOT-RETRY` `INV-NO-PUBLICATION` `INV-NO-FALSE-RESOLUTION` |
| S8 | Timeout handling | ● | — | **LG** | `timeout_probe`, `execution.during_tool_call` | bound enforced; correct failure class; `INV-ATTEMPT-TERMINAL` |
| S9 | Identity spoof resistance | ● | reviewer-inspection | **LG** | `identity_echo` | echoed identity equals trusted context; model-supplied `repo_id` discarded; `INV-REPO-ISOLATION` |
| S10 | Same-repo concurrency | ● | — | **LG** | — | `INV-LOCK-ORDER` `INV-THREAD-ISOLATION`; distinct branches; overlap observed in event timestamps |
| S11 | Cross-repo concurrency | ● | — | **LG** | — | `INV-REPO-ISOLATION`; no serialization between repos |
| S12 | Backlog / queue draining | ● | — | **LP** | — | every queued thread runs exactly once; none lost or duplicated; capacity released |
| S13 | Thread lock contention | ● | — | **LP** | — | one worker proceeds, other backs off; no duplicate planning/execution/publication |
| S14 | Singleton server | ● | — | **LP** | — | second `sweforge-serve` rejected cleanly; only one dispatcher polls |
| S15 | Restart awaiting approval | ● | — | **LG** | — | plan remains canonical; no duplicate plan comment; approval resumes same plan version |
| S16 | SIGKILL during INITIAL | ● | — | **LG** | `execution.after_attempt_persist` (fifo) | orphan detected; `INV-ONE-INITIAL` `INV-PERMIT-BOUND` `INV-RETRY-BOUNDED`; same workspace/branch reused |
| S17 | Recovery exhaustion | ● | — | **LP** | `orphan.recover` (fail ×N) | bound respected; durable terminal state; **no infinite resurrection** — see note |
| S18 | Unsolicited follow-up | ● | — | **LG** | — | `INV-NO-INJECTION` `INV-DEFERRED-PRESERVED` `INV-PROVENANCE` |
| S19 | No-change execution | ● | reviewer-finalize (no-change class) | **LG** | — | `INV-NO-EMPTY-COMMIT` `INV-NO-PUBLICATION`(PR); distinguished from failure; no fabricated change evidence |
| S20 | Unmapped PR mention | ● | — | **LG** | — | fails closed; no thread attachment; no durable identity created; `INV-THREAD-ISOLATION` |
| S21 | Sandbox escape corpus | ● | — | **LP** | — | every corpus payload confined; `INV-WORKTREE-CONFINED` for all |
| S22 | Memory / skills write denial | ● | — | **LP** | — | `/memories/**` `/skills/**` denied; `INV-NO-FALSE-MEMORY` |
| S23 | Execution without approval corpus | ● | — | **LP** | — | `INV-PERMIT-NONE` for every near-miss payload; no attempt begins |
| S24 | Repo-memory curator failure | ● | curators | **LP** | `curator.repo_memory` | `INV-LEARNING-ISOLATED` `INV-NO-FALSE-MEMORY`; bounded; observable |
| S25 | Resolution curator failure | ● | curators | **LP** | `curator.resolution` | publication stays COMPLETED; `INV-NO-FALSE-RESOLUTION` |
| S26 | Transient review failure | ● | reviewer-inspection | **LP** | `review.before_inspector` | `INV-REVIEW-LEDGER-FRESH` `INV-REVIEW-NO-REPAIR-ON-INFRA`; no new permit; INITIAL reused; later review ACCEPTs |

**LIVE-GITHUB: 12 · LIVE-PROCESS: 14 · integration runs: 26.**

> **Note on S17 and S26.** `ReviewFinalizationError` is caught nowhere in
> `src/`; it surfaces as a `dispatcher_failures` row whose delay caps at 3600s
> but whose count never terminates. Before S17 and S26 can have PASS
> conditions, decide the intent: (a) review infrastructure failure retries
> indefinitely at a capped interval — then `INV-RETRY-BOUNDED` does not apply
> and S17 asserts only the execution `retry_count` bound of 3; or (b) it
> terminates after N — then a durable review-attempt counter and a terminal
> state must be added first. **This is a design decision, not a test decision**,
> and it blocks writing those two scenarios.

## K. The exact gate before S1 may run again

All eight conditions, machine-checked, before any live S1:

```
K1  Fixture schema v1 implemented; capture hook live in _run_review_locked;
    capture proven non-fatal (a capture exception does not fail a review).

K2  Issue #16 frozen as a v1 fixture from existing artifacts, WITHOUT
    mutating #16 and WITHOUT creating any issue.

K3  Guard codes (M0) complete: all 41 sites emit GuardProblem; every code
    present in guard_classification.json. UNKNOWN is a legitimate standing
    value -- it means "not yet observed against real model output", and is
    resolved by evidence in K4, never by reasoning. What must be zero is a
    code MISSING from the file, which the coverage test already enforces.
    Status: 41 codes, 25 classified B, 16 UNKNOWN (the IA-* authority
    family plus FA-INSPECTION-COVERAGE and FA-CHALLENGE-COVERAGE).

K4  All three #16 inspector failures reproduced offline from the frozen
    fixture, each attributed to a specific GuardCode, and each resolved
    from UNKNOWN to A or B on the evidence of what the model actually
    emitted.

K5  All three diagnosed and fixed, with the fix justified as either
    "guard was wrong" (Class A) or "prompt/contract was wrong" (Class B).

K6  Regression corpus contains at least one PASSING pre-fix fixture; the
    fix does not regress it. This is the condition that stops a #16 fix
    from breaking a previously valid review shape.

K7  Conformance over #16 plus the corpus with the actual configured review
    model: first-pass >= 95%, bounded-eventual = 100%, Class A = 0,
    across 20 consecutive runs.

K8  S1's deterministic L1 scenario PASSes with its full invariant set, and
    PRIMARY allowlist enforcement is proven by a negative test — the
    harness refuses a PRIMARY target.
```

Until K1–K8 all hold: no live S1, no S2, no new GitHub issue, no mutation of
#16.

### K7 model scope (decided 2026-08-25)

`openai:gpt-5.6-luna` is the certification model and the production
candidate. `openai:gpt-5.6-sol` is cost-constrained and is reduced to a
single 1×8 generalization check, run only if luna certifies clean.

The cross-model check is not ceremony. It is what proved the guards were
not overfit to one model's evidence *style*: for RF-016 luna enumerated
all five files under `src/main/java` while sol named the directory. Both
were semantically complete; only sol's form satisfied the guard, and that
asymmetry is how the Class A defect in `_absence_scope_covers_target` was
found. Certifying on a single model would have concealed it.

So if the sol check is dropped entirely, K7 certifies "the guards hold for
luna", not "the guards are model-general", and the certification record
must say so rather than imply the stronger claim.

Replaying the stored sol 20×8 artifacts is NOT a substitute. They predate
the ABSENCE_OF_CHANGE schema, so current guards reject them for missing
evidence the model was never asked to emit — the 118 spurious
IA-MISSING-ABSENCE-OF-CHANGE changes already observed. Replay cannot
predict how a model answers a changed prompt.

## Sequencing summary

```
M0 guard codes ──┐
M1 fixture + #16 ─┼──> diagnose 3 inspector failures offline ──> M3 conformance ──> GATE K
M1b faults        │
M1c probes ───────┤
M2 event log ─────┴──> M4 invariants ──> M5 twenty-six L1 scenarios ──┐
                                                                       ├──> M7
                                                        GATE K ────────┘
M7: S1 → S19 → S2 → S18 → S15 → S16 → S4 → S8 → S9 → S20 → S10 → S11   (LIVE-GITHUB)
    interleaved with S3 S5 S6 S7 S12 S13 S14 S17 S21 S22 S23 S24 S25 S26 (LIVE-PROCESS)
```
# Declarative workflow milestone

The generic serial scheduler, versioned trusted workflow schema, task-run
persistence, immutable per-issue MANUAL/AUTO policy, exact plan permits,
validation/repair transitions, exact validated-result approvals, phase policy,
progressive skill disclosure, bounded investigator delegation, and all-tasks
publication gate are implemented and covered offline. MANUAL has distinct plan
and result approval barriers; AUTO records both authorities without human
waits. A future live campaign may exercise the A -> {B,C} -> D fixture against
an external repository; this refactor intentionally does not create or
configure that repository.
