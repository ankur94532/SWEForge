# SWEForge Acceptance Campaign — Autonomous Execution Brief

You are executing this campaign end to end. This document is your standing
contract. Re-read it at the start of every session.

Design of record: `docs/acceptance/ROADMAP.md`. Read it before your first
action. It defines the architecture; this document defines how you execute it,
how you prove your work, and where you must stop.

---

## 1. Mission

SWEForge has 26 acceptance scenarios (S1–S26), each isolating one failure class
in its durable GitHub-native workflow. The campaign has been stalled in a loop:

```
fix → run a live GitHub issue → the reviewer rejects the model's output for a
new reason → fix that → burn another issue → discover another reason
```

Unbounded, and each turn costs a real GitHub issue plus a full end-to-end run.

The cause is structural. `review_execution(*, context, model, evidence: dict)`
takes serializable input, but that input was discarded on failure — so
reproducing a reviewer defect required another live run. Meanwhile the reviewer
fails closed behind 41 guard sites, any of which can reject structurally
plausible model output.

**Your objective: make every reviewer defect reproducible offline, drive the
reviewer to a measured reliability threshold against frozen real evidence, and
then execute all 26 scenarios such that each live run is a confirmation rather
than a discovery.**

Final state: 26 independently attributable PASSes, no scenario skipped or
substituted, PRIMARY untouched until the campaign completes.

---

## 2. Current state — verified, not reported

Branch `codex/m0-guard-codes`, HEAD `50b259f`. Suite: **413 passing**.

| Milestone | Status | Evidence |
| --- | --- | --- |
| **M0** guard codes | ✅ done, verified | 41 `GuardCode` members, 41 guard sites, `GuardProblem` frozen dataclass with sound hash. Classification: 25 `B`, 16 `UNKNOWN`. |
| **M1** fixtures + capture + backfill | ✅ done, verified | Schema v1 writer/reader, non-fatal capture hook, `sweforge-review-freeze`, `sweforge-review-replay --offline`, 8 fixtures (~680 KB), 115 lines of new tests. |
| **M1b** acceptance mode + faults | ⬜ not started | — |
| **M1c** probe MCP server | ⬜ not started | — |
| **M2** event log | ⬜ not started | — |
| **M3** conformance runner | ⬜ not started | — |
| **M4** invariants + harness | ⬜ not started | — |
| **M5** 26 deterministic scenarios | ⬜ not started | — |
| **M6** Gate K | ⬜ not started | — |
| **M7** 26 integration runs | ⬜ not started | — |

### The 16 UNKNOWN guard codes

`IA-*` (the entire inspection-authority family), plus `FA-INSPECTION-COVERAGE`
and `FA-CHALLENGE-COVERAGE`. `UNKNOWN` means *not yet observed against real
model output*. It is resolved by evidence in M3/M4 work, **never by reasoning**.
Do not reclassify one because it seems obvious.

### The frozen corpus

`acceptance/fixtures/review/v1/` — 8 fixtures:

- `RF-016-inspector-authority` — the stalled issue. Thread
  `github:1341401850:issue:16`, cycle 1, `REVIEW_EXECUTION`, a **SUCCEEDED**
  INITIAL attempt, and **zero** rows in `execution_reviews` because every
  review raised `ReviewFinalizationError` before persistence. That absence is
  the failure.
- 7 corpus fixtures (issues 7, 10, 11×1, 12, 13, 14×2) with real stored
  verdicts, both `BLOCKED` and `NEEDS_FIXES`. These prove a later #16 fix does
  not break a review shape that already worked.

### Read-only evidence archive

`~/.sweforge/acceptance-archive/20260824-prefixture/` is the only copy of the
pre-fixture state. **Copy out of it. Never write into it, never delete from
it.** Contains `s1-16/` (the #16 state.db and worktree), `stress-state.db`,
`stress-workspaces/`, and `common/` (which holds a working probe MCP server and
capability config — see M1c).

### Known open defect, logged not fixed

`record_dispatcher_failure` truncates `last_error` to 1000 characters, which
destroys the embedded `diagnostic={...}` JSON payload — the reason #16's stored
diagnostic is unparseable. Do not fix this as a side quest. Log it; it is a
candidate for its own work order after Gate K.

---

## 3. Hard rules

Violating any of these fails the work, regardless of what else was delivered.

1. **No GitHub mutation** until §4 authorises it. No creating, commenting on,
   closing, or labelling issues or PRs. No command that reaches
   `api.github.com`.
2. **Never mutate issue #16** or its durable state, at any point.
3. **`~/.sweforge/acceptance-archive/**` is read-only.**
4. **PRIMARY is untouchable** for the entire stress campaign. Enforcement is
   mechanical (M1b/M4), not a matter of care.
5. **No runtime dependency** added to `pyproject.toml` without asking.
   Dev/test-only dependencies are fine if justified.
6. **One milestone per branch and commit series.** Do not begin the next
   milestone in the same session you finish one.
7. **Never weaken a test to make a change pass.** If a test must change, that
   means behaviour changed — stop and report instead.
8. **Never invent evidence.** If a value cannot be derived from data, record
   the absence and the reason. A fabricated guard code, a guessed
   classification, or a placeholder presented as derived is the single worst
   outcome available to you — it corrupts the evidence base this entire
   campaign is built on. This has already happened once (commit `6e17ea8`
   hardcoded a guard code into the freeze CLI); it was caught and reverted.

---

## 4. The two hard stops

Stop, report, and wait for a human at exactly these two points. Nowhere else.

**STOP 1 — before the first live GitHub run of M7.**
Report Gate K's eight conditions with the command output proving each. Do not
run S1 live until a human replies.

**STOP 2 — before any action touching PRIMARY.**
The stress campaign never touches PRIMARY. The PRIMARY acceptance set is a
separate exercise after the campaign exit condition holds.

Everything between those points is yours to execute without checking in.

---

## 5. The two decisions reserved for a human

Do not decide these. Implement everything around them and report.

**D1 — Review-infrastructure retry bound.**
`ReviewFinalizationError` is caught nowhere in `src/`. It escapes
`WorkflowEngine.advance()` into `SWEForgeServer._worker_entry`, which writes a
`dispatcher_failures` row whose delay caps at 3600s but whose **count never
terminates** — a permanently failing review retries forever at one attempt per
hour. Either (a) that is intended, and `INV-RETRY-BOUNDED` does not apply to
review, or (b) it must terminate after N, requiring a durable review-attempt
counter and a terminal state. **This blocks the PASS conditions for S17 and
S26.** Build everything else in those two scenarios; report what is blocked.

**D2 — The #16 fix classification.**
When M3 conformance reveals why #16's inspector fails, each failure is either
Class A (the guard is wrong — a SWEForge defect) or Class B (the model failed
to emit sufficient facts — a prompt defect). **Report the evidence and your
reasoning; do not apply the fix.** A wrong call here silently breaks review
shapes that currently work, which is precisely what the 7 corpus fixtures exist
to detect.

---

## 6. How you prove work — the anti-self-certification protocol

The most likely way this campaign fails is not bad code. It is a plausible
summary that is not true. This has already happened: a work order was reported
as `401 passed` when it added **zero** tests — technically true, materially
misleading, because 401 was the pre-existing count.

Therefore:

> **A claim without its command output is not a claim.**

Every milestone report must include the literal output of the gate commands
below. Not a description of the output. The output.

### Universal gates — every milestone

```bash
# 1. Suite grew or held, and you state both numbers
uv run pytest -q | tail -1

# 2. Tests were actually added when the milestone required them
git diff --stat <base>..HEAD -- tests/

# 3. Lint and format
uv run ruff check .
uv run ruff format --check .

# 4. No fabricated guard codes anywhere in production code
grep -rnE '"(IA|FA|SP|RC|II)-[A-Z-]+"' src/ | grep -v guard_codes.py

# 5. No live GitHub reach
git log -p <base>..HEAD | grep -nE 'api\.github\.com|create_comment|create_pull_request' || true
```

### Reporting rules

- State the pytest count **before and after**. A milestone that adds
  functionality and not tests is incomplete, not efficient.
- If a done-condition could not be satisfied, **say so explicitly and why**.
  Silently omitting it from your summary is a reporting failure and will be
  treated as one.
- Distinguish *implemented* from *verified*. "I wrote the replay path" and
  "I ran replay on all 8 fixtures and it reproduced their outcomes" are
  different sentences. Use the one that is true.
- When you make a judgment call the work order did not specify, name it.

---

## 7. Execution sequence

Two tracks. The critical path unblocks #16; the parallel track builds the
machinery for the other 25 scenarios. Nothing in the parallel track gates the
critical path.

```
CRITICAL   M3 conformance ──> [D2 report] ──> Gate K ──> STOP 1 ──> M7
PARALLEL   M1b faults ─┐
           M1c probes ─┼──> M4 invariants ──> M5 scenarios ──────────┘
           M2 events ──┘
```

### M3 — Conformance runner  *(critical path, do this first)*

Read ROADMAP §F.

- Add an `attempt_observer` callback to `review_execution`, firing at each
  internal correction-attempt boundary (`for inspection_attempt in range(2)`
  and any equivalent). Without it first-pass conformance is unmeasurable, and
  first-pass is the metric that matters.
- `acceptance/runner/conformance.py`: run a fixture N times against a real
  configured model; classify each run `OK` / Class A / Class B / `UNCLASSIFIED`
  using `acceptance/guard_classification.json`.
- Report **both** thresholds: first-pass ≥ 95% measured *before* the correction
  retry, and bounded-eventual = 100% for fixtures with a known-valid expected
  outcome. `19/20` first-pass with `20/20` eventual passes. `12/20` with
  `20/20` fails — that gap is the reviewer-architecture signal the campaign
  exists to surface.
- Class A rate must be 0%. `UNCLASSIFIED` must be 0 and blocks the run.
- Emit the per-guard rejection histogram. That histogram is the deliverable —
  it names which guard to loosen or which prompt to fix.
- Never retry a failed conformance run. The failure rate *is* the measurement.
- The runner's own logic is tested with a scripted model. No provider call in
  `uv run pytest`.

**Then run it against RF-016 with the real review model** and produce the D2
report: which guard codes fire, how often, what the model actually emitted,
what the guard required, and your Class A/B reasoning per failure. Resolve the
relevant `UNKNOWN` classifications **from this evidence**. Do not apply fixes.

### M1b — Acceptance mode and fault registry

Read ROADMAP §G. Two-key gate: `SWEFORGE_ACCEPTANCE_MODE=1` **and**
`SWEFORGE_FAULT_SPEC=...`. A spec without acceptance mode is a **startup
failure** — not a warning, not a silent ignore. Faults are named, deterministic
(`on_calls` by index, never probabilistic), bounded (`max_fires`), observable
(`FAULT_FIRED` + ledger). Teardown asserts the ledger **drained**, so a fault
that quietly never fired cannot produce a false PASS. `hit()` costs one boolean
check when inert — prove inertness with a test.

Includes `runner/allowlist.py`: PRIMARY protection checked before every
mutating call, proven by a negative test.

### M1c — Probe MCP server  *(adoption, not a build)*

A working server already exists at
`~/.sweforge/acceptance-archive/20260824-prefixture/common/mcp/acceptance_mcp.py`
with all six probes and a capability config across three stress repos. Move it
into `acceptance/probes/server.py` under version control.

The one substantive change: **replace its JSON state file with a SQLite ledger**
at `SWEFORGE_PROBE_LEDGER`. JSON state will not survive the process kills
S15–S17 require.

`identity_echo` takes **no arguments** and returns the `repo_id` /
`repo_full_name` it actually received — `repo_scope_interceptor` injects those
from the trusted `RepoAgentContext`, which is what makes it the S9 assertion
surface. Also pin the sync/async call path: `tests/test_execution.py` already
records that an async-only `StructuredTool` raises under delegation.

### M2 — Event log

Read ROADMAP §I. JSONL to `SWEFORGE_EVENT_LOG`, one line per durable
transition, flushed **before** the transition's transaction commits so a crash
cannot lose the event that explains it. Redaction is **structural**: `data`
accepts a per-kind allowlist of scalars only, so plan bodies, issue bodies,
diffs, model messages and reasoning are *unrepresentable*. A disallowed key
raises in tests.

### M4 — Invariants and harness

Read ROADMAP §A.2 and §D. Build `tests/conftest.py` (the repo has none),
`tests/harness/*`, and all 30 named invariants.

**Consolidate the three duplicate `FakeGitHub` classes** in
`test_clarification_resume_identity.py`, `test_github_poller.py` and
`test_publication_identity.py` into one implementation over the `GitHubClient`
Protocol, and migrate those files. They must keep passing. Test count goes up,
never down.

`GitHubFacts` needs two implementations behind one interface — ledger-backed
offline, REST-readback live — so a predicate never knows which layer it runs
in. That is what lets one invariant serve both.

### M5 — The 26 deterministic scenarios

Read ROADMAP §J. One file per scenario, one attributable result each. A failing
scenario reports **which invariant broke**, never a bare traceback.

Split into five chunks so each shares a harness surface — do not attempt 26 in
one pass:

- **5a** lifecycle: S1, S15, S18, S19, S20
- **5b** probes: S4, S5, S6, S7, S8, S9
- **5c** concurrency and process: S10, S11, S12, S13, S14
- **5d** corpora: S21, S23
- **5e** faults and clarification: S2, S3, S16, S17, S22, S24, S25, S26

S21 and S23 are **corpora, not single cases** — payload tables. S21: `../`
traversal, absolute paths, symlink-out, `/proc` and `/dev`, `.git/` internals,
nested physical-path tricks, worktree-relative `..` after `cd`, long-path and
unicode variants. S23: `@agent approve please`, bare
`Approve`, approval of a superseded version, approval by a different user,
approval quoted inside another comment, approval before the plan was posted,
approval after a revision.

Corrected after S23 was written: doubled whitespace and approval by a
different user are accepted deliberately, not near-misses. See COVERAGE-GAPS.md
F3 and F4.

In **5e**, S17 and S26 are blocked on D1 — build what does not depend on it and
report the rest.

### M6 — Gate K

Eight conditions, each with command output. See §8.

### M7 — The 26 integration runs

**STOP 1 comes first.** After clearance:

- 12 LIVE-GITHUB, 14 LIVE-PROCESS. Every scenario gets exactly one
  integration-level run. See ROADMAP §J for the split.
- LIVE-GITHUB order: S1 → S19 → S2 → S18 → S15 → S16 → S4 → S8 → S9 → S20 →
  S10 → S11. LIVE-PROCESS interleaved.
- Dedicated acceptance repos only. Every artifact carries a
  `sweforge-acceptance` label and a
  `<!-- sweforge:acceptance:<scenario>:<run-id> -->` marker.
- Cleanup runs as teardown **and** as a standalone reaper, so a crashed run
  does not poison the next.
- Contamination detector after every scenario: assert no durable row exists
  outside that scenario's own thread ids.
- **A live failure becomes a fixture and returns to M3. It is never retried
  live.** That rule is the whole point of the campaign.
- Harness-level flake (GitHub 5xx, network) may be retried twice, recorded, and
  reported as PASS-WITH-RETRY — which does not satisfy the exit condition.

---

## 8. Gate K — before any live S1

```
K1  Fixture schema v1 implemented; capture hook live in _run_review_locked;
    capture proven non-fatal by test.                          [M1 ✅ done]

K2  #16 frozen as a v1 fixture without mutating #16 and without
    creating any issue.                                        [M1 ✅ done]

K3  All 41 guard sites emit GuardProblem; every code present in
    guard_classification.json. UNKNOWN is legitimate; a MISSING
    code is not.                                               [M0 ✅ done]

K4  All three #16 inspector failures reproduced offline from the frozen
    fixture, each attributed to a specific GuardCode, and each resolved
    from UNKNOWN to A or B on the evidence of what the model actually
    emitted.

K5  All three diagnosed and fixed, each fix justified as either "guard was
    wrong" (Class A) or "prompt/contract was wrong" (Class B).  [needs D2]

K6  The regression corpus contains at least one PASSING pre-fix fixture and
    the fix does not regress it. This is the condition that stops a #16 fix
    from breaking a review shape that already worked.

K7  Conformance over #16 plus the corpus with the actual configured review
    model: first-pass >= 95%, bounded-eventual = 100%, Class A = 0, across
    20 consecutive runs.

K8  S1's deterministic L1 scenario passes with its full invariant set, and
    PRIMARY allowlist enforcement is proven by a negative test — the harness
    refuses a PRIMARY target.
```

Until K1–K8 all hold with command output: no live S1, no S2, no new GitHub
issue, no mutation of #16.

---

## 9. Definition of done

Machine-checked from `campaign-status.json`, not asserted:

1. All 26 scenarios PASS independently, each attributed to its own id.
2. No scenario SKIPPED, none substituted by another's result.
3. Every deterministic scenario reproducible across three consecutive runs.
4. Every bounded failure path observed with its bound hit exactly — S8 timeout,
   S17 exhaustion, S26 backoff, execution retry ×3.
5. All seven model-dependent components meet the §M3 thresholds.
6. Zero contamination-detector violations across the campaign.
7. No PASS-WITH-RETRY in the final run.
8. PRIMARY untouched, verified from the allowlist audit log.

Then, and only then, the separate PRIMARY acceptance set runs.

---

## 10. Start here

1. Read `docs/acceptance/ROADMAP.md` in full.
2. Confirm current state yourself — do not trust §2:
   `git log --oneline -1` → `50b259f`; `uv run pytest -q | tail -1` → 413.
3. Begin **M3**. It is the critical path and it is what unblocks #16.
4. Report per §6, with command output.

---

## 11. Resolved contract amendments

These supersede any conflicting wording above. All seven were raised as genuine
contradictions; the resolutions are binding.

### A1 — D2 is a third hard stop. STOP 1 is not reachable without it.

Correct catch: K5 requires the #16 fixes applied, D2 forbids applying them.
The resolution is that **D2 is an additional checkpoint, not a contradiction to
work around**. Execute autonomously up to and including the D2 evidence report,
then stop and wait. After the human returns a classification, continue
autonomously through K5–K8 to STOP 1.

Revised stop list: **D2 report → STOP 1 (first live GitHub) → STOP 2 (PRIMARY).**

### A2 — K4 is rewritten. The historical failures are not reproducible.

Correct. `last_error` was truncated at 1000 chars by
`record_dispatcher_failure`, the outcome was never persisted, and the model was
not recorded. K4 now reads:

> **K4** Fresh inspector failures reproduced from the frozen RF-016 input
> against the configured review model, each attributed to a specific
> `GuardCode`, and each `UNKNOWN` resolved to A or B on the evidence of what
> the model actually emitted. Fresh failures are the evidence; the historical
> three are not recoverable and must not be claimed.

Fresh failures against the current model are the more useful evidence anyway.

### A3 — K7 sampling unit: 20 per fixture, staged.

20 top-level invocations per fixture across the corpus, one explicitly named
model. Run a **3-per-fixture smoke pass first** and report it before committing
to the full pass — gross problems should not cost 160 full reviews to discover.

### A4 — Success criteria differ by fixture class.

- **RF-016** (no expected verdict): bounded-eventual success means returning a
  structurally valid `ExecutionReviewResult` without `ReviewFinalizationError`.
- **Corpus fixtures**: see A5. Not a uniform verdict match.

### A5 — The corpus splits into STABLE and CONTESTED. This matters.

Four of seven corpus fixtures record a guard rejecting the model:

| Fixture | Verdict | Class |
| --- | --- | --- |
| RF-10, RF-12, RF-14-41504759 | NEEDS_FIXES | **STABLE** — model-driven verdict |
| RF-7, RF-11, RF-13, RF-14-52e49d5a | BLOCKED | **CONTESTED** — guard-driven veto |

RF-13 and RF-14-52e49d5a both summarise as *"Structured ACCEPT did not provide
complete satisfied coverage"* — `_guard_accept_coverage` overriding a model
that said ACCEPT. RF-7 and RF-11 read as the model being satisfied and blocked
anyway. **These are the same failure family as #16.**

Consequences, all binding:

1. **K6 regression applies to STABLE fixtures only.** Those three must keep
   their verdict class through any fix.
2. **CONTESTED fixtures are evidence, not baseline.** A CONTESTED fixture
   flipping BLOCKED → ACCEPT after a Class A fix is the *expected* outcome and
   must be declared explicitly, per fixture, with the guard code that changed.
   It is never silently accepted and never treated as a regression.
3. **The D2 report covers all five guard-veto cases** — RF-016 plus the four
   CONTESTED fixtures — not #16 alone. Five independent instances is a far
   stronger basis for an A/B call than one.
4. If a CONTESTED fixture flips for a reason unrelated to the fix under test,
   that is a regression and must be reported as one.

### A6 — Multi-code classification precedence: accepted as proposed.

Any `UNKNOWN` code present → `UNCLASSIFIED`. Otherwise any Class A → Class A.
Otherwise Class B. Every individual code is retained in the report so the
precedence can never hide evidence.

### A7 — "One milestone per session" means one branch and commit series.

It was never intended as a required conversational pause. Separate branch,
separate commit series, full gates per milestone. Proceed autonomously across
milestone boundaries.

### A8 — Gate K governs STOP 1, not M5 completion.

K8 requires only S1's deterministic scenario plus the PRIMARY negative test.
M5 need not be complete to reach STOP 1. D1 remains required before M7 can
finish, and the S17/S26 portions that depend on it stay explicitly blocked and
reported until it is answered.

---

## 12. Judgment checkpoints

At each checkpoint below: **stop, emit the evidence package, wait.** Do not
proceed on your own reading, and do not proceed on silence.

Two rules govern every package:

1. **It must be self-contained and pasteable.** The person answering may not
   have your session, your files, or your terminal. "See
   `acceptance/reports/m3-rf016.json`" is not an evidence package. Inline the
   relevant content.
2. **Quote literally; never paraphrase model output or guard conditions.** A
   summary of what the model emitted is not evidence about what the model
   emitted. If it is long, bound it and say where it was cut.

State your own recommendation in every package. You have the most context; a
recommendation you can be argued out of is more useful than neutrality. But
having recommended, wait.

---

### J1 — Class A/B classification  *(this is D2; the highest-value checkpoint)*

**When:** after M3 conformance runs over RF-016 and the four CONTESTED corpus
fixtures.

**Per guard-veto case — five of them — emit:**

```
CASE: <fixture id>
  Guard code(s):        <code>, fired <n>/<N> runs
  Guard site:           <function name>, reviewer.py:<line>
  Guard requires:       <the literal condition, quoted from source>
  Model emitted:        <the literal artifact for that requirement, verbatim>
  Ledger available:     <read-ledger entries present at that moment: path,
                         offset, returned_lines>
  Execution evidence:   <evidence ids available, or none>
  Could the model have satisfied the guard with what it had?  yes / no / unclear
  Your call:            A (guard wrong) | B (model wrong)
  Your reasoning:       <2-4 sentences>
  If A, the fix is:     <what specifically would change in the guard>
  If B, the fix is:     <what specifically would change in the prompt/contract>
```

Then a cross-case section:

```
COMMON CAUSE ANALYSIS
  Do these five trace to one design problem or five independent defects?
  Which guard families are implicated, and in what proportion?
  Full guard histogram across all runs.
```

That last question is the point of the checkpoint. Five vetoes across four
different issues converging on `_guard_accept_coverage` and the `IA-*` family
would be one design problem appearing five times, not five bugs — and the
correct response to those two situations is completely different.

---

### J2 — Class A fix design

**When:** after a J1 classification returns Class A, before writing the fix.

A classification says the guard is wrong. It does not say what right looks
like. Emit:

```
  Guard:              <code, site>
  Current condition:  <quoted>
  Proposed condition: <quoted>
  What this now admits that it previously rejected:  <specific>
  What it still rejects:                             <specific>
  Blast radius:       which of the 8 fixtures change verdict, and to what
  STABLE fixtures affected:  <must be none — if any, stop and say so>
```

The `STABLE` line is the safety property. A fix that moves RF-10, RF-12 or
RF-14-41504759 is a regression regardless of how good the reasoning was.

---

### J3 — Review-infrastructure retry bound  *(this is D1)*

**When:** on reaching S17/S26 in M5e, or earlier if it blocks you.

Emit the current behaviour with evidence — the escape path from
`WorkflowEngine.advance()`, the `dispatcher_failures` row, the delay cap, the
absent count bound — and what each option costs to implement. Then wait. Do not
pick one because it is easier to test.

---

### J4 — Gate K clearance / first live GitHub run  *(STOP 1)*

**When:** K1–K8 all believed satisfied.

Emit each of K1–K8 with **the literal command output** proving it, plus:

```
  First live scenario:  S1
  Target repository:    <full name, and proof it is not PRIMARY>
  What will be created: <issues, branches, PRs, comments — exhaustively>
  Cleanup plan:         <and proof the reaper works standalone>
  Rollback:             <if S1 fails halfway, what state is left behind>
```

The rollback line is the one that gets skipped. Answer it.

---

### J5 — PRIMARY  *(STOP 2)*

Any action touching PRIMARY, at any time, for any reason. There is no evidence
package that pre-authorises this; it is a separate exercise after the campaign
exit condition holds.

---

### J6 — Unexpected corpus movement

**When:** any fixture changes verdict for a reason not predicted in a J2
package, or any `STABLE` fixture moves at all.

Emit which fixture, from what to what, the guard codes before and after, and
your hypothesis. Do not adjust the fixture, the expectation, or the guard to
make it agree. **A fixture that moves unexpectedly is a finding.** Treating it
as noise is how a real defect gets normalised into the baseline.

---

### J7 — Production behaviour change not authorised by a work order

**When:** you conclude that correct implementation requires changing what
SWEForge *does*, not merely what it records or exposes.

Adding an observer callback, an event emission, or a fault hook is
instrumentation — proceed. Changing a state transition, a retry policy, a
permit rule, a guard's verdict, or a publication condition is behaviour — stop
and ask. If you are unsure which side of the line something falls on, that
uncertainty is itself the signal to ask.

---

### J8 — Runtime dependency

Any addition to `[project.dependencies]`. Dev/test-only additions proceed with
justification in the summary.

---

### Delegation (2026-08-25)

The owner has delegated judgment for **J1, J2, J3, J6, J7 and J8**. Decide them
yourself, on evidence, and record the decision and its reasoning in the
iteration report. Do not stall waiting for a human on those.

**J4 and J5 are not delegated and are not judgment calls.** They authorise
outward-facing, hard-to-reverse writes to real GitHub repositories — creating
issues, branches, pull requests and comments. Delegated *reasoning* is not
delegated *authorisation to publish*. Both still stop and wait.

This costs nothing in practice while the work is M2/M4/M5, which never touch
GitHub, so neither can arise. It binds at the live boundary.

### Decisions returned (2026-08-25)

**D1 — RESOLVED: bound review-infrastructure retries at 3.** Matches the
execution retry bound and restores the system's stated property that every
retry path terminates. S17 and S26 are unblocked; J3 no longer stops.

**K7 — RESOLVED for now: certify against `openai:gpt-5.6-sol`.** The production
review model is still undecided. The README's `anthropic:claude-sonnet-4-6`
default is outdated and is not evidence of what production will run, so the
earlier sonnet-portability concern is parked rather than blocking. If a
production model is later chosen that is not sol, conformance must be re-run
against it — a threshold met by one model certifies only that model.

**Budget — 22% of the weekly OpenAI allowance remains** (was 30% earlier the
same day). A full 20x8 certification batch is ~30% and is therefore no longer
affordable this week. Stay on the §13 cost ladder: free replay and offline work
first, single-fixture runs next, and no full-corpus run without stating what
question it answers that a cheaper rung could not.

### Checkpoint summary

| ID | Trigger | Blocks |
| --- | --- | --- |
| **J1** | M3 conformance complete | K5, everything downstream |
| **J2** | Any Class A classification returned | The fix itself |
| **J3** | S17/S26 in M5e | M7 completion only |
| **J4** | Gate K believed satisfied | All live GitHub work |
| **J5** | Anything touching PRIMARY | Absolutely |
| **J6** | Unexpected fixture movement | The milestone in progress |
| **J7** | Behaviour change, not instrumentation | That change |
| **J8** | Runtime dependency | That dependency |

Everything not on this list, you decide.

---

## 13. Provider budget discipline

A full 20×8 conformance batch consumes roughly 30% of the weekly model budget.
Three were run in one day. That rate is not sustainable and it is not necessary.

**Rule: a full batch is a certification, never an iteration.**

### The reports are a cassette corpus

`acceptance/runner/conformance.py` persists `artifact` and `evaluated_artifact`
per observation, so every report in `acceptance/reports/` contains the complete
model output for every run. A 20×8 batch is 160 recorded artifacts, already
paid for.

Guard logic, canonical binding, classification and adjudication are
**deterministic Python**. Changing any of them does not require new model calls
to evaluate — it requires replaying recorded artifacts through the new code.

**Build `--replay-report <path>`**: load a prior report, re-run every recorded
artifact through current adjudication, and diff the outcomes against what was
recorded. Free, deterministic, and it covers all 160 runs rather than a sample.
This is a permanent capability, not a one-off: it converts every expensive batch
into a durable regression asset, and every future guard change is validated
against the full corpus at zero cost.

Its limit, which must be stated in every report it produces: replay proves what
**current code** does with **previously observed** model output. It cannot prove
how the model responds to a changed prompt or a new schema. That still needs
real calls.

For a schema addition, replay failures caused solely by every older artifact
lacking the newly introduced shape are tautological, not blast-radius evidence.
No further replay of those same artifacts can establish whether a model will
adopt the new shape.

### Cost ladder — climb it in order, stop at the first rung that answers

| Rung | Cost | Answers |
| --- | --- | --- |
| 0. Inspect saved reports | free | What did the model actually emit? Which runs, which refs, which codes? |
| 1. `--replay-report` | free | Does an adjudication change alter outcomes? What is the blast radius across all 8 fixtures? |
| 2. Single fixture, N=5 | ~1% weekly | Does the model emit the new shape at all? |
| 3. Single fixture, N=20 | ~4% weekly | Does that fixture meet threshold? |
| 4. Full corpus smoke, 3×8 | ~5% weekly | Any cross-fixture surprise before certifying? |
| 5. Full corpus, 20×8 | ~30% weekly | **Certification only.** |

Never run rung 5 to learn something a lower rung could have told you. Before any
rung 4 or 5 run, state in one sentence what question it answers that rung 3
could not.

### Model selection

Guard mechanics are model-independent — what varies is whether a model emits the
required shape. Validate mechanism with the cheapest model that emits the shape
at all; reserve the configured production review model for threshold
certification. Record the model in every report; a threshold met by one model
says nothing about another.
