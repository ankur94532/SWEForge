"""A behavioural absence claim is proved by the diff, not by cited lines.

Class A defect from the K7 luna batch, the fifth recurrence of F1. Two
requirements read "Leave all production code files untouched" and "Diff shows
changes only in the test file". The model emitted ABSENCE_OF_CHANGE for every
named file -- exactly the right evidence -- and the guard rejected it demanding
a direct code observation. Nothing can be cited to prove a file did not change.
It accounted for 42 of roughly 53 guard failures in that batch.
"""

from sweforge.reviewer import _negative_change_targets

UNTOUCHED = (
    "Leave all production code files (DiscountPolicy.java, PricingCalculator.java, "
    "Order.java, Item.java, CustomerTier.java) untouched."
)
DIFF_ONLY = "Diff shows changes only in the test file; no production source modified."
POSITIVE = "The discount rate returns 0.10 at or above the bulk threshold."


def test_an_untouched_files_requirement_is_an_absence_claim():
    assert _negative_change_targets(UNTOUCHED) is not None


def test_a_diff_scoped_requirement_is_an_absence_claim():
    assert _negative_change_targets(DIFF_ONLY) is not None


def test_an_ordinary_behavioural_requirement_is_not_an_absence_claim():
    """Positive control: a real behavioural requirement must still demand a
    direct code observation, or the fix would disable the guard entirely."""
    assert _negative_change_targets(POSITIVE) is None


def test_the_behavioural_branch_routes_absence_claims_to_absence_evidence():
    """The guard source must consult the absence detector before demanding a
    code observation; asserting only the detector would not prove the wiring."""
    import inspect

    from sweforge import reviewer

    source = inspect.getsource(reviewer)
    marker = source.index("IA_MISSING_DIRECT_CODE_OBSERVATION")
    window = source[max(0, marker - 1400) : marker]
    assert "_negative_change_targets(requirement_text)" in window, (
        "the behavioural branch demands a code observation without first "
        "checking whether the requirement is an absence claim"
    )
    assert 'elif not any(item.kind == "CODE"' in window, (
        "the code-observation demand is no longer the fallback branch"
    )


OUTCOME_CASES = [
    "Existing tests (a, b, c) still pass unmodified.",
    "Existing tests (a, b, c) continue to pass unchanged.",
    "mvn test (full suite) passes with no regressions",
]


def test_an_outcome_assertion_is_not_routed_to_absence_evidence():
    """A requirement about the suite still passing is proved by the run.

    These trip the negation patterns because "unchanged" and "unmodified"
    describe the subject, not the claim. Routing them to ABSENCE_OF_CHANGE
    demanded the wrong artifact and introduced 26 failures while fixing 42.
    """
    from sweforge.reviewer import _asserts_an_outcome, _negative_change_targets

    for text in OUTCOME_CASES:
        routed = _negative_change_targets(text) is not None and not _asserts_an_outcome(
            text
        )
        assert not routed, f"outcome assertion routed to absence evidence: {text}"


def test_a_pure_absence_claim_is_still_routed_to_absence_evidence():
    """Positive control: excluding outcomes must not disable the absence route."""
    from sweforge.reviewer import _asserts_an_outcome, _negative_change_targets

    for text in (UNTOUCHED, DIFF_ONLY):
        routed = _negative_change_targets(text) is not None and not _asserts_an_outcome(
            text
        )
        assert routed, f"pure absence claim lost its absence route: {text}"
