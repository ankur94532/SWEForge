"""E3 must cover the registry, including support scenarios beyond S26."""

from acceptance.runner.exit_conditions import evaluate_exit_conditions


def _condition(status):
    return next(
        item
        for item in evaluate_exit_conditions(status)["conditions"]
        if item["condition_id"] == "E3"
    )


def _status(run_ids, *, declared=None):
    records = [
        {
            "id": item,
            "layer": "L1",
            "ok": True,
            "checks": [{"invariant": "CONTROL", "ok": True, "status": "PASS"}],
        }
        for item in run_ids
    ]
    return {
        "deterministic_scenarios": declared,
        "repetitions": [
            {"index": number, "status": {"scenarios": records}}
            for number in range(1, 4)
        ],
        "reproducibility": {
            "scenarios": {item: {"identical": True, "runs": 3} for item in run_ids}
        },
    }


def test_three_runs_cannot_pass_without_a_registry_snapshot():
    ids = [f"S{number}" for number in range(1, 29)]
    result = _condition(_status(ids))
    assert result["state"] == "CANNOT_EVALUATE"
    assert "registry snapshot" in result["detail"]


def test_three_runs_missing_a_declared_support_scenario_cannot_pass():
    declared = [f"S{number}" for number in range(1, 29)]
    result = _condition(_status(declared[:-1], declared=declared))
    assert result["state"] == "CANNOT_EVALUATE"
    assert result["evidence"]["missing"] == [declared[-1]]


def test_three_identical_runs_over_the_declared_registry_meet_e3():
    declared = [f"S{number}" for number in range(1, 29)]
    result = _condition(_status(declared, declared=declared))
    assert result["state"] == "MET"
    assert result["evidence"] == {"runs": 3, "scenarios": 28}
