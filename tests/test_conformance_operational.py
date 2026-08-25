"""Infrastructure faults must never be reported as reviewer conformance results."""

import pytest

from acceptance.runner.conformance import evaluate_runs, is_operational_error


@pytest.mark.parametrize(
    "message",
    [
        "Missing credentials. Please pass an `api_key`",
        "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error'}}",
        "Connection error.",
        "Request timed out",
    ],
)
def test_operational_errors_are_recognised(message):
    assert is_operational_error(RuntimeError(message))


def test_guard_failures_are_not_operational():
    assert not is_operational_error(ValueError("missing direct code observation"))
    assert not is_operational_error(None)


def _runner(exc):
    def run_once(_observer):
        raise exc

    return run_once


def test_all_operational_reports_incomplete_never_fail():
    report = evaluate_runs(
        fixture_id="RF-13-7fda18a4",
        model="test",
        runs=3,
        expected_verdict=None,
        classifications={},
        run_once=_runner(RuntimeError("Missing credentials. Please pass an `api_key`")),
    )
    assert report["verdict"].startswith("INCOMPLETE")
    assert "FAIL" not in report["verdict"]
    assert report["operational"] == {
        "runs": 3,
        "effective_runs": 0,
        "requested_runs": 3,
    }
    # A credentials outage must not masquerade as a threshold breach.
    assert "first-pass" not in report["verdict"]


def test_finalization_error_marked_operational_is_operational():
    """SWEForge's own wording for a provider/structured-output failure."""
    from sweforge.reviewer import ReviewFinalizationError

    error = ReviewFinalizationError(
        "execution review finalization failed operationally",
        diagnostic={"guard_codes": []},
    )
    assert is_operational_error(error)


def test_empty_guard_codes_alone_is_not_operational():
    """ReviewFinalizationError sets guard_codes=[] even with no diagnostic."""
    from sweforge.reviewer import ReviewFinalizationError

    error = ReviewFinalizationError("scripted rejection")
    assert error.diagnostic == {"guard_codes": []}
    assert not is_operational_error(error)


def test_finalization_error_with_guard_codes_is_a_real_result():
    from sweforge.reviewer import ReviewFinalizationError

    error = ReviewFinalizationError(
        "inspector returned an invalid inspection artifact",
        diagnostic={"guard_codes": ["IA-MISSING-ABSENCE-OF-CHANGE"]},
    )
    assert not is_operational_error(error)
