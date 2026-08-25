"""Operational review failures must carry their cause.

Three conformance runs failed with diagnostic={"guard_codes": []}, which named
no cause at all, so a batch could not tell a provider timeout from a bug.
"""

from sweforge.reviewer import (
    MAX_OPERATIONAL_MESSAGE_CHARS,
    ReviewFinalizationError,
    _operational_failure_diagnostic,
)


def _raise_chain() -> BaseException:
    try:
        try:
            raise TimeoutError("model request exceeded 120s")
        except TimeoutError as inner:
            raise RuntimeError("tool strategy aborted") from inner
    except RuntimeError as outer:
        return outer


def test_diagnostic_records_the_immediate_cause():
    diagnostic = _operational_failure_diagnostic(_raise_chain())
    assert diagnostic["cause"], "operational failure named no cause"
    assert diagnostic["cause"][0]["type"] == "RuntimeError"
    assert "tool strategy aborted" in diagnostic["cause"][0]["message"]


def test_diagnostic_walks_the_full_chain_to_the_root():
    diagnostic = _operational_failure_diagnostic(_raise_chain())
    types = [item["type"] for item in diagnostic["cause"]]
    assert types == ["RuntimeError", "TimeoutError"], types


def test_diagnostic_bounds_the_message():
    huge = _operational_failure_diagnostic(ValueError("x" * 5000))
    assert len(huge["cause"][0]["message"]) == MAX_OPERATIONAL_MESSAGE_CHARS


def test_diagnostic_terminates_on_a_self_referential_chain():
    """A cycle must not hang the error path that reports the error."""
    first = ValueError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first
    diagnostic = _operational_failure_diagnostic(first)
    assert len(diagnostic["cause"]) == 2


def test_error_still_reports_guard_codes_for_consumers():
    """Callers read diagnostic["guard_codes"]; it must stay present."""
    error = ReviewFinalizationError(
        "execution review finalization failed operationally",
        diagnostic=_operational_failure_diagnostic(_raise_chain()),
    )
    assert error.diagnostic["guard_codes"] == []
    assert error.diagnostic["cause"][0]["type"] == "RuntimeError"
    assert "TimeoutError" in str(error), "cause is absent from the message"
