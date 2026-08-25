# Handoff — 2026-08-25 late

## State

HEAD `433fcbb`. **870 tests passing**, ruff clean, 28 L1 scenarios registered
(S1–S28), zero vacuous invariants anywhere in the campaign.

## Campaign exit conditions: 3 of 8 MET

| | State | Note |
| --- | --- | --- |
| E1 | CANNOT_EVALUATE | needs the 12 LIVE-GITHUB runs |
| E2 | CANNOT_EVALUATE | needs the 12 LIVE-GITHUB runs |
| E3 | **MET** | 3 identical runs over all 28 scenarios |
| E4 | **MET** | all four bounded paths observed at their exact bound |
| E5 | CANNOT_EVALUATE | 7 model components; needs provider calls |
| E6 | **MET** | contamination detector, 26 scenarios, zero violations |
| E7 | CANNOT_EVALUATE | needs the 12 LIVE-GITHUB runs |
| E8 | CANNOT_EVALUATE | PRIMARY audit log; only exists once they run |

## The real blocker on E1/E2/E7/E8

**No `LIVE_GITHUB` scenario body exists.** All 28 registered scenarios are
`L1`/`L1_PROCESS`. The layer is in the enum and the runner plumbing is ready
(`check_live_target` fires on `Layer.LIVE_GITHUB`, `--repo` is wired), but the
12 bodies are unwritten. This was previously described as "blocked on J4",
which was wrong: J4 is the *second* link.

The 12 are **S1, S2, S4, S8, S9, S10, S11, S15, S16, S18, S19, S20**. Several
adapt from existing L1 bodies (S8, S9, S16). Writing them needs no GitHub, no
models, and no proxy — it is safe alongside a running batch.

Per §J of ROADMAP.md the environment axis is independent of the model axis, so
a LIVE-GITHUB run can use scripted models and need not contend for the proxy.
Confirm per scenario before running.

## Pending user decision

The user will send a **go** command with a target repo. Suggested name
`<user>/sweforge-acceptance-sandbox`, private, one initial commit.

At run time set, and verify, both:

    export SWEFORGE_ACCEPTANCE_REPOS="<user>/sweforge-acceptance-sandbox"
    export SWEFORGE_PRIMARY_REPOS="<user>/<real repo>"

`PRIMARY_REPOS` in code is currently **empty**, so protection rests entirely on
the allowlist being right. PRIMARY is checked first and independently: a repo
in both lists is still refused.

## In flight right now

- **K7 conformance batch**, pid 69858, ~1h into a ~7h serial run, writing
  `acceptance/reports/k7-luna-20x8-serial.json` at the end. Not restartable.
  Do not kill it, do not start another batch, do not restart the cliproxy.
- Battery **66%** and falling on battery power; the run is longer than the
  remaining charge. Plugging in is needed before it finishes.

## Rules that cost real time to learn

- Conformance batches are **serial only**. 8-way killed the proxy: 153
  connection errors, 75 minutes wasted, uncertifiable sample.
- Never kill a batch on low CPU, idle sockets, or an empty log. Python
  block-buffers stdout; one run is 15+ model calls.
- `recover_orphaned_repair_attempt` retires the attempt and moves the thread to
  `REPAIR_READY`; the counter advances only when the same attempt is *resumed*.
- Absence assertions ("no memory was written") are substantive at zero.
  Universal ones ("every accepted candidate cites lines") are vacuous at zero.
  Pick the invariant that matches the claim.
- Approval requires repository write access. Test doubles need
  `collaborator_permission`.
- Store rows are `sqlite3.Row`: no `.get()`.
