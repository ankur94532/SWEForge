"""An out-of-scope condition must never be able to hide a failure.

E5 is excluded by decision, and the risk of any such mechanism is that it
becomes a way to silence a condition that would otherwise fail. It cannot:
a condition reports OUT_OF_SCOPE only while its evidence is absent, so
supplying evidence puts it back under evaluation.
"""

from acceptance.runner.exit_conditions import ExitState, evaluate_exit_conditions


def _e5(status):
    report = evaluate_exit_conditions(status)
    return next(c for c in report["conditions"] if c["condition_id"] == "E5")


def test_absent_evidence_is_out_of_scope_not_cannot_evaluate():
    """The distinction that matters: nobody intends to gather this."""
    assert _e5({})["state"] == ExitState.OUT_OF_SCOPE


def test_the_exclusion_states_its_reasoning():
    detail = _e5({})["detail"]
    assert "excluded by decision" in detail
    assert "re-enables this check" in detail, "the exclusion must say how to undo it"


def test_supplying_evidence_puts_the_condition_back_under_evaluation():
    """The safety property. Incomplete evidence must NOT stay out of scope."""
    state = _e5({"model_components": {"planner": {}}})["state"]
    assert state != ExitState.OUT_OF_SCOPE, (
        "supplying evidence left the condition excluded, so the exclusion "
        "could mask a real failure"
    )


def test_readiness_reports_which_conditions_were_excluded():
    report = evaluate_exit_conditions({})
    assert report["out_of_scope"] == ["E5"], (
        "an excluded condition must be visible in the report, not merely "
        "counted toward readiness"
    )


def test_a_failing_condition_still_blocks_readiness():
    """Positive control: exclusion must not make everything ready."""
    report = evaluate_exit_conditions({})
    assert report["ready"] is False, (
        "an empty status became ready; exclusion is masking real failures"
    )
