# Handoff — remaining acceptance work

Written 2026-08-25 at the point of transferring execution to Codex. Supersedes
`CODEX-BRIEF.md` (which describes milestones that are now complete).

## Verified current state

```
uv run pytest -q -n auto   681 passed        (399 at session start)
uv run ruff check .        All checks passed!
uv run ruff format --check clean
git HEAD                   33a0ea0
uncommitted                42 paths — ALL of the work below is uncommitted
```

**21 of 26 L1 scenarios pass**: S1, S4–S15, S18–S21, S23–S26.
**Remaining: S2, S3, S16, S17.**

### Complete

| Milestone | What exists |
| --- | --- |
| M0 guard codes | 41 stable `GuardCode`s; `acceptance/guard_classification.json` (25 B, 16 UNKNOWN) |
| M1 fixtures | Schema v1, capture hook, `sweforge-review-freeze`, `sweforge-review-replay`, 8 frozen fixtures |
| M1c probes | `acceptance/probes/{server,ledger}.py` — six probes on a SQLite ledger that survives restart |
| M2 event log | `src/sweforge/events.py`, 13 emission sites, structural redaction |
| M3 conformance | `acceptance/runner/conformance.py`, dual thresholds, `--replay-report` |
| M4 harness | `tests/conftest.py`, `tests/harness/*` — 30 invariants, `@scenario`, `World`, doubles, `ServeProcess` |
| D1 | Review retries bounded at 3 via `review_recovery_count` |
| PRIMARY | `acceptance/runner/allowlist.py`, fails closed, K8 negative test |

### Remaining, in order

1. **S2, S3, S16, S17** — completes M5.
2. **Wire `ROOT_INGESTED` and `INPUT_DELIVERED`** — both are in the event
   schema with no emission site, so any invariant reading them passes
   vacuously.
3. **`acceptance/runner/cli.py`** — run a scenario by id at a chosen layer,
   call `check_live_target()` before any mutating live action, write
   `campaign-status.json`, plus a standalone reaper for crashed runs.
4. **The 14 LIVE-PROCESS integration runs** — S3, S5, S6, S7, S12, S13, S14,
   S17, S21, S22, S23, S24, S25, S26. No provider, no GitHub, not gated by J4.
5. **K7 certification** — see budget below.
6. **The 76 guard tests** from the audit, then **S27–S36**.

## Budget: two separate pools

The cliproxy authenticates **through Codex**. That is why `gpt-5.6-luna`
appeared free, and why it died with `auth_unavailable: providers=codex` when
Codex ran out.

| Model | Pool | State |
| --- | --- | --- |
| `gpt-5.6-luna` | Codex weekly | restored to 100% |
| `gpt-5.6-sol`, `gpt-5.6-terra` | OpenAI API key | 100% |

**K7 plan:** run the full 20×8 certification on **luna**, then a 3×8
cross-check on **sol**. Two models agreeing is materially stronger than one,
and it addresses the portability risk that `claude-sonnet-4-6` measured 0.44
against sol's 1.00 on the same frozen corpus.

Env: `set -a; . $(ls -t ~/.sweforge/e2e/*/cliproxy.env|head -1); set +a`.
**Serial only** (`--workers 1 --run-workers 1`) — higher concurrency overloads
the proxy with 502s.

## Standing rules (each earned by a real defect)

- **Never let a check return empty/false when it cannot observe — raise.**
  `forced_updates()` reported "no force-push" because bare repos disable
  reflog; the isolation invariants would have passed on an empty declared set.
  The scenario runner treats *unevaluable* as FAIL.
- **Every invariant and guard needs a passing AND a failing test.** Writing the
  failing one twice revealed the schema already enforced what the check claimed
  to guard (`evidence_start_line NOT NULL`, `UNIQUE(repo_id, pr_number)`).
- **Include positive controls.** S23 proves approvals *can* mint in a separate
  world, otherwise `INV-PERMIT-NONE` could pass because minting is broken.
- **Read the real code before concluding a cause.** Five wrong diagnoses in one
  day, each from one suggestive line: two healthy conformance batches killed on
  low CPU (normal for I/O-bound work); an `ABSENCE` failure called Class B when
  it was Class A; the accept-coverage guard called invisible when
  `guard_problems` is passed two lines later; force-push detected via a reflog
  string that only exists client-side.
- **A green suite does not prove a new call site works.** `World.build`
  silently discarded its `planner` argument — a string replacement that
  no-opped after ruff reflowed the line — and every test still passed. Verify
  edits landed; grep for them.
- **Never kill a conformance batch on low CPU or idle sockets.** That is the
  normal signature of I/O-bound model work. Kill only on a failed independent
  proxy health check or a pre-declared wall-clock budget (~2–3 min/run).
- **`--replay-report` cannot validate a schema addition.** Pre-schema artifacts
  necessarily fail a new guard, so "118/160 changed" is a tautology.
- **Seed test state through the store's public API**, not raw SQL — foreign
  keys, NOT NULLs and `source_created_at` all bite.

## Harness facts

- Tests import as `from harness.world import World`; `tests/conftest.py` puts
  `tests/` on `sys.path`. Scenarios live in **`tests/scenarios/l1/`** — never
  `tests/acceptance/`, which shadows the repo-root `acceptance/` package.
- `execute_authorized()` receives **only** `execute_kwargs`, so `model`,
  `repo_paths` and `workspace_root` must be inside it. `World.tick` handles
  this and merges caller overrides over its defaults.
- A runner double receives `worktree` in kwargs and **must mutate it** for
  changes to appear. A raising runner yields a `FAILED` attempt and
  `EXECUTION_FAILED`. A runner may ingest events mid-execution (S18) or call
  probe functions directly (S4–S9).
- **An approval event must carry the same `issue_number` as its thread**, or it
  routes to a different thread entirely.
- `World.later()` for timestamps — never hardcode against the synthetic clock.
- `thread_lock(root, thread_id)` — that argument order.
- `World.build(...)` accepts `planner`, `reviewer`, `memory_learner`,
  `clarification_classifier`, `with_origin`, `repo_id`, `full_name`.
- `FaultyRole` fails at explicit 1-based call indices, never probabilistically.
- `ReviewFinalizationError` is caught in `advance()` and bounded; below the
  bound it re-raises so dispatcher backoff owns the retry.

## Two stops that remain human

- **J4** — the first live GitHub write. Creates real issues, branches and PRs.
- **J5** — anything touching PRIMARY, ever, during the stress campaign.

Everything else is delegated.

## Open findings, deliberately unfixed

- **F4 — there is no approver check.** `WorkflowEngine.approve` records
  `author_login` and never checks it. On a public repository any GitHub user
  could approve a plan. Pinned by a test asserting current behaviour; whether
  it is a defect depends on the deployment model.
- **Guard audit** — only 10 of 43 guards have ever fired across 784 recorded
  runs, and 38 have no test. A Class A rate of zero may reflect dead branches.
  76 tests sized, not started.
