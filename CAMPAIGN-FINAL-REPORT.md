# SWEForge V1 Final Comprehensive Live E2E Acceptance Report

Campaign completed 2026-08-30. During the campaign, no SWEForge commit was
pushed and no pull request was merged. The user subsequently authorized a
post-campaign commit and push of all local changes to `main`.

## A. Environment

| Item | Value |
|---|---|
| SWEForge SHA | `1939e132a33abcfe1889bb3728ac948d566127b4` |
| `origin/main` SHA at campaign completion | `7aced32862def6c975da1cfc7e95a6e32c98c799` |
| Fixture | `ankur94532/SWEForge-Diamond-Test` |
| Primary isolated root | `/tmp/sweforge-live-e2e-20260830T115148Z/` |
| Clean continuation root | `/tmp/sweforge-live-e2e-20260830T150000Z/` |
| Continuation state | Separate `state.db`, `checkpoints.sqlite`, `memory.sqlite`, `workspaces/`, `locks/`, `logs/`, and `0600` secret key |
| Sandbox | Real `acceptance-seatbelt` provider; no `--unsafe-local-shell` |
| CLI proxy | Local `cliproxyapi`, PID observed as 15987 |
| Endpoint/transport | OpenAI-compatible HTTP at `http://127.0.0.1:8317/v1` |
| Model | `openai:gpt-5.6-sol` for planning, execution, review, clarification, memory, and resolution |
| Direct provider API key used | **NO**; only the proxy's local syntactic token was supplied |

Working note required by the campaign:

```text
CLI PROXY FOUND: YES
proxy mechanism: local cliproxyapi process
endpoint/transport: http://127.0.0.1:8317/v1, OpenAI-compatible HTTP
model identifier: openai:gpt-5.6-sol
direct provider API key required: NO
```

Repository startup verification found the expected upstream history. At the
campaign checkpoint, the SWEForge tree was six local bug-fix commits ahead of
`origin/main`. The unrelated, pre-existing reference-bundle edits remained
uncommitted and were never staged by the campaign; they were committed only in
the later user-authorized publication step.

## B. Feature summary

Every prompted phase is classified below. “Live” means the actual CLI/server,
SQLite state, Git/worktree, sandbox, proxy, and/or GitHub path was exercised;
pytest-only evidence is never called live.

| Feature | Scenario | Result | Evidence | Notes |
|---|---:|---|---|---|
| Repository onboarding | 1 | LIVE PASS | Real `repo init`, invalid `validate`, `configure`, `show`; generation/digest inspected | Init installed no authority; show exposed metadata only |
| Immutable generation binding | 2 | LIVE PASS | Issues #16/#19 and #23/#25/#26 retained their generation across processes | No current-pointer recomputation |
| Incremental configuration CLI | 3 | LIVE PASS | Generations 1–8; add/replace/remove/set successes and failure atomicity | Source bundle removal also proved installed materialization |
| Skill injection/discovery/confinement | 4 | LIVE PASS | `SKILL CATALOG`, on-demand `SKILL READ`, negative path cases plus integration regression | No body appeared in safe trace |
| Phase-owned tool authority | 5 | LIVE PASS | Planning/validation mutation denial and execution allow-list observed | Lifecycle gateways remained application-owned |
| Registered script tools | 6 | LIVE PASS | Fixed entrypoint, JSON args, worktree, bounds, timeout/effect policy exercised | Model could not select entrypoint/env/timeout |
| Repository secrets CLI/store | 7 | LIVE PASS | Set/list/check/delete; raw DB scan; no plaintext show | Fake plaintext absent from state DB |
| Script secret injection | 8 | LIVE PASS | Exact wrapper injection, generic/unrelated isolation, stdout/stderr redaction, rotation/delete/re-add | Generation digest unchanged on value rotation |
| Local stdio MCP | 9 | LIVE PASS | Real `release_catalog` server; `required=1 resolved=1`; missing credential failed closed | Session/tool authority was repo-bound |
| Remote HTTPS MCP auth | 10 | LOCAL-INTEGRATION PASS | `test_repo_secret_injection.py` exercises adapter construction, HTTPS, secret headers, rotation, pre-connect failure, `follow_redirects=False` | No disposable trusted-TLS HTTPS service was available; TLS was not weakened |
| Manual diamond workflow | 11 | LIVE PASS | Issue #19 and issue #23: `model → config → readiness → reporting` with exact approvals | Waiting retained active ownership |
| AUTO mode and immutable mode | 12 | LIVE PASS | Issue #21/PR #22; label removed after ingestion; full workflow auto-authorized; issue #23 stayed MANUAL after AUTO was added | Plans/results/validation/publication were not skipped |
| Related plan feedback | 13 | LIVE PASS | Issue #19 review `REPLAN`; old plan superseded, new occurrence approved | Same root/task owner |
| Unrelated plan feedback | 14 | LIVE PASS | Issue #19 `DEFERRED_WAITING`; deterministic pushback; retained occurrence | Queued once for revision |
| Related result feedback | 15 | LIVE PASS | Issue #19 result review status `REPLAN` | Same task/worktree; validation reran and exact new result was required |
| Unrelated result feedback | 16 | LIVE PASS | Issue #19 exposed bug; issue #23 clean rerun retained exact result/validation and deferred one input | Fixed by `1939e13` |
| Mid-run steering | 17 | LIVE PASS | Inputs during live agent phases queued with deterministic one-time acknowledgement | No phase/task interruption |
| Generic revision | 18 | LIVE PASS | Issue #19 cycle 2 and issue #23 cycle 2 each had one logical `revision` task | Same thread/branch/worktree/PR |
| Multiple revision inputs | 19 | LIVE PASS | PR conversation, inline review, and submitted review were batched in durable event order on issue #19 | No double consumption; later input stayed for next safe cycle |
| Clarification/restart | 20 | LIVE PASS | Issue #23 entered `WAITING_FOR_INPUT`; restart retained one occurrence; arbitrary answer resumed the same task from PLANNING | Approval was not accepted as an answer |
| All GitHub input surfaces | 21 | LIVE PASS | Issue body/comment and PR #20 conversation/inline/submitted review records | Leading-mention actionability enforced |
| PR conversation routing | 22 | LIVE PASS | Source comment `5469034478` routed PR #20 → issue #19 | Revision reused PR mapping |
| Inline review provenance | 23 | LIVE PASS | Review comment `3889522830`; path, line 12, RIGHT side, hunk, and `c7c30590` anchors persisted | Immutable context retained |
| Submitted review body | 24 | LIVE PASS | Review `5060965604`, `PR_REVIEW`, state `COMMENTED`, routed to issue #19 | Review state alone granted no SWEForge authority |
| Exact approval security | 25 | LIVE PASS | Exact approval worked; suffix/prefix/approved/stale/wrong-surface cases did not approve | Stale exact input did not become revision work |
| Cross-surface policy | 26 | LIVE PASS | PR feedback while issue artifact was canonical did not mutate the approval occurrence | Own-surface acknowledgement/pushback used |
| Base/worktree freezing | 27 | LIVE PASS | Issue #25 stayed at `46842d9`; main advanced to `3179820`; issue #26 bound `3179820` | Old worktree lacked marker; new worktree contained it |
| Same branch/worktree/PR | 28 | LIVE PASS | Issue #19/PR #20 and issue #23/PR #24 | No PR per task or revision |
| Restart durability | 29 | LIVE PASS | Plan, result, clarification, deferred input, active revision, and pre-publication boundaries restarted | No duplicate artifact/ack/PR; EXECUTING crash variants additionally covered by integration tests |
| Publication | 30 | LIVE PASS | Issue #23 initial+revision material published once as commit `5bb712f`, PR #24 | One marker, one commit, one PR; both cycles `PUBLISHED` |
| Repository memory | 31 | LOCAL-INTEGRATION PASS | Live curation ran through proxy and selected `NO_UPDATE`; live revision invoked search; candidate acceptance/isolation covered by memory tests | A useful accepted live candidate was not produced, so that subpath is not promoted to live |
| Issue-resolution memory | 32 | LIVE PASS | Issue #23 resolution record completed from 5 accepted lifecycle records and 1 consumed revision input | Changed files, validation, PR, commit, limitations preserved; superseded noise absent |
| Progressive skills in revision | 33 | LIVE PASS | Issue #23 revision catalog count 5; reporting/fixture skills read on demand | Model did not eagerly read all skills |
| Root versus investigator | 34 | LIVE PASS | Issue #23 validation spawned a bounded investigator using read/research operations only | No mutation, lifecycle, approval, recursion, or secret enumeration |
| Validation/failure branches | 35 | LIVE PASS | Real fixture tests/verifiers and ACCEPT; issue #18 real FAILED branch | NEEDS_FIXES/REPLAN/BLOCKED repair mechanics also have focused integration coverage |
| Same-repo serialization | 36 | LOCAL-INTEGRATION PASS | `test_concurrency_locking.py` and execution concurrency tests exercise repo/thread lock ordering and shared Git serialization | Exact live overlap at EXECUTING was not forced |
| Cross-repo live isolation | 37 | BLOCKED | No second safe GitHub fixture existed | No unrelated/production repository was used |
| Cross-repo local isolation | 37 | LOCAL-INTEGRATION PASS | Cross-repo config/skill/tool/MCP/secret/memory and lock tests passed | Distinct repo IDs remain isolated |
| Config changes during active issue | 38 | LIVE PASS | Issue #16 remained generation 1 while workflow/skill/tool/MCP commands advanced through later generations; later issues bound later generations | Frozen files and digests inspected from installed generations |
| Secret value versus reference | 39 | LIVE PASS | Value rotation/delete/re-add affected same issue without digest change; reference change created a new generation | Old/new issues kept their own reference sets |
| Trace/security audit | 40 | LIVE PASS | 13,888-line original trace scan plus continuation DB scan | No raw repo/proxy/GitHub/MCP secret, private key, auth header, hidden reasoning, env, or eager skill body |
| Noise/actionability | 41 | LIVE PASS | Non-leading issue/PR/inline/review messages created no workflow action | Non-actionable inputs were not persisted as commands |
| Unmapped PR negative | 42 | LIVE PASS | Actionable input on PR #13 retained `thread_id=NULL` | No IssueThread invented |
| Failure/idempotency/recovery | 43 | LOCAL-INTEGRATION PASS | Poll replays were live-idempotent; outbox markers, duplicate deliveries, post-before-commit recovery, and retry paths passed focused/full tests | Not every injected crash point was forced against GitHub |
| Full regression/static checks | 44 | LOCAL-INTEGRATION PASS | `1195 passed, 12 skipped`; Ruff, formatter, and `git diff --check` clean | Test count increased by focused regressions |
| GitHub App authentication | Added inventory item | LIVE PASS | Real issue/comment/review/branch/PR API activity used existing App/`gh` setup | Credentials never entered model context |
| Dispatcher failure budget | Added inventory item | LIVE PASS | Issue #18 reached deterministic FAILED after retry exhaustion | Failure was surfaced rather than silently looped after `7fcd15f` |
| No-change learning/publication | Added inventory item | LIVE PASS | Repository curator returned durable `NO_UPDATE` | It did not invent a memory candidate |
| Canonical review evidence/guards | Added inventory item | LOCAL-INTEGRATION PASS | Full review/conformance suites passed | Malformed claims cannot bypass deterministic ACCEPT guards |

## C. Repo onboarding/config

- `repo init` created `workflow.yaml`, `skills/`, `tools/scripts/`, and
  `tools/mcp/` but no installed authority.
- Invalid workflow, skill, script metadata, MCP config, duplicate add, and
  referenced removal all failed before pointer movement.
- `repo configure` installed a complete immutable generation and emitted a
  deterministic digest. `repo show` returned names/digests/status only.
- Add/replace/remove/set commands created complete immutable generations;
  previous generations were byte-stable and failures were atomic.
- After the original bundle was removed, a focused MCP mutation materialized
  from installed generation files, proving there was no source-directory
  dependency.

## D. Workflow/runtime

The real diamond was application-selected in declaration order:

```text
model → config → readiness → reporting
```

The model never selected the next task. Dependencies, active owner, phase,
permits, validation, and publication were controlled by the workflow runtime.
Issue #21 proved AUTO still creates and validates exact plan/result artifacts;
issues #19/#23 proved MANUAL pauses and exact human authorization.

The real sandbox provider ran. Its registered Python verifier sometimes failed
before assertions because the seatbelt environment could not load a Python
dynamic library. Independent fixture verification from the persistent worktree
passed all stages, and the earlier registered-script scenario proved the wrapper
path. This environment limitation is reported rather than treated as validation
evidence.

## E. Human interaction

- Exact current-occurrence `@agent approve` authorized only the plan/result it
  matched.
- Related feedback replanned the current owner cumulatively.
- Unrelated feedback retained the exact artifact, queued one revision input,
  and used application-authored deterministic pushback.
- Issue #23 clarification survived restart without duplicate posting and resumed
  the same interrupted logical phase after its answer.
- Mid-call steering did not interrupt the model; it generated one durable inbox
  item and one stable acknowledgement.
- Stale, malformed, wrong-thread, wrong-PR, and wrong-surface approvals did not
  grant authority.

## F. Revision

Revision was a single logical task, not a replay of the diamond. Context included
the root issue, accepted initial lifecycle, current ordered inputs and provenance,
and prior accepted material. Issue #23 reused its worktree and branch and published
one PR only after initial plus revision completion. The clean rerun after
`1939e13` proved unrelated result feedback waits for the next revision while the
current validated result remains approvable.

## G. GitHub surfaces

All supported surfaces were exercised against GitHub: issue root, issue comment,
PR conversation, inline review, and submitted review. Inline evidence stored
path/range/side/hunk/commit anchors. Submitted review state was provenance only.
PR #13 proved an unmapped actionable comment cannot invent routing.

## H. Skills/tools

Single-skill eager injection and multi-skill progressive discovery were both
covered. Multi-skill traces exposed only metadata/canonical paths until a read.
Inactive/cross-skill traversal, separators, encoding, and noncanonical aliases
failed closed. Built-ins were shared implementations with phase-specific
authority. Registered scripts had fixed trusted entrypoints, schema-bound input,
bounded output/time, controlled effects and minimal environments. The live
investigator remained read-only while the root retained lifecycle ownership.

## I. Secrets/MCP

The fake test credential was encrypted with a campaign-only Fernet key. List,
check, show metadata, delete, rotation, and re-add behavior were exercised. A
final raw-byte scan reported `secret_plaintext_in_state_db=False`.

Registered script output redacted the fake value from stdout and stderr; generic
execute and unrelated tools did not inherit it. The real stdio MCP resolved only
its configured repo secret. Remote HTTP MCP is integration-only: the production
adapter requires HTTPS, resolves fixed secret references before connect, does not
expose headers to the model, refreshes on a new client, and disables redirects.
No TLS verification was weakened.

## J. Durability

Durable checks covered plan/result/input waits, interrupted feedback review,
revision, publication, worktree/branch identity, input consumption, outbox
markers, and PR uniqueness. The mainline advance from `46842d9` to `3179820`
left issue #25 frozen while issue #26 received the new base. Configuration
generation remained independent of both base movement and secret value rotation.

## K. Memory/publication

Issue #23 produced one publication, commit `5bb712f952fa2c037df0975513c54df7089715b2`,
and PR #24 containing the four initial task changes plus the accepted revision.
Issue-resolution memory contains accepted facts, all changed files, validation
limitations, the consumed revision provenance, commit and PR. Repo-memory
curation ran through the proxy and correctly returned `NO_UPDATE`; accepted
candidate storage/validation/isolation remains integration-level coverage.

## L. Cross-repo isolation

The live portion is **BLOCKED** because no second safe GitHub fixture was
available. Existing integration tests passed for separate workflow/config
generations, skill catalogs and reads, scripts, MCP clients, secrets, memory,
and per-repository locks. No production or unrelated repository was touched.

## M. Bugs found

All fixes were clean local commits and were not pushed during the campaign. They
were included only in the later user-authorized publication step.

1. **Interrupted feedback review not runnable — `8c2f1d2`.** A persisted
   `REVIEWING` item could never wake because runnability considered only
   unconsumed input. The selector now recognizes durable in-flight review work.
   Focused PLAN/RESULT restart regressions pass; live feedback resumed.
2. **Replayed feedback gateway unauthorized — `4885e43`.** LangGraph replayed
   the phase gateway during semantic review, but REVIEWING authority omitted it.
   The exact phase gateway is now allowed while all other review access remains
   read-only. Focused middleware tests and live plan/result reviews pass.
3. **Resume errors silently swallowed — `7fcd15f`.** Synthetic replay phase was
   compared to itself rather than the durable entry phase, causing an endless
   redispatch loop. Durable advancement is now required before tolerating a
   post-gateway exception. Focused positive/negative tests pass.
4. **Validation verdict unconstrained — `8b4e5a8`.** The model saw a free-form
   string and returned `pass`, which crashed enum coercion and exhausted retries.
   The tool schema now exposes the legal enum. Focused schema regression passes;
   later live validation completed.
5. **Inline response recovery called the wrong client API — `5d5d43d`.** Both
   immediate reconciliation and outbox recovery called `review_comments` rather
   than `review_comments_for_pull_request`. Both paths now use the implemented PR
   review-comment API; focused surface-routing regression and real inline input
   pass.
6. **Unrelated result feedback corrupted the active revision — `1939e13`.** The
   steering settler batched every pending input, including
   `DEFERRED_RESULT_FEEDBACK`, into the currently waiting revision and destroyed
   its retained result. It now batches only selected `UNSOLICITED_STEERING` IDs;
   deferred result feedback stays pending for the next cycle. The 95-line focused
   regression passes, and issue #23 live re-verification retained/approved the
   original result before starting one new revision.

Historical issue #19 cycle 3 was already corrupted before fix 6 and was not
manually rewritten. Clean issue #23 is the authoritative post-fix live rerun.

Open robustness finding: an unauthorized/invalid model tool call is denied, but
because tool errors are configured as hard dispatcher errors, repeated model
mistakes can exhaust the retry budget and fail a cycle rather than returning a
model-visible correction. The security boundary holds; this remains an explicit
availability-policy decision, not a credential or authority bypass.

## N. Automated verification

| Check | Result |
|---|---|
| Focused regressions for six bugs | PASS |
| Fixture `python3 -m unittest discover -s tests` | 13 passed |
| Fixture `verify.py baseline/model/loader/readiness/report` | PASS |
| Full `uv run pytest -n auto` | **1195 passed, 12 skipped** |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | 218 files already formatted |
| `git diff --check` | PASS |

The pytest run emitted known warnings for an unregistered `live` marker and an
upstream Pydantic forward-reference warning; neither was a test failure.

## O. Remaining blockers / untested items

- No second safe GitHub fixture existed, so cross-repo external isolation is
  BLOCKED; the complete local integration matrix passed.
- No disposable HTTPS MCP service with a trusted certificate was available, so
  remote MCP authentication is LOCAL-INTEGRATION PASS only. TLS was not disabled.
- Same-repository concurrent mutation at the exact EXECUTING instant was not
  forced live; lock ordering/serialization is LOCAL-INTEGRATION PASS.
- A live accepted repository-memory candidate was not produced; the real curator
  chose `NO_UPDATE`. Accepted-candidate validation and isolation are integration
  proven.
- Not every synthetic crash point in the idempotency matrix was injected into the
  real GitHub run; stable markers and re-poll/restart behavior were live, with the
  remaining paths integration-proven.
- Two harmless manual fixture issues (#25 and #26) intentionally remain waiting
  for plan approval; they were never authorized and produced no mutation branch
  or PR.
- The availability-policy finding for invalid/unauthorized model tool calls is
  unresolved but does not weaken the authorization boundary.

## Final verdict

SWEForge V1 LIVE E2E: PASS WITH BLOCKED EXTERNAL COVERAGE

- Core onboarding, manual/AUTO workflow, exact approval, feedback, revision,
  clarification, GitHub input, secret, stdio MCP, validation, worktree, memory,
  and publication paths worked against real components.
- Six live-discovered product defects were fixed in isolated local commits, each
  with a focused regression and post-fix live verification where applicable.
- Application code, not the model, retained ownership of workflow, task, phase,
  permits, approvals, validation, publication, configuration, secrets, and
  revision lifecycle throughout.
- Remaining gaps are clearly limited to unavailable/safely-unforced external
  coverage and are not represented as live passes.
