"""A check that ranged over nothing must not read as evidence.

The campaign reported `INV-NO-FALSE-MEMORY PASS 0 accepted candidate(s), each
citing repository lines`. A universal claim over an empty set is trivially
true, so that PASS asserted nothing while looking like proof.
"""

from harness.invariants import InvariantResult, _ok
from harness.scenario import CheckOutcome


def test_a_universal_check_over_an_empty_set_is_vacuous():
    result = _ok("0 accepted candidate(s), each citing repository lines", observed=0)
    assert result.ok, "a vacuous check still holds"
    assert not result.substantive
    assert result.status == "VACUOUS"


def test_the_same_check_over_a_real_set_is_substantive():
    result = _ok("3 accepted candidate(s), each citing repository lines", observed=3)
    assert result.substantive
    assert result.status == "PASS"


def test_an_absence_assertion_is_substantive_at_zero():
    """ "no permit was created" claims zero; zero is the evidence, not its
    absence, so these sites pass no observed= count."""
    result = _ok("no permit row exists")
    assert result.substantive
    assert result.status == "PASS"


def test_a_failure_is_never_reported_as_vacuous():
    result = InvariantResult(False, "accepted memory without cited lines")
    assert result.status == "FAIL"


def test_vacuity_reaches_the_scenario_report_line():
    line = CheckOutcome("INV-NO-FALSE-MEMORY", True, "0 candidate(s)", False).line()
    assert "VACUOUS" in line
    assert "PASS" not in line, "a vacuous check must not render as PASS"


def test_a_substantive_check_still_renders_as_pass():
    line = CheckOutcome("INV-ATTEMPT-TERMINAL", True, "1 attempt(s)", True).line()
    assert "PASS" in line and "VACUOUS" not in line
