# Codex Work Orders — Acceptance Framework

Send **one work order per session**, in order. Prefix every session with the
standing brief. Do not paste this whole file.

---

## Standing brief (prefix every Codex session)

```
Read docs/acceptance/ROADMAP.md before doing anything. It is the design of
record; this work order implements one milestone from it.

HARD RULES — violating any of these fails the task:

1. Do NOT create, comment on, close, label, or otherwise mutate any GitHub
   issue or pull request. Do not run any command that talks to api.github.com.
2. Do NOT run sweforge-serve, sweforge-github-poll, sweforge-github-execute,
   or sweforge-github-publish against a real repository.
3. Do NOT run acceptance scenarios S1 or S2, live or otherwise.
4. Do NOT modify issue #16 or its durable state.
5. ~/.sweforge/acceptance-archive/** is READ-ONLY source-of-truth evidence.
   Copy out of it; never write into it, never delete from it.
6. Do NOT decide whether review-infrastructure failure should retry forever
   or terminate after N attempts. That is an open design decision (ROADMAP
   §J note). If your work order depends on it, stop and report.
7. Do NOT add a runtime dependency to pyproject.toml without asking first.
   Dev/test-only dependencies are fine if justified in the summary.

BASELINE — must hold at the end of every work order:
  uv run pytest        -> 399 passed (or more; never fewer, never a failure)
  uv run ruff check .  -> clean
  uv run ruff format --check .  -> clean

SCOPE: implement exactly this work order. Do not start the next milestone.
If you find a defect outside scope, report it in the summary; do not fix it.

DELIVERABLE: one branch, one commit series, and a summary that states what
you implemented, what you verified, what you could not verify, and any
decision you had to make that the work order did not specify.
```

---

## WO-1 · Guard codes (milestone M0)

**Critical path. Send this first.**

```
Implement ROADMAP milestone M0: stable guard codes in the reviewer.

src/sweforge/reviewer.py contains 41 `problems.append("free text")` sites
across six guard families. Free-text problems make the rejection histogram
and the Class A / Class B split impossible to automate.

TASK

1. Add src/sweforge/guard_codes.py:
   - `GuardCode(StrEnum)` with one stable member per distinct rejection
     reason. Group by family with a code prefix:
       RC-  requirement contract        (~4 sites, near line 639)
       IA-  inspection authority        (~14 sites, 2425-2511)
       II-  inspection artifact         (~2 sites, 2747-2753)
       SP-  specialist / semantic       (~11 sites, 2312-2388)
       FA-  finalize artifact           (~10 sites, 2820-2886)
       AC-  accept coverage / repairability guards
   - `GuardProblem` frozen dataclass: `code: GuardCode`, `detail: str`.
     `detail` carries the variable part (requirement id, path, cluster id);
     the code carries the invariant that was violated.

2. Convert all 41 sites to append `GuardProblem` instead of a string.
   Preserve the exact human-readable message as `detail` so nothing gets
   less debuggable. Where a message today interpolates an id, the id goes in
   `detail` and the code stays constant across ids.

3. Update every consumer: `_inspection_failure_diagnostic`,
   `ReviewFinalizationError` diagnostics, and any place a problem list is
   formatted or truncated. `ReviewFinalizationError.diagnostic` must now
   carry a machine-readable `guard_codes: [str]` alongside the existing
   human text.

4. Add `acceptance/guard_classification.json` mapping every GuardCode to
   "A" (semantically valid model output rejected by a SWEForge guard —
   product bug) or "B" (model failed to emit sufficient/correct facts).
   Include a one-line rationale per code. Where you genuinely cannot tell,
   use "UNKNOWN" and list those codes prominently in your summary — do not
   guess.

5. Add a test asserting every GuardCode member appears in the
   classification file and vice versa, so a new guard cannot be added
   without classifying it.

CONSTRAINTS
- This is a mechanical refactor. Do NOT change any guard's logic, threshold,
  or acceptance behaviour. No guard may start or stop firing.
- Prove that: the existing reviewer tests must pass unchanged.

DONE WHEN
- All 41 sites emit GuardProblem; `grep -c 'problems.append' ` still 41.
- Every code classified A, B, or explicitly UNKNOWN in your summary.
- 399 tests pass, ruff clean.
```

---

## WO-2 · Fixture schema, capture hook, and offline backfill (milestone M1)

**Critical path. This is the work order that ends the burn-an-issue loop.**

```
Implement ROADMAP milestone M1: the review fixture format, live capture, and
an offline backfill that freezes issue #16 without touching GitHub.

Read ROADMAP §E for the schema. Read WorkflowEngine._run_review_locked in
src/sweforge/workflow.py (around line 2186) — it builds the complete evidence
dict immediately before calling self.reviewer(...). That call site is the
capture point.

TASK

1. src/sweforge/review_fixture.py — versioned writer/reader for schema v1
   exactly as specified in ROADMAP §E.2. All ten files. Notes:
   - evidence.json is the exact dict passed to review_execution, verbatim.
   - contract.json is review_requirement_contract(evidence) at capture time.
     It is a pure function of evidence; storing it lets replay detect
     contract-derivation drift.
   - worktree.tar.zst is MANDATORY — read_repo_file resolves against
     context.worktree, so evidence alone cannot reproduce a review.
   - Redaction per ROADMAP §E.3: reuse execution_evidence.sanitize(), exclude
     .git/config, .git/credentials, .env*, **/*.pem, **/id_* from the
     snapshot, and re-scan every written file afterward. A secret hit
     quarantines the fixture rather than publishing it.

2. Capture hook in _run_review_locked, enabled by SWEFORGE_REVIEW_FIXTURE_DIR.
   Writes on EVERY review — success and failure alike. Passing reviews are the
   regression corpus.
   CRITICAL: capture must never fail a review. Wrap it so any exception is
   swallowed, counted, and logged, and the review proceeds. Add a test that
   proves a raising capture does not fail the review.

3. `sweforge-review-freeze` CLI — reconstructs a fixture from durable state
   with NO network and NO model call. Given a state.db, a thread_id, a
   cycle_id and a workspace path, it rebuilds the evidence dict the same way
   _run_review_locked does (plan, execution, attempt, execution_tool_evidence,
   source event, changed_files, diff, dirty) and writes a v1 fixture.
   Factor the evidence construction out of _run_review_locked into one shared
   function so capture and backfill can never drift apart.

4. Use it to freeze the existing evidence. Source is READ-ONLY:
   ~/.sweforge/acceptance-archive/20260824-prefixture/

   a) Issue #16 — the three inspector failures.
      state.db:  s1-16/state.db
      thread:    github:1341401850:issue:16   cycle 1
      worktree:  s1-16/workspaces/1341401850/issue-16
      The thread is REVIEW_EXECUTION with a SUCCEEDED INITIAL attempt and
      ZERO rows in execution_reviews — every review raised
      ReviewFinalizationError before save_execution_review. That is exactly
      the failure being frozen. Name it RF-016-inspector-authority.

   b) The regression corpus — 7 reviews that DID produce structured verdicts,
      in stress-state.db, for issues 7, 10, 11, 12, 13, 14 (BLOCKED and
      NEEDS_FIXES both present). Worktrees are under stress-workspaces/.
      These are the fixtures that prove a #16 fix does not break a review
      shape that already worked.

   Write fixtures to acceptance/fixtures/review/v1/.

5. `sweforge-review-replay` — unpacks a fixture's worktree to a temp dir,
   rebuilds ReviewerContext against it, re-derives the contract and compares
   it to contract.json, then calls the real review_execution.
   Support --runs N and --report PATH. Do NOT invoke a real model in tests.

CONSTRAINTS
- Do not call a model provider anywhere in this work order.
- Do not modify anything under acceptance-archive/. Copy out of it.
- Committing fixtures: if total size is over ~20MB, report it and ask before
  committing rather than adding git-lfs on your own.

DONE WHEN
- 8 fixtures exist (#16 plus 7 corpus reviews), each loadable by the reader.
- Replay of a corpus fixture with a stubbed model reproduces its stored
  outcome; replay of RF-016 with a stubbed model reproduces its failure.
- Contract re-derivation matches contract.json for all 8.
- The secret scan passes on all 8.
- 399 tests pass, ruff clean.
```

---

## WO-3 · Conformance runner (milestone M3)

```
Implement ROADMAP milestone M3: the real-model conformance runner.

Read ROADMAP §F. Depends on WO-1 (guard codes) and WO-2 (fixtures).

TASK

1. `attempt_observer` — an optional callback parameter on review_execution
   that fires at each internal correction attempt boundary (the
   `for inspection_attempt in range(2)` loop, and the equivalent in any other
   stage that retries). Without it, first-pass conformance cannot be measured
   — and first-pass is the metric that matters.

2. acceptance/runner/conformance.py — runs a fixture N times against a real
   configured model and classifies each run:
     OK       accepted artifact
     Class A  valid output rejected by a guard  (from guard_classification.json)
     Class B  model failed to emit correct facts
     UNCLASSIFIED  a GuardCode with no classification — BLOCKS the run

3. Report both metrics per ROADMAP §F.2:
     first-pass  >= 95%   measured BEFORE the correction retry
     eventual    = 100%   for fixtures with a known-valid expected outcome
     Class A     = 0%
     UNCLASSIFIED = 0
   Emit the per-guard rejection histogram — that is the actionable output.

4. CI-safe tests: the runner's own logic is tested with a scripted model.
   No provider call in `uv run pytest`.

CONSTRAINTS
- The runner must never retry a failed run. The failure rate IS the
  measurement.
- Do not run it against a real provider as part of this work order. Wiring
  and correctness only; I will run the real conformance pass.

DONE WHEN
- `uv run sweforge-conformance --fixture … --runs N --model … --report …`
  works end to end against a scripted model.
- Thresholds and classification are unit-tested.
- 399 tests pass, ruff clean.
```

---

## WO-4 · Diagnose #16 — report only, do not fix

```
Using the RF-016-inspector-authority fixture from WO-2 and the conformance
runner from WO-3, diagnose the three inspector failures on issue #16.

TASK
- Replay RF-016 against the configured review model enough times to observe
  each distinct failure.
- For each of the three failures report: the exact GuardCode, the guard
  function and line, what the model actually emitted, what the guard
  required, and whether it is Class A (guard wrong) or Class B (model
  output insufficient).

DO NOT FIX ANYTHING. Report only. I want to see the diagnosis before any
guard or prompt changes, because a wrong fix here silently breaks review
shapes that already work.

DONE WHEN
- A written diagnosis of all three, each attributed to a specific GuardCode
  and classified, with the evidence that supports the classification.
```

---

## WO-5 · Event log (milestone M2) — parallel track

```
Implement ROADMAP milestone M2: the structured event log.

Read ROADMAP §I.

TASK
1. src/sweforge/events.py — `emit(kind, **fields)` writing JSONL to
   SWEFORGE_EVENT_LOG. Envelope per §I.1, event kinds per §I.2.
2. Redaction is STRUCTURAL, not filtered: `data` accepts a per-kind allowlist
   of scalar fields only. Free-form text, model messages, reasoning, plan
   bodies, issue bodies, diffs and tool output must be unrepresentable. A
   disallowed key raises in tests.
3. Emission points across workflow.py, server.py, github_publisher.py — one
   line per durable transition. Flush before the transition's transaction
   commits, so a crash cannot lose the event that explains it.
4. Inert with no cost when SWEFORGE_EVENT_LOG is unset.

DONE WHEN
- A full offline plan→approve→execute→review→publish sequence produces a
  well-formed event stream with no gaps.
- A test proves a disallowed field raises.
- 399 tests pass, ruff clean.
```

---

## WO-6 · Acceptance mode and fault registry (milestone M1b) — parallel track

```
Implement ROADMAP milestone M1b: the two-key fault seam.

Read ROADMAP §G. This code can inject failures into a production process, so
the gating matters more than the features.

TASK
1. src/sweforge/acceptance_mode.py — single authority: acceptance_enabled(),
   require_acceptance(feature).
2. src/sweforge/faults.py — registry with hit(point, **ctx), spec loading,
   ledger, drain assertions. Faults are named, deterministic (`on_calls` by
   index, NEVER probabilistic), bounded (`max_fires`, default 1), observable
   (each firing emits FAULT_FIRED).
3. Gating, all enforced:
   - SWEFORGE_FAULT_SPEC without SWEFORGE_ACCEPTANCE_MODE=1 is a STARTUP
     FAILURE. Not a warning. Not a silent ignore.
   - Acceptance mode without a spec is legal and injects nothing.
   - hit() returns immediately when either key is absent.
   - acceptance_mode is stamped into every event-log envelope.
4. Injection points per ROADMAP §G.4. Include
   `execution.after_attempt_persist` with `action: "block_on_fifo"` so a
   scenario can SIGKILL at a deterministic instant rather than racing a sleep.
5. Teardown asserts the ledger DRAINED — every declared fault fired exactly
   its expected count. An undrained fault must fail the scenario, so a fault
   that quietly never fired cannot produce a false PASS.

CONSTRAINTS
- Do not wire any fault point into a code path in a way that changes
  behaviour when the registry is inert. Prove inertness with a test.

DONE WHEN
- Startup rejection tested. Inertness tested. Drain assertion tested.
- 399 tests pass, ruff clean.
```

---

## WO-7 · Adopt the existing probe MCP server (milestone M1c) — parallel track

```
Implement ROADMAP milestone M1c. NOTE: the probe server already exists and
works — this is adoption and hardening, not a rewrite.

EXISTING (read-only, copy out of it):
  ~/.sweforge/acceptance-archive/20260824-prefixture/common/mcp/acceptance_mcp.py
  ~/.sweforge/acceptance-archive/20260824-prefixture/common/capabilities-stress.json

It already exposes retryable_probe, nonretryable_probe, warning_probe,
fatal_probe, timeout_probe, identity_echo, plus three repo-specific matrix
tools, registered under server id `acceptance` across three stress repos.

TASK
1. Move it into acceptance/probes/server.py under version control.
2. Replace its JSON tool-state file with a SQLite ledger at
   SWEFORGE_PROBE_LEDGER. State must survive process boundaries, because
   S15-S17 kill and restart the server mid-run. JSON state is not durable
   enough for that.
3. Ledger row per call: ts, tool, operation_id, repo_id, repo_full_name,
   args_received, outcome. Expose a ProbeLedger reader for assertions.
4. Keep behaviour identical otherwise: retryable_probe fails retryably on
   call 1 per operation_id and succeeds on call 2, etc.
5. identity_echo must take NO arguments and return the repo_id /
   repo_full_name it actually received — repo_scope_interceptor injects those
   from the trusted RepoAgentContext, so this is the S9 assertion surface.
6. Check the sync/async call path: tests/test_execution.py already records
   that an async-only StructuredTool raises "StructuredTool does not support
   sync invocation" when reached through delegation. Make sure the probes are
   invocable on the executor's real call path and add a test pinning it.

DONE WHEN
- Probe server runs from the repo, ledger is SQLite, counts survive a restart.
- 399 tests pass, ruff clean.
```

---

## WO-8 · Harness and invariant registry (milestone M4) — parallel track

```
Implement ROADMAP milestone M4: the test harness spine and named invariants.

Read ROADMAP §A.2 and §D.

TASK
1. tests/conftest.py — the repo has none today.
2. tests/harness/ per §A.2: world.py, github_fake.py, models.py,
   observation.py, invariants.py, scenario.py, serve.py.
3. github_fake.py must CONSOLIDATE the three existing duplicate FakeGitHub
   classes in test_clarification_resume_identity.py, test_github_poller.py
   and test_publication_identity.py into one implementation over the
   GitHubClient Protocol, with a call ledger. Migrate those three test files
   to it. They must keep passing.
4. The Observation bundle per §D.1. GitHubFacts needs two implementations
   behind one interface — ledger-backed offline, REST-readback live — so a
   predicate never knows which layer it is running in.
5. All 30 invariants from §D.2, each returning InvariantResult with a
   `detail` string stating what was actually observed.
6. The @scenario decorator and per-invariant reporting per §D.3. A failing
   scenario must report which invariant broke, never a bare traceback.

CONSTRAINTS
- Do not weaken or delete any existing test to make consolidation easier.
  Test count goes up, never down.

DONE WHEN
- Three duplicate fakes are gone, one remains, all migrated tests pass.
- All 30 invariants implemented with tests for both outcomes.
- pytest total >= 399, ruff clean.
```

---

## WO-9 · The 26 deterministic scenarios (milestone M5)

```
Implement ROADMAP milestone M5: tests/acceptance/l1/test_s01.py … test_s26.py.

Read ROADMAP §J for each scenario's invariant set and pass conditions.
Depends on WO-5, WO-6, WO-7, WO-8.

TASK
- One file per scenario, one attributable result each.
- Each declares its invariant set via @scenario plus any scenario-specific
  predicate.
- S21 and S23 are CORPORA, not single cases — a payload table each:
    S21  ../ traversal, absolute paths, symlink-out, /proc and /dev, .git/
         internals, nested physical-path tricks, worktree-relative .. after
         cd, long-path and unicode normalization variants
    S23  "@agent approve please", doubled whitespace, bare "Approve",
         approval of a superseded version, approval by a different user,
         approval quoted inside another comment, approval posted before the
         plan, approval after a revision
- Emit campaign-status.json.

STOP AND REPORT on S17 and S26. Their pass conditions depend on an open
design decision (ROADMAP §J note): ReviewFinalizationError is caught nowhere
in src/, so it surfaces as a dispatcher_failures row whose delay caps at
3600s but whose count never terminates. Write the parts of S17 and S26 that
do not depend on that, and report what is blocked. Do not decide it.

DONE WHEN
- 26 scenarios, all PASS, three consecutive clean runs, under ~5 minutes.
- No scenario skipped or substituted.
```

---

## Order

```
Critical path   WO-1 → WO-2 → WO-3 → WO-4 → [my review + fix decision] → Gate K7
Parallel track  WO-5, WO-6, WO-7 → WO-8 → WO-9
```

WO-1 and WO-2 unblock the #16 diagnosis. Everything else can proceed
alongside. Gate K (ROADMAP §K) must be fully satisfied before any live S1.
