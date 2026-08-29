# SWEForge Acceptance Campaign — Execution Plan

Status: proposed. Owner: campaign lead. Scope: scenarios S1–S26.

## 1. Diagnosis: why the current campaign is expensive

The campaign is currently discovering roughly one model/output defect per fresh
GitHub issue. That is a structural consequence of three properties of the code,
not of insufficient care:

1. **The reviewer fails closed behind many independent guards.**
   `src/sweforge/reviewer.py` is a multi-stage pipeline — inspection → resolved
   evidence → clustering → specialists → challenge → finalize — with at least
   fifteen distinct guard families (`_inspection_artifact_problems`,
   `_artifact_problems`, `_semantic_artifact_problems`, `_reference_problems`,
   `_finding_provenance_errors`, `_guard_accept_coverage`,
   `_guard_repairability`, …). Each can reject a real model's structurally
   plausible output and raise `ReviewFinalizationError`. Every such rejection is
   currently found by burning an issue.

2. **The failing input is serializable but is never retained.**
   `review_execution(*, context, model, evidence: dict)` takes a plain dict.
   Once a live review fails, that dict is discarded, so the next attempt to
   reproduce the failure requires another live run.

3. **There is no acceptance substrate in the tree.**
   There is no `conftest.py`, no shared harness, three separate `FakeGitHub`
   classes (`test_clarification_resume_identity.py`, `test_github_poller.py`,
   `test_publication_identity.py`), and no scenario registry. The 26 scenarios
   exist only as process. Nothing produces an attributable per-scenario result.
   The probe tools S4–S9 depend on do not exist: `acceptance_retryable_probe`
   appears only as a literal string in `tests/test_execution.py`.

The plan below fixes cause (2) first, because a frozen evidence corpus converts
cause (1) from a live-discovery problem into an offline-iteration problem.

## 2. Architecture

Four tiers. Layers 1–3 are the user's; the **spine** is shared infrastructure
that all three import, and is the reason scenarios stay independently
attributable.

```
tests/harness/            <- the spine (new)
tests/acceptance/l1/      <- Layer 1: deterministic, zero provider calls
tests/acceptance/proc/    <- Layer 1p: real processes, still zero GitHub
tests/conformance/        <- Layer 2: real model vs frozen fixtures
acceptance/live/          <- Layer 3: real GitHub, real models
fixtures/                 <- frozen evidence corpus (git-LFS or committed)
```

### 2.1 The spine

| Module | Responsibility |
| --- | --- |
| `harness/world.py` | `World` builds an isolated universe per scenario: origin repo, local checkout, `state.db`, `checkpoints.sqlite`, `memory.sqlite`, workspace root, lock root, wired `WorkflowEngine`. Exposes `tick(thread_id)` and `drive(thread_id, until=…, max_ticks=…)`. |
| `harness/github_fake.py` | One `FakeGitHub` implementing the `GitHubClient` Protocol. Replaces all three duplicates. Records a full call ledger; supports injected HTTP faults, pagination, ETags, and divergent-branch simulation. |
| `harness/models.py` | `ScriptedModel` (deterministic), `ReplayModel` (cassette playback), `RealModel` (passthrough model string), `FaultyModel` (wraps any, fails at chosen call indices). |
| `harness/probes.py` | **New capability, not just a test double.** A real operator-registered MCP server exposing `acceptance_retryable_probe`, `acceptance_nonretryable_probe`, `acceptance_warning_probe`, `acceptance_fatal_probe`, `acceptance_timeout_probe`, `acceptance_identity_echo`, each with a durable call ledger. Registered through `RepoCapabilityRegistry` exactly like any other MCP server, so S4–S9 exercise the real capability path at every layer. |
| `harness/invariants.py` | The named assertion library — see §3. |
| `harness/scenario.py` | `@scenario("S7", layer=…, invariants=[…])`; produces one attributable PASS/FAIL per scenario plus `campaign-status.json`. |
| `harness/serve.py` | Spawn/kill/restart a real `sweforge-serve` subprocess; wait-for-ready; SIGKILL at a fault-defined instant. |
| `harness/capture.py` | Record and load frozen fixtures (§4). |

### 2.2 Two seams that must be added to `src/`

These are production changes, small and safety-gated, and they are what make
the campaign cheap. Without them the harness must monkeypatch module internals,
which is exactly the pattern that rots.

**(a) `sweforge.faults` — a deterministic fault registry.**
Process-local, inert unless `SWEFORGE_FAULTS` is set (so it cannot fire in
production). Named injection points with deterministic predicates:

```
review.model_call        fail_on_call=[1]      -> S26
review.finalize          raise=ReviewFinalizationError
execution.attempt        hang_until=<fifo>     -> S16 (precise SIGKILL instant)
orphan.recover           fail_always           -> S17
publish.push             fail_on_call=[1]
curator.repo_memory      raise                 -> S24
curator.resolution       raise                 -> S25
probe.timeout            sleep=<n>             -> S8
```

Required because S14–S17 and S24–S26 need faults *inside a real dispatcher
process*, where `monkeypatch` cannot reach.

**(b) `SWEFORGE_EVENT_LOG` — a structured JSONL transition log.**
One line per durable transition: `{ts, thread_id, cycle_id, phase_from,
phase_to, execution_id, attempt_kind, permit_id, publication_id, reason}`.
This is the cheapest possible route to obvious failure attribution, because
**the same assertions then work identically offline and live.** Layer 3 stops
depending on scraping GitHub UI state.

Two smaller additions: `sweforge-serve --ready-file` (removes readiness
races in process-layer scenarios) and a `ReviewerModelFactory` injection point
so Layer 1 stops patching `init_chat_model` / `create_agent` /
`build_reviewer` / `_build_finalizer` separately.

## 3. Invariants are the unit of reuse — not scenarios

Scenarios must not be collapsed, but their *assertions* should be. Each
scenario declares the named invariants it requires. Roughly forty predicates
over the state DB, the FakeGitHub ledger, the event log, and git:

```
INV-PLAN-SINGLE          exactly one canonical plan per (thread, cycle)
INV-PERMIT-BOUND         permit binds exact (plan_id, version, thread, cycle)
INV-PERMIT-NONE          no ExecutionPermit exists
INV-EXEC-ONCE            exactly one INITIAL attempt per cycle
INV-EXEC-NO-HOT-RETRY    no second attempt after terminal EXECUTION_FAILED
INV-PUB-ONCE             one commit, one non-force push, one PR, one comment
INV-PUB-MAPPING          PR <-> IssueThread mapping is exact and unique
INV-NO-FALSE-MEMORY      no repo-memory or resolution rows for unfinalized work
INV-PROVENANCE           root_event_key + root_input_id preserved end to end
INV-WORKTREE-CONFINED    no filesystem effect outside the issue worktree
INV-NO-CONTAMINATION     no durable rows outside this scenario's thread ids
...
```

Two consequences worth stating explicitly:

- **S1's invariant set is the universal postlude.** Every other scenario runs an
  adapted subset of it in its terminal state. Cross-scenario contamination and
  accidental publication are then detected everywhere, for free.
- **A scenario fails with the name of the invariant it broke**, not with a
  traceback in a 4,000-line test file. That is the attribution requirement.

## 4. Frozen fixtures: the mechanism that stops burning issues

A fixture is a directory:

```
fixtures/review/RF-003-multi-requirement-accept/
  manifest.json      provenance: repo, issue, cycle, capture date, sweforge sha
  evidence.json      the exact `evidence` dict passed to review_execution
  worktree.tar.zst   worktree at review time (the reviewer reads real files)
  expected.json      verdict class, per-requirement status, repairability
  observed/          per-run conformance results, appended over time
```

`worktree.tar.zst` is mandatory, not optional: `read_repo_file` resolves
against `context.worktree`, so evidence alone does not reproduce a review.

**Capture is automatic and unconditional.** A hook in `_run_review_locked`
writes a fixture on *every* live review — pass or fail — under
`SWEFORGE_CAPTURE_FIXTURES=<dir>`. Passing reviews are as valuable as failing
ones: they are the regression corpus that proves a guard fix did not tighten
something that used to work.

Target corpus before Layer 3 opens, per component, deliberately including
adversarial classes rather than only whatever live runs happened to produce:

| Component | Min fixtures | Required classes |
| --- | --- | --- |
| Reviewer inspection | 12 | multi-requirement, no-change diff, unavailable evidence, oversized diff, single-file, test-only change |
| Reviewer specialists | 10 | primary/context cluster split, cross-file, provenance-ambiguous |
| Reviewer finalize | 10 | ACCEPT, repairable REJECT, non-repairable REJECT |
| Planner | 8 | vague issue, multi-requirement, historical-case-influenced |
| Clarification classifier | 12 | each answer type ×2, mixed answer, unrelated input, scope-changing |
| Repo-memory curator | 6 | proposal-only, diff-only, nothing-found |
| Resolution curator | 6 | ordinary, no-change lifecycle |

Every fixture derived from a past live failure should be back-filled now, from
notes and logs, before any new live run.

## 5. Layer assignment for S1–S26

`L1` deterministic · `L1p` real process, no GitHub · `L2` conformance ·
`L3` live GitHub. **Every scenario has an L1 or L1p representation** — Layer 3
is never the only proof of an invariant.

| # | Scenario | L1 | L1p | L2 | L3 | Live rationale |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | Happy path + publication | ● | | | **●** | Baseline integration proof; required |
| S2 | Clarification gauntlet | ● | | ● | ● | One answer type live; routing proven offline |
| S3 | Scope change invalidates auth | ● | | ● | ○ | Classifier is the only model dependency |
| S4 | Retryable probe | ● | | | ● | Proves real MCP transport retry, once |
| S5 | Non-retryable probe | ● | | | | Pure policy |
| S6 | Warning probe | ● | | | | Pure classification |
| S7 | Fatal execution failure | ● | | | | Pure state machine |
| S8 | Timeout | ● | | | ● | Real sandbox timeout differs from fake |
| S9 | Identity spoof resistance | ● | | ● | ○ | Spoof text is deterministic; model may comply-or-not |
| S10 | Same-repo concurrency | ● | ● | | ● | Real git contention |
| S11 | Cross-repo concurrency | ● | ● | | ● | Proves no global lock |
| S12 | Backlog drain | | ● | | ○ | Scheduler fairness is process-local |
| S13 | Thread lock contention | ● | | | | Pure lock semantics |
| S14 | Singleton server | | ● | | | No GitHub involved |
| S15 | Restart awaiting approval | | ● | | ● | Durability across real restart |
| S16 | SIGKILL during INITIAL | | ● | | ● | Orphan recovery is the highest-risk path |
| S17 | Recovery exhaustion | | ● | | | Needs many forced failures; wasteful live |
| S18 | Unsolicited follow-up | ● | | | ● | Real event ordering |
| S19 | No-change execution | ● | | ● | ● | Reviewer must ACCEPT a no-op — distinct fixture class |
| S20 | Unmapped PR mention | ● | | | ○ | Routing logic |
| S21 | Sandbox escape | ● | | | | Fuzz corpus, offline |
| S22 | Memory/skills write denial | ● | | | | Permission model |
| S23 | Execution without approval | ● | | | | Table of ~20 near-miss approvals |
| S24 | Repo-memory curator failure | ● | | ● | | Fault-injected |
| S25 | Resolution curator failure | ● | | ● | | Fault-injected |
| S26 | Transient review failure | ● | | ● | | Fault-injected; conformance is the real risk |

● required · ○ optional, run if cheap

Live budget: **11 required live scenarios**, not 26.

Two scenarios deserve corpus treatment rather than a single case, because a
single example proves almost nothing about them:

- **S21** — a corpus of escape patterns: `../` traversal, absolute paths,
  symlink-out, `/proc` and `/dev`, `.git/` internals, nested physical-path
  tricks, worktree-relative `..` after `cd`, long-path and unicode
  normalization variants.
- **S23** — a table of near-miss approvals: `@agent approve please`,
  `@agent  approve`, `Approve`, `@agent approve` on a superseded version,
  approval by a different user, approval quoted inside another comment,
  approval before the plan was posted, approval after a revision.

## 6. Isolation, cleanup, contamination

- **L1/L1p**: each scenario gets its own tmp root and its own three SQLite
  databases. Nothing is shared. Parallel-safe.
- **L3**: a dedicated GitHub repository per scenario class (not per run).
  Every artifact carries a `sweforge-acceptance` label and a
  `<!-- sweforge:acceptance:<scenario>:<run-id> -->` marker.
- **Cleanup** runs as a teardown *and* as a standalone reaper
  (`acceptance-reap`) so a crashed run does not poison the next: close issue,
  close PR, delete remote branch, remove worktree, remove local branch.
- **Contamination detector** after every scenario at every layer: snapshot
  `state.db` row counts per table and assert no durable row exists outside the
  scenario's own thread ids. This is what makes "a failed scenario must not
  contaminate later scenarios" a checked property rather than an intention.

### PRIMARY protection is enforced, not observed

Layer 3 preflight refuses to run if the configured repository is not in the
`sweforge-acceptance-*` allowlist, and refuses outright on any repository named
in a `PRIMARY_REPOS` constant. The stress campaign cannot touch PRIMARY by
construction; the separate PRIMARY acceptance set runs only after the campaign
exit condition is met.

## 7. Retry policy inside the harness

Distinguish two things the campaign keeps conflating:

- **SWEForge's own retry behaviour is under test** and is never retried by the
  harness. If S4 expects exactly two probe calls, the harness asserts two.
- **Harness-level flake** (GitHub 5xx, network) is retried at most twice at
  Layer 3 only, and every retry is recorded in the scenario report. A scenario
  that needed a retry is reported PASS-WITH-RETRY, which does not satisfy the
  exit condition on its own.

Conformance runs are never retried — the failure rate *is* the measurement.

## 8. Reliability threshold for model-dependent components

Separate two failure classes on every conformance run; they have different owners:

- **Class A — guard rejected structurally valid output.** A SWEForge defect.
  Must be fixed. Any Class A occurrence blocks the component regardless of rate.
- **Class B — model produced a substantively wrong result.** A prompt/model
  quality issue. Counted against the rate threshold.

A component passes conformance when, over 20 consecutive runs per fixture
across the full corpus:

- zero Class A failures,
- ≥ 95% Class B success **on first attempt** (measured before, not after,
  `review_execution`'s built-in `range(2)` correction retry — post-correction
  rate hides the real signal),
- no previously unseen guard family fires.

Output is a per-guard rejection histogram, which is the artifact that tells you
*which* guard to loosen or which prompt to fix, rather than "review failed".

## 9. Phases and sequencing

**Phase 0 — spine (no scenario passes yet).**
Harness package, `conftest.py`, consolidate the three `FakeGitHub` classes,
`sweforge.faults`, event log, `--ready-file`, `ReviewerModelFactory`, probe MCP
server, fixture capture hook. Exit: `pytest` green, existing 13.8k lines of
tests still pass, capture hook writes a fixture on a synthetic review.

**Phase 1 — Layer 1 and 1p for all 26.**
Exit: 26 attributable results, all PASS, reproducible across three consecutive
runs, total runtime under ~5 minutes, wired into CI.

**Phase 2 — fixture harvest.**
Back-fill every known past live failure as a fixture. Reach the §4 corpus
minimums. Exit: corpus complete, every historical failure reproduces offline.

**Phase 3 — conformance to threshold.**
Iterate offline on guards and prompts until §8 is met for all seven
model-dependent components. This is where the "one bug per issue" cost is
actually eliminated. Exit: histogram clean, thresholds met.

**Phase 4 — Layer 3, gated and ordered.**
A scenario may run live only when its L1 passes *and* every model-dependent
component it touches has met threshold. Order: S1 → S19 → S2 → S18 → S15 → S16
→ S4 → S8 → S10 → S11 → S20. Each live run deposits new fixtures; a Layer 3
failure is converted into a fixture and returned to Phase 3 rather than
retried live.

**Phase 5 — PRIMARY acceptance set.** Separate, after the exit condition.

## 10. Exit condition

Machine-checked from `campaign-status.json`, not asserted by hand:

1. All 26 scenarios PASS independently, each attributed to its own scenario id.
2. No scenario SKIPPED, and no scenario's result substituted by another's.
3. Every deterministic scenario reproducible: three consecutive clean runs.
4. Every bounded failure/recovery path observed at least once with its bound
   hit exactly (S8 timeout, S17 exhaustion, S26 backoff, execution retry ×3).
5. All seven model-dependent components meet the §8 threshold.
6. Zero contamination-detector violations across the full campaign.
7. No PASS-WITH-RETRY in the final run.
8. PRIMARY untouched — verified from the allowlist audit log.

Then, and only then, the PRIMARY acceptance set runs.

## 11. Immediate next steps

1. Build the spine (Phase 0). It is the prerequisite for everything else.
2. Add the probe MCP server — S4–S9 currently have no substrate at all.
3. Turn on fixture capture **before** the next live run, so that run stops
   being wasted.
# Declarative workflow architecture (2026-08)

The acceptance target now treats an IssueThread cycle as a trusted declarative
DAG of generic task runs. The application persists exactly one active task and
owns every transition through planning, exact human approval, execution,
validation, repair/replan, completion, and one cumulative publication. The root
Deep Agent is the sole workflow owner. Its investigator subagent is a bounded
read-only worker and cannot receive lifecycle gateways or broader phase
authority. Deterministic coverage lives in `test_workflow_spec.py`,
`test_workflow_runtime.py`, `test_workflow_middleware.py`, and
`test_workflow_agent_runtime.py`.
