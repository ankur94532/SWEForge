# Acceptance Coverage Gaps — proposed S27–S47

S1–S26 cover the workflow state machine well. They cover the peripheral
subsystems thinly. This is the gap list, ranked by severity.

Two of these — S27 and S28 — are the ones I would not ship without.

## Critical — authorization and security fail-closed

| # | Scenario | Why it matters | Uncovered path |
| --- | --- | --- | --- |
| **S27** | AUTO mode full lifecycle | `PermitSource.AUTO` is a second authorization path where no human approves. S1–S26 never exercise it. | `authorize_auto`, `WorkflowMode.AUTO` |
| **S28** | Immutable AUTO capture | Superseded legacy behavior re-read the label at permit creation. Production now captures AUTO once on IssueThread creation; later label removal or addition is ignored. | `issue_threads.interaction_mode`, declarative runtime |
| **S29** | No sandbox provider configured | Strict execution must refuse to run. Security-critical fail-closed. | `SecureExecutionUnavailable` |
| **S30** | Unapproved MCP tool invoked | Interceptor must reject. Distinct from S9 — that proves identity, this proves the allowlist. | `repo_scope_interceptor` |

## High — durability and idempotency

| # | Scenario | Why it matters | Uncovered path |
| --- | --- | --- | --- |
| **S31** | Crash between commit and push | The classic partial-publication window. Restart must resume idempotently: no second commit, no second PR. | `publication_id` reconciliation |
| **S32** | Divergent remote branch | Must refuse, never force-push. | `github_publisher.py` divergence check |
| **S33** | Ambiguous PR or comment match | Two matching PRs must fail closed, not pick one. | `github_publisher.py:200,242` |
| **S34** | Repair loop to exhaustion | Bounded at 5, never scenario-tested. Must reach `REVIEW_BLOCKED` with no publication. | `create_repair_permit`, `max_repairs` |
| **S35** | Successful repair to ACCEPT | The repair *success* path. S26 covers review infra failure; nothing covers repair working. | `REPAIR_READY` → ACCEPT → publish |
| **S36** | One SourceEvent, two logical inputs | README specifies S → D1, D2 → two cycles, two publications. D1's ACCEPT must not authorize D2. | `deferred_id`, sibling-cycle isolation |

## Medium — subsystem correctness

| # | Scenario | Why it matters | Uncovered path |
| --- | --- | --- | --- |
| **S37** | Two clarifications in one cycle | README describes `occurrence_key` resume selection in detail. Each interrupt must get its own answer. | `occurrence_key` (18 sites) |
| **S38** | Poller correctness across polls | ETags, cursors, pagination. No event missed, none ingested twice. | `github_poller.py` |
| **S39** | Issue edited after root ingestion | There is a commit specifically for deduplicating unchanged roots. | root dedup |
| **S40** | `propose_repo_memory` validator corpus | Path escape, inverted/out-of-range spans, secret-looking evidence. A corpus, like S21. | proposal validator |
| **S41** | Resolution retrieval isolation | Repo A must not retrieve repo B's cases. S11 is concurrency; this is retrieval. | FTS5 `repo_id` filter |
| **S42** | Planner model failure | S26 covers reviewer infra failure. Planner failure is unhandled by any scenario. | `generate_plan` |
| **S43** | Execution model provider failure | Distinct from `fatal_probe` — that is tool failure, this is the model itself. | execution agent |
| **S44** | Branch exists without worktree | Crash mid `worktree add`. Must reattach, not recreate. | `ThreadWorkspace.create` |
| **S45** | Local checkout missing or moved | `--repo-path` target gone. Must fail closed. | repo path resolution |

## Low — worth a unit test, probably not a scenario

| # | Scenario | Note |
| --- | --- | --- |
| **S46** | Installation token expiry mid-publication | Refresh window is 9 min; hard to stage, low value |
| **S47** | Pre-existing database schema migration | README claims in-place migration; deserves a test, not a scenario |

## What this list still does not cover

Honesty about the limits of any finite list:

- **Resource exhaustion** — disk full, SQLite `database is locked` under real
  contention, file-descriptor limits. Real, but poorly served by scenarios.
- **Adversarial repository content** — a target repo whose files are crafted to
  attack the reviewer's prompt or the evidence packer. S21 covers filesystem
  escape, not prompt injection through repository content.
- **LangGraph checkpoint corruption** — truncated or partially written
  checkpoint state.
- **Time and clock** — timestamp ordering across restarts, DST, clock skew
  between poll cursor and event time.
- **Scale** — everything here is small-N. Nothing establishes behaviour at 500
  queued threads or a 60k-line diff beyond the packer's own bounds.

The right claim is not "S1–S47 covers all possible failures." It is that the
identified *failure classes* are covered, and that the frozen-fixture mechanism
converts newly discovered classes into permanent regression coverage instead of
one-off live discoveries.

## Candidate defects requiring contract decisions

### F1 — canonical absence-of-change binding must be stage-symmetric

RF-13's structural requirement was: "Do not modify any file under
/src/main/java (production code must remain unchanged) - verify by diffing only
the test file after edits." The first inspection cited `src/main/java` as a
`TRUSTED_DIFF`; canonical binding correctly dropped the directory
pseudo-reference. The correction then passed by citing the positive diff for
`src/test/java/com/sweforge/pricing/PricingCalculatorTest.java` lines 18–33.

That positive test-file diff is not evidence that production files did not
change. This confirmed soundness gap led to the approved `ABSENCE_OF_CHANGE`
evidence kind, bound by the application against authoritative `changed_files`.

The first RF-13 × 5 Luna run exposed the same defect one layer later. Inspector
output with `source_id=''` was canonically bound and accepted, but identical
finalizer output was adjudicated without binding and rejected as
`IA-INVALID-ABSENCE-OF-CHANGE`. The finalizer had no route to learn the minted
identifier. Finalizer absence nominations therefore require the same canonical
binding before adjudication; an unbound blank identifier remains invalid.

The same run also showed the old evidence-selection habit persists: ACCEPT
finalizers continued to cite the positive `PricingCalculatorTest.java`
`TRUSTED_DIFF` alongside the relevant absence authority. The positive diff is
harmless once the required absence reference is independently bound and
adjudicated, but it is not itself proof of the negative structural claim.

---

## Guard audit — 2026-08-25

Triggered by F1 recurring three times. Rather than a fourth point fix, every
reviewer guard was audited against: *can the stage being judged actually
produce what this demands?*

### The three F1 instances

| # | Guard demanded | Stage could not produce | Class | Status |
| --- | --- | --- | --- | --- |
| 1 | Evidence that a file did **not** change | No evidence kind expressed absence; `if not refs` accepted any substitute | A | fixed — `ABSENCE_OF_CHANGE` |
| 2 | A canonical `source_id` on a finalizer absence ref | Binding ran on inspector output only; the finalizer was never given the minted id | A | fixed — symmetric binding |
| 3 | Coverage of every prose-extracted target | Directory scope matched basename `java`; per-file refs left the directory uncovered — both required at once | A | fixed — directory subsumes files, gated on `changed_files` |

All three are the same shape: **a guard demanding evidence the judged stage had
no route to produce.** None was caught by a test. All three were found by
running a real model against a frozen fixture and reading what it emitted.

### Measured state of the guard set

Across **784 recorded conformance runs**:

- **10 of 43** guards have ever fired. All ten are `IA-*` inspection-authority
  or `_reference_problems` guards.
- **33 of 43** have never fired in any recorded run.
- **38 of 43** have no reference in any test.

The overlap is the finding. For most guards nothing establishes that they *can*
fire, and nothing establishes that they *can be satisfied*. A guard in that
state is not protection — it is an untested fail-closed branch that will first
be exercised by a live run, which is exactly how the campaign has been paying
for its defects.

Firing counts alone cannot separate "correct and never violated" from
"unreachable". Only a test can.

### Remediation rule

Every `GuardCode` requires two tests before it can be trusted:

1. **Reachability** — an input that makes it fire. Proves the branch is
   reachable and the condition means something.
2. **Satisfiability** — an input a well-behaved stage could plausibly produce
   that does *not* fire it. Proves the demand is answerable with what the stage
   is given.

The absence-coverage fix follows this pattern (11 tests, both directions,
including the negative case where an unrelated quiet directory must not
vacuously satisfy a named file). The other 38 guards do not.

This is a prerequisite for trusting a Class A rate of zero: a guard that cannot
fire cannot produce a Class A observation, so a clean histogram may reflect dead
branches rather than a healthy reviewer.

### Not yet done

Writing the 76 tests. Sized but not started; it is the largest single piece of
remaining reviewer work and it gates any claim that conformance measures the
whole guard set rather than the tenth of it that has ever executed.

---

## Approval-boundary findings — 2026-08-25 (from S23)

Writing the S23 near-miss corpus surfaced two places where the roadmap's
expectation and the implementation disagree. Both are pinned by tests asserting
**current** behaviour, so changing either becomes a deliberate, visible edit
rather than silent drift.

### F3 — doubled whitespace approves, and the roadmap was wrong

`_EXACT_APPROVAL_RE` in `src/sweforge/github_models.py:24` is
`^\s*@agent\s+approve\s*$`. `\s+` canonicalises any run of whitespace, so
`@agent  approve` approves — and so does `@agent\napprove`.

**Assessment: the implementation is right and the roadmap's S23 payload list was
wrong.** A user typing two spaces plainly means the same command; requiring
exactly one space would be brittle and would reject a legitimate approval. The
roadmap entry has been corrected rather than the regex.

The newline case is more debatable — `@agent\napprove` is less obviously one
command — but it is low risk and tightening it would need a reason beyond taste.

### F4 — no approver check exists at all

`WorkflowEngine.approve` (`src/sweforge/workflow.py:547`) accepts an
`author_login`, **records** it, and never checks it. Any actor whose comment the
poller ingests can approve a plan and cause execution.

Verified: no permission, role, or allowlist check exists anywhere on the
approval path.

This may be deliberate — authorization is arguably delegated to GitHub's own
repository-permission layer, and on a private repository only collaborators can
comment. On a **public** repository, any GitHub user can comment on an issue,
which would make plan approval open to anyone.

**RESOLVED 2026-08-25.** The deployment decision was made: approval requires
repository **write access**. `WorkflowEngine._require_authorized_approver`
now runs immediately before the permit is minted, alongside the existing
conversation-target and plan-version checks.

`APPROVER_PERMISSIONS` is `{admin, maintain, write}`. `triage` and `read` are
deliberately excluded: both can comment, neither can change the repository,
and approval is exactly what lets the agent write to it.

The check fails closed. An indeterminate permission — an unreachable API, a
client that cannot answer, a missing author or repository — is refused rather
than assumed, and a refused approval mints no permit, so the thread stays in
WAITING_FOR_PLAN_APPROVAL.

`GitHubClient.collaborator_permission` was added for this, backed by
`GET /repos/{owner}/{repo}/collaborators/{username}/permission`.


## Decided: repo memory needs no mandatory human approval (2026-08-26)

The branch `codex/memory-learning` (single commit `16ea1ea`, 21 Aug) carried a
different design for repository memory: a model proposed candidates and a human
called `approve_candidate` or `reject_candidate` before any of them became
repository memory.

It was not merged, and the branch was deleted. It is not lost -- `16ea1ea`
remains on `origin/codex/memory-learning`, and the design can be recovered from
there if the decision is ever revisited.

**What replaced it.** The implementation on the main line grounds memory in
evidence instead of in approval: every candidate must cite a path, a line range
and a content hash, each verified against the worktree, with path-escape,
inverted-range, range-past-end, task-specific-fact and secret checks. S40
exercises that corpus payload by payload.

**Why that is enough.** Evidence grounding is mechanical and does not tire,
where an approval gate does -- this campaign found two approval gates that were
waved through, F4 accepting any commenter and the AUTO path minting permits
with no audit event at all. A human retains an explicit review and edit path
through `repo_memory_cli`: `show`, `replace` and `append`.

**The residual limit, stated plainly.** Grounding proves provenance, not truth.
A memory can cite real lines and still be misleading. Nothing catches a
well-grounded but wrong repo fact, and repository memory feeds future planning.
This is accepted deliberately rather than overlooked; the mitigation is that a
human can inspect and rewrite memory at any time.

Note this concerns repository *facts* only. Issue-resolution memory -- the
root-cause records of past issues surfaced to the planner -- is a separate
subsystem, and is already presented as fallible clues to verify rather than as
repository truth.
