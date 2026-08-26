"""E3 measures deterministic reproducibility, so it compares only what ran
deterministically.

A scenario with a deterministic body may still be run at LIVE_GITHUB in an
integration campaign. A live run against a shared repository is not
reproducible by construction: the sandbox accumulates issues, so an isolation
invariant reports "within 6 threads" then "within 8" while the outcome stays
PASS. Comparing those made E1 and E3 mutually exclusive, since E1 requires
live layers and E3 forbade anything that varies.
"""

from acceptance.runner.exit_conditions import evaluate_exit_conditions


def _record(scenario_id, layer, ok=True):
    return {
        "id": scenario_id,
        "layer": layer,
        "ok": ok,
        "error": None,
        "harness_retries": 0,
        "outcome": "PASS",
        "checks": [{"invariant": "INV-ONE-ROOT", "ok": True, "status": "PASS"}],
    }


def _status(*, live_identical, deterministic_identical):
    """E3 requires the full registry snapshot, so every scenario appears.

    S1 ran live and S3 deterministically; the rest are quiet passes.
    """
    from acceptance.runner.exit_conditions import SCENARIO_SET

    ids = sorted(SCENARIO_SET)
    records = [_record(item, "LIVE_GITHUB" if item == "S1" else "L1") for item in ids]
    reproducibility = {item: {"identical": True, "runs": 3} for item in ids}
    reproducibility["S1"] = {"identical": live_identical, "runs": 3}
    reproducibility["S3"] = {"identical": deterministic_identical, "runs": 3}
    return {
        "scenarios": records,
        "deterministic_scenarios": ids,
        "repetitions": [
            {"index": i, "status": {"scenarios": records}} for i in (1, 2, 3)
        ],
        "reproducibility": {"scenarios": reproducibility},
    }


def _e3(status):
    report = evaluate_exit_conditions(status)
    return next(c for c in report["conditions"] if c["condition_id"] == "E3")


def test_a_varying_live_run_does_not_break_reproducibility():
    """The case that blocked the campaign: S1 ran live and varied."""
    assert (
        _e3(_status(live_identical=False, deterministic_identical=True))["state"]
        != "UNMET"
    )


def test_a_varying_deterministic_run_still_breaks_it():
    """Positive control: scoping must not disable the check. A deterministic
    scenario that differs between repetitions is a real failure."""
    result = _e3(_status(live_identical=True, deterministic_identical=False))
    assert result["state"] == "UNMET"
    assert "S3" in str(result)


def test_both_varying_still_reports_the_deterministic_one():
    result = _e3(_status(live_identical=False, deterministic_identical=False))
    assert result["state"] == "UNMET"
    assert "S3" in str(result)
