# SWEForge V1 Live E2E Campaign — Handoff

> Historical continuation note. The campaign is now complete; see
> [`CAMPAIGN-FINAL-REPORT.md`](CAMPAIGN-FINAL-REPORT.md) for the authoritative
> final results, all six local fixes, and the final acceptance verdict.

Status: **partially complete, 4 product bugs found and fixed.** Nothing pushed.

---

## 1. Repository state

| Item | Value |
|---|---|
| Campaign start HEAD / `origin/main` | `7aced32` Add incremental repo configuration commands |
| Current HEAD | `8b4e5a8` |
| Commits added (local only, **NOT pushed**) | 4 (see §4) |
| `origin/main` | still `7aced32` — **nothing pushed** |
| Full suite | **1194 passed, 12 skipped** |
| Ruff / format / `git diff --check` | all clean |

```
8b4e5a8 Constrain the validation verdict to its legal values
7fcd15f Fail closed when a resumed turn errors without advancing
4885e43 Authorize the replayed gateway during a feedback review
8c2f1d2 Resume interrupted feedback reviews after a restart
7aced32 Add incremental repo configuration commands   <-- campaign baseline
```

### Uncommitted work in the worktree (DO NOT discard)

There is a **separate, unrelated** body of work sitting uncommitted: an
expansion of `examples/repo-config` (done immediately before the campaign, at
the user's request). It is complete and green, just never committed because
the user hadn't asked.

```
 M README.md, docs/adding-a-repository.md, src/sweforge/repo_config_cli.py
 M examples/repo-config/{workflow.yaml,skills/*,tools/mcp/README.md}
 D examples/repo-config/tools/mcp/servers.example.yaml
 M tests/{test_repo_admin_cli,test_repo_config,test_repo_config_cli,
           test_repo_secret_cli,test_repo_secret_injection,test_script_tools}.py
?? examples/README.md
?? examples/repo-config/skills/readiness-rules/checks.md
?? examples/repo-config/skills/release-catalog/
?? examples/repo-config/tools/mcp/servers.yaml
?? examples/repo-config/tools/scripts/README.md
?? examples/repo-config/tools/scripts/write-readiness-report/
```

Suggested commit message if the user approves: `Expand the reference
repository configuration bundle`. **When committing campaign bug fixes, always
stage explicit file paths — never `git add -A`** or this work gets swept in.

---

## 2. Environment — how to resume (CRITICAL)

### Campaign root
```
/tmp/sweforge-live-e2e-20260830T115148Z/
├── campaign-env.sh      # source this first — sets everything
├── run-serve.sh         # long-running server
├── worker.py            # single-thread worker (one drain, for debugging)
├── state.sh <issue#>    # prints cycle + task phases
├── approver.sh <issue#> # posts real `@agent approve` when a task waits
├── state.db  checkpoints.sqlite  memory.sqlite
├── .secret-master-key   # Fernet key for this campaign only
├── mcp/catalog_server.py + servers.yaml   # real stdio MCP test server
├── workspaces/1350417130/issue-{16,17,18,19}/
└── logs/serve*.log, approver*.log
```

### Model transport — CLI proxy only, NO provider API key
`cliproxyapi` on `127.0.0.1:8317` (check with `pgrep -fl cliproxyapi`; restart
with `nohup /opt/homebrew/bin/cliproxyapi -config ~/.cli-proxy-api/config.yaml &`).

Two routes are needed, **both Claude, both the same proxy**:

| Role | Model string | Env | Why |
|---|---|---|---|
| planning / execution / review | `openai:claude-sonnet-5` | `OPENAI_BASE_URL=http://127.0.0.1:8317/v1` | deepagents + `ToolStrategy` verified working |
| clarification / memory / resolution | `anthropic:claude-sonnet-5` | `ANTHROPIC_BASE_URL=http://127.0.0.1:8317` (**no `/v1`**) | plain `with_structured_output` default method works |

Both use `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` = the `cpak-…` key from
`~/.cli-proxy-api/config.yaml` → `api-keys[0]`. Parse it with YAML — `tr -d '-'`
eats the hyphen in `cpak-` and gives a 401.

**Why the split (do not "simplify" it):**
- `anthropic:` + deepagents → `400 A maximum of 4 blocks with cache_control may be provided. Found 5.`
- `openai:claude-*` + default `with_structured_output` → returns prose, `ValidationError`. (`method="function_calling"` works, but SWEForge uses the default in `clarification.py`, `issue_resolution.py`, `memory_learning.py`.)

This split uses SWEForge's own per-role model flags. **No product code was
changed to accommodate the proxy.**

### GitHub
- App creds come from `SWEForge/.env` (gitignored): `SWEFORGE_GITHUB_APP_CLIENT_ID`,
  `SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH` → `~/.sweforge/credentials/sweforge-dev.pem`.
- **`sweforge-serve` and `sweforge-github-workflow` REQUIRE App creds** — unlike
  `sweforge-github-poll`, they do *not* accept `SWEFORGE_GITHUB_TOKEN`.
- `gh` CLI is authed as `ankur94532`, ADMIN on the fixture.
- **The user asked that Claude post all issue/PR comments itself via `gh`.
  Never ask the user to comment.**

### Fixture
- Repo `ankur94532/SWEForge-Diamond-Test` (repo_id `1350417130`), local checkout
  `/Users/shashwattripathi/sweforge-fixtures/SWEForge-Diamond-Test`.
- It is purpose-built: `verify.py <baseline|model|loader|readiness|report>` is a
  deterministic per-stage verifier that **fails before the change and passes
  after**. This is objective ground truth for validation integrity.

### Sandbox
Real provider `acceptance-seatbelt` is installed and was used
(`--sandbox-provider acceptance-seatbelt`), **not** `--unsafe-local-shell`.

---

## 3. Live GitHub artifacts created

| Item | Purpose / state |
|---|---|
| Issue #16 | MANUAL, gen 1. Used for Phases 2/13/14. Now wedged (bound to a generation whose skills reference a tool its execution phase lacks). Has 2 queued revision inputs. |
| Issue #17 | Closed. Bound to gen 5 whose MCP path was invalid. |
| Issue #18 | Cycle terminally `FAILED` — killed by Bug #4 before the fix existed. Good FAILED-branch evidence. |
| **Issue #19** | **The clean run.** MANUAL, gen 7. Diamond + revision + publication all completed. `cycle=PUBLISHED`. |
| **PR #20** `sweforge/issue-19` | Created by SWEForge at publication. **This is the PR to use for the remaining PR-surface phases.** |
| PR #13 | Pre-existing, deliberately left unmapped — used for Phase 42. |

Config generations installed during the campaign: 1→8.
Issue→generation bindings: #16→1, #17→5, #18→7, #19→7.

---

## 4. Bugs found and fixed (4)

All four were found **only** by live running; the pre-existing 1184-test suite
passed throughout. Each has a focused regression test.

### Bug #1 — `8c2f1d2` Resume interrupted feedback reviews after a restart
- **Symptom:** a plan-feedback comment permanently wedged the thread. Review stuck
  `REVIEWING`, `is_thread_runnable=False`, user feedback silently dropped forever —
  no replan, no pushback, no ack.
- **Cause:** `begin_feedback_review` consumes its triggering input.
  `_declarative_thread_runnable` decided runnability at `WAITING_FOR_*` purely
  from *unconsumed* inputs, so nothing could ever re-select the thread.
- **Fix:** `github_store.py` — treat an in-flight review as durable work. The
  controller already reconstructed the resume payload; only selection was missing.
- **Tests:** `test_in_flight_feedback_review_keeps_its_thread_runnable[PLAN|RESULT]`.

### Bug #2 — `4885e43` Authorize the replayed gateway during a feedback review
- **Symptom:** every resumed feedback review failed with
  `PermissionError: tool 'submit_plan' is forbidden for PLANNING`. No review
  could ever be decided.
- **Cause:** LangGraph replays the interrupted gateway on resume. `submit_plan`/
  `finish_validation` already handle the `*_FEEDBACK` payload (they return the
  review instruction and cannot approve anything), but `allowed_tools`' `REVIEWING`
  branch omitted the phase gateway.
- **Fix:** `workflow_middleware.py` — add `_phase_gateway(snapshot.phase)` to that
  branch. Review stays read-only otherwise.
- **Tests:** `test_reviewing_feedback_still_authorizes_its_replayed_gateway[…]`.

### Bug #3 — `7fcd15f` Fail closed when a resumed turn errors without advancing
- **Symptom:** Bug #2's hard error was **silently swallowed** and reported as a
  completed turn; the drain loop re-dispatched forever (~2,500 iterations/worker,
  11k log lines, zero model calls, no error surfaced anywhere).
- **Cause:** the post-gateway allowance compared the durable phase against
  `before.phase`. For a resume that is a *synthetic* replay phase (`PLANNING` for a
  plan review) that never equals the durable waiting phase → true before the agent
  did anything → every resume exception swallowed.
- **Fix:** `workflow_agent_runtime.py` — compare against the **durable** phase
  captured at entry (`_durable_phase`). Genuine post-gateway transitions still pass.
- **Tests:** `test_resume_error_fails_closed_when_nothing_durably_advanced`,
  `test_resume_error_is_still_accepted_after_a_real_durable_advance`.

### Bug #4 — `8b4e5a8` Constrain the validation verdict to its legal values
- **Symptom:** model returned `verdict="pass"`; `ValidationVerdict('pass')` raised;
  worker died; after 3 dispatcher retries the **entire workflow cycle went FAILED**
  (this killed issue #18).
- **Cause:** `finish_validation(verdict: str, …)` advertised a free-form string for
  a 4-value enum, and the coercion error was not model-visible.
- **Fix:** `workflow_tools.py` — type it `ValidationVerdict`, putting the four
  values in the tool schema so bad values are rejected as argument errors.
- **Tests:** `test_validation_verdict_is_a_constrained_choice_not_free_text`.

### Open finding (NOT fixed — needs a product decision)
**Unauthorized/invalid model tool calls become hard dispatcher failures.** With
`handle_tool_errors=False`, a `PermissionError` from `wrap_tool_call` (e.g. the
model calling a tool its phase doesn't grant) propagates out and burns the
3-retry budget, then marks the cycle FAILED. The model never sees "that tool
isn't available here", so it repeats the mistake. The **security boundary held
perfectly** — this is availability/robustness, not a security hole. Changing it
alters error-handling policy, so it was deliberately left for the owner.
Observed live twice (`verify_stage` in EXECUTING; `verdict="pass"`).

---

## 5. Phases completed

| # | Phase | Result | Key evidence |
|---|---|---|---|
| 1 | Onboarding (`init`/`validate`/`configure`/`show`) | **LIVE PASS** | init installs no authority (no config tables); 4 bad bundles rejected pre-install; gen 1 digest `35f1eb75`; `show` leaks no skill/script bodies or secrets |
| 2 | Generation freezing | **LIVE PASS** | #16→gen 1 after 4 later generations; #19→gen 7; survives fresh processes |
| 3 | Incremental config CLI | **LIVE PASS** | add/replace/remove → gens 2–4; digest returns to gen 1's on round-trip (content-addressed); 5 negatives refused, pointer unmoved |
| 3.5 | Canonical materialization | **LIVE PASS** | source bundle **deleted from disk**, `mcp set` still produced gen 5 with all 9 files |
| 4/33 | Progressive skill discovery | **LIVE PASS** | `SKILL CATALOG: count=2` + on-demand `SKILL READ: skill=domain-model` / `fixture-conventions`; no eager bodies |
| 5 | Tool authority | **LIVE PASS** (partial) | `verify_stage` correctly refused in EXECUTING; MCP call without authoritative context rejected by interceptor |
| 6 | Script tools | **LIVE PASS** | `verify_stage` ran in real worktree, JSON stdin, bounded output, fixed entrypoint |
| 7 | Repo secrets | **LIVE PASS** | Fernet ciphertext at rest, no plaintext in raw `state.db` bytes, no `show` subcommand |
| 8 | Script secret injection | **LIVE PASS** | schema clean; stdout **and** stderr redacted to `[REDACTED]`; minimal child env `{LANG,PATH,PROBE_REGION,PROBE_TOKEN}`; metadata-only trace |
| 9 | Local stdio MCP | **LIVE PASS** | real MCP server; `required=1 resolved=1`; no credential in schema/trace |
| 11 | MANUAL happy path + diamond ordering | **LIVE PASS** | `model → config → readiness → reporting`, strict declaration order; reporting waited for both branches; exact `@agent approve` drove each transition |
| 13 | Plan feedback RELATED | **LIVE PASS** | `FEEDBACK REPLAN`; v1 `SUPERSEDED`, v2 `POSTED`; same task owner; new occurrence key |
| 14 | Plan feedback UNRELATED | **LIVE PASS** | `DEFERRED_WAITING`; plan retained at v1; queued once; deterministic pushback with stable marker |
| 18 | Generic revision loop | **LIVE PASS** | cycle 2 `kind=REVISION seq=1` with ONE logical `revision` task — did **not** re-run the diamond |
| 25 | Exact approval security | **LIVE PASS** | `please @agent approve` never ingested; `@agent approve please` / `@agent approved` ingested as feedback, approved nothing |
| 27/28 | Worktree / same branch+PR | **LIVE PASS** (partial) | one worktree per issue; all 4 tasks + revision on one branch `sweforge/issue-19`; one PR (#20) |
| 30 | Publication | **LIVE PASS** (partial) | `cycle=PUBLISHED`, PR #20 created after diamond + revision |
| 35 | Validation integrity | **LIVE PASS** | `verify.py model`/`loader` **pass** in #19's worktree; ACCEPT was evidence-based. FAILED branch observed on #18 |
| 39 | Secret rotation vs generation | **LIVE PASS** | rotation used next call, generation 8 + digest unchanged; delete → fail closed; re-add → runs again |
| 40 | Trace/security audit | **LIVE PASS** | 13,888 log lines: **0** occurrences of secret value, secret *names*, proxy key, `gho_`/`ghs_`, private-key markers, `Bearer`, or skill bodies |
| 41 | Actionability / noise | **LIVE PASS** | real non-actionable comments **never even persisted** as events |
| 42 | Unmapped PR | **LIVE PASS** | actionable comment on PR #13 persisted with `thread_id=None`; no thread invented |
| 44 | Automated regression | **PASS** | 1194 passed / 12 skipped; ruff clean; format clean; `git diff --check` clean |

Real work produced on #19: `models.py` (+`fallback`, +`source`), `config_loader.py`
(+27 lines), `tests/test_config_loader.py` (**+68 lines of tests the agent wrote
itself**), plus readiness/report changes.

---

## 6. Phases NOT yet done

**Highest value first. PR #20 now exists, which unblocks most of these.**

| # | Phase | Note |
|---|---|---|
| 12 | AUTO mode | Create issue with `AUTO` label **before first ingestion**; verify immutability (add/remove label after → mode must not change) |
| 15/16 | Result feedback RELATED / UNRELATED | Same pattern as 13/14 but at `WAITING_FOR_RESULT_APPROVAL` |
| 17 | Unsolicited mid-run steering | Comment during PLANNING/EXECUTING/VALIDATING; expect no interruption + one ack |
| 19 | Multiple revision inputs | Issue #16 already holds 2 queued inputs |
| 20 | Clarification | Needs an issue genuinely lacking info so `request_clarification` fires (it is granted in `config` planning) |
| 21–24 | PR conversation / inline review / submitted review body | **Use PR #20.** `gh pr comment 20`, `gh api` for inline review comments, `gh pr review 20 --comment --body "@agent …"` |
| 26 | Cross-surface routing | |
| 29 | Restart durability at each boundary | Partially proven (Bug #1/#3 work) but not systematically |
| 31/32 | Repo memory / issue-resolution memory | Check `memory.sqlite`, `repo_memory_*`, `issue_resolution_memory` tables |
| 34 | Root vs investigator subagent | |
| 36 | Repo-level serialization | |
| 37 | Cross-repo isolation | **BLOCKED** — no second safe fixture repo. Classify live portion BLOCKED, use existing integration tests |
| 38 | Config change while issue active | |
| 10 | Remote HTTPS MCP | Likely **LOCAL-INTEGRATION** only — needs a disposable HTTPS MCP server; do **not** weaken TLS |
| 43 | Failure/idempotency matrix | Partially observed (re-poll persisted 0 new events) |

---

## 7. Practical tips for whoever resumes

1. `source /tmp/sweforge-live-e2e-20260830T115148Z/campaign-env.sh` first, always.
   Also `export SWEFORGE_SECRET_MASTER_KEY=$(cat $CAMPAIGN_ROOT/.secret-master-key)`.
2. A server may still be running: `pgrep -f "bin/sweforge-serve"`. Kill with
   `pkill -9 -f sweforge-serve` before restarting with new code.
3. `$CAMPAIGN_ROOT/state.sh 19` is the fastest way to see cycle + task phases.
4. `$CAMPAIGN_ROOT/approver.sh <issue>` in the background posts real
   `@agent approve` comments whenever a task waits — this is how the diamond
   was driven. It is a legitimate stand-in for the human approver.
5. `$CAMPAIGN_ROOT/worker.py <thread_id>` runs exactly one worker drain in the
   foreground — much easier to debug than the server loop.
6. To surface a swallowed error, monkeypatch in a throwaway script (never in
   product code): `war._phase_advanced = lambda a, b, e=None: False`.
7. A **new** issue is needed for any phase requiring a clean generation binding —
   existing issues are frozen to their generation and cannot be retrofitted.
8. Model calls are slow (a full task ≈ 10–20 min). Use `until … done` loops
   with `run_in_background: true`, not foreground `sleep`.
9. Bugs found live have all been in the **resume/feedback** and
   **model-output-validation** paths — that is where to keep looking.

---

## 8. Verdict so far

Not final — too many phases remain. Provisional:

**SWEForge V1 LIVE E2E: PASS WITH BLOCKED EXTERNAL COVERAGE** *(provisional)*

- The central invariant held everywhere it was tested: the application chose
  the workflow, task, phase, capabilities and approvals; the model only chose
  within them. Declaration order beat model preference in a real diamond.
- Security boundaries held with zero leaks across 13,888 log lines.
- 4 real bugs were found — all in resume/feedback durability and model-output
  validation, all invisible to the 1184-test suite, all now fixed with tests.
- One robustness finding is deliberately left open for a product decision (§4).
- Cross-repo isolation (37) is BLOCKED for lack of a second safe fixture.

**Do not push. Do not merge. Do not run destructive operations on the fixture.**
