# Acceptance campaign — final status

## What holds

- **28 scenarios registered**, covering the required 26 plus S27 (review
  infrastructure exhaustion) and S28 (repair-execution bound).
- **All 12 LIVE_GITHUB scenarios exist and pass** against a real repository:
  S1, S2, S4, S8, S9, S10, S11, S15, S16, S18, S19, S20.
- **904 tests** passing, ruff clean.
- **J4 executed.** Two disposable sandboxes; PRIMARY protection verified
  including the case that matters most — a repository in both the allowlist
  and the PRIMARY list is still refused, because PRIMARY is checked first
  and independently.

## Exit conditions: 7 of 8 MET

E1, E2, E3, E4, E6, E7, E8 all MET, from a 28-scenario x 3-repetition
integration campaign in which live bodies ran at LIVE_GITHUB and every one of
the 84 runs passed.

E3 needed a scoping fix, not a behaviour change. It compared every scenario
including those run live, and a live run against a shared repository is not
reproducible by construction: the sandbox accumulates issues, so an isolation
invariant reports "within 6 threads" then "within 8" while the outcome stays
PASS. That made E1 and E3 mutually exclusive, since E1 requires live layers
and E3 forbade anything that varies. E3 now compares only scenarios that
actually ran deterministically, which is what its own description states. A
positive control keeps a varying deterministic scenario failing it.

**E5 is not certified**: it requires conformance metrics for all seven model components,
and only the four reviewer components have data. Planner,
clarification-classifier and both curator tracks would each need their own
runs, which costs provider budget that was not available. No metric was
invented to close it.

## K7 does not certify: first-pass 0.869 against a 0.95 bar

`acceptance/reports/k7-luna-20x8-serial.json`, 20x8 serial on
`openai:gpt-5.6-luna`, 158 effective runs. Seven guard defects were found and
fixed after that batch; replaying its own artifacts under the corrected guards
costs no model calls and measures the improvement:

| Stage | first-pass |
| --- | --- |
| as recorded | 0.665 |
| behavioural absence claims | 0.719 |
| outcome assertions excluded (BEHAVIORAL) | 0.794 |
| outcome assertions excluded (STRUCTURAL) | 0.825 |
| changed files excluded from absence targets | 0.869 |

Bounded-eventual is 0.806 against a required 1.000.

**The remaining gap is model output, not guard defects.** The largest residual
cluster was observations the guard called ungrounded. The inspector had read
the files -- the read ledger proves it -- so grounding was widened to consult
the ledger directly, which reached 0.925. An existing test then caught that
this let an observation about lines 30-40 rest on a read of lines 10-20, which
is exactly the authority violation the guard exists to prevent. Grounding is
now range-aware and the honest figure is 0.869.

What remains is an off-by-one: the model cites lines 1-8 having read lines
2-11. It saw the substance and annotated the range one line wider. Closing
that last 13% requires letting an inspector assert facts about lines it did
not read, which is not a trade worth making to pass a gate.

**Recorded as uncertified.** Reaching 0.95 needs either better model output or
a decision that 0.95 first-pass is the wrong bar. The second is a contract
question and is deliberately left open rather than settled by loosening a
guard.

## The finding that matters most for production

first-pass measures whether guards accept the artifact's *form*.
bounded-eventual measures something different: whether the run produced the
expected *verdict*. They are separate axes and should not be read as one.

On RF-14-41504759, a STABLE fixture whose expected verdict is NEEDS_FIXES,
the reviewer as recorded returned:

    ACCEPT 11, NEEDS_FIXES 8, no result 1

A reviewer that approves work needing fixes is far more dangerous than one
that rejects valid evidence, and this was invisible while attention was on
the first-pass rate.

The seven guard fixes improved it substantially. Replaying the same artifacts
under the corrected guards:

    ACCEPT 6, NEEDS_FIXES 8, BLOCKED 5, no result 1
    guard vetoes: 0 before, 5 after

Five false accepts became guard vetoes, and the guards went from doing no
protective work on this fixture to catching five bad approvals. False accepts
fell from 11 to 6.

**Six remain.** That residual 30% false-accept rate is the highest-value open
issue in the campaign -- higher than first-pass, higher than E5 -- because it
is a correctness failure rather than a form one.

## Not done, and why

- **E5** — needs provider budget for three uncovered components.
- **sol cross-check** — gated on luna certifying, which it does not.
- **S29-S47** — 19 proposed gap scenarios, never part of the original 26.
  Explicitly out of scope rather than forgotten.
- **J5 / PRIMARY acceptance set** — runs only after the exit condition holds.
  It does not hold.
