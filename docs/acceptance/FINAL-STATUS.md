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

## K7 does not certify

`acceptance/reports/k7-luna-20x8-serial.json`, 20x8 serial on
`openai:gpt-5.6-luna`, 158 effective runs:

| Metric | Observed | Required |
| --- | --- | --- |
| first-pass | 0.665, then 0.719 after the behavioural-absence fix | >= 0.95 |
| bounded-eventual | 0.787 | 1.000 |
| Class A | **1 confirmed** | 0 |

The Class A count is the decisive one. K7 defines Class A as zero-tolerance,
so K7 cannot pass while that stands, regardless of the rate. More batches
would not change that, since the shortfall is a guard defect and a rate, not
a sampling accident.

The transport retry did fix sample validity: operational failures fell from
25-79% to 1.25%, so these numbers are trustworthy rather than noise.

## The recurring defect

Six investigations of guard failures found **five guard defects**, not model
defects. Each of the five is the same shape: a guard demanding evidence in a form
the judged stage had no route to produce.

1. No evidence kind expressed absence at all.
2. The finalizer could not produce a bound `source_id`.
3. Coverage required a directory and all its children simultaneously.
4. A child scope could not prefix-match its parent directory, so naming all
   five files under `src/main/java` was refused.
5. BEHAVIORAL demanded a cited line range for "leave these files untouched".
   Nothing can be cited to prove a file did not change. This one alone was
   42 of roughly 53 guard failures in the K7 batch.
A sixth was investigated and turned out **not** to be a guard defect.
`IA-UNKNOWN-EXECUTION-SOURCE` looked like one: the cited execution id is in
the fixture's trusted evidence. It was raised at the FINALIZATION stage, not
INSPECTION, and the finalizer had cited
`exec-evidence-bebb9cc...ba8d}]},{'` -- the real id with JSON fragments
appended by malformed model output. The guard was right; the first reading
was wrong because it examined the inspection artifact rather than the stage
that raised the problem. Classified B.

That five-in-six rate is still the finding. The guards were tuned against one model's
output shape and reject other correct shapes, which is why a cross-model
check was worth having and why "the model is wrong" was never assumed.

## Not done, and why

- **E5** — needs provider budget for three uncovered components.
- **sol cross-check** — gated on luna certifying, which it does not.
- **S29-S47** — 19 proposed gap scenarios, never part of the original 26.
  Explicitly out of scope rather than forgotten.
- **J5 / PRIMARY acceptance set** — runs only after the exit condition holds.
  It does not hold.
