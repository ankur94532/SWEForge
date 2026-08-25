"""The campaign exit checker must fail closed on absent evidence."""

from acceptance.runner.exit_conditions import (
    BOUNDED_PATHS,
    LIVE_GITHUB_IDS,
    MODEL_COMPONENTS,
    SCENARIO_IDS,
    evaluate_exit_conditions,
)


def _states(report):
    return {item["condition_id"]: item["state"] for item in report["conditions"]}


def _scenario_records(*, retries=False):
    return [
        {
            "id": scenario_id,
            "ok": True,
            "checks": [{"invariant": "CONTROL", "ok": True, "detail": "observed"}],
            "harness_retries": 1 if retries and scenario_id == "S1" else 0,
            "outcome": "PASS_WITH_RETRY" if retries and scenario_id == "S1" else "PASS",
        }
        for scenario_id in SCENARIO_IDS
    ]


def _complete_status():
    records = _scenario_records()
    return {
        "scenarios": records,
        "requested_scenarios": list(SCENARIO_IDS),
        "execution_integrity": {
            "observed_ids": list(SCENARIO_IDS),
            "skipped": [],
            "substitutions": [],
        },
        "repetitions": [
            {"index": number, "status": {"scenarios": _scenario_records()}}
            for number in range(1, 4)
        ],
        "reproducibility": {
            "required_runs": 3,
            "identical": True,
            "scenarios": {
                scenario_id: {"identical": True, "runs": 3}
                for scenario_id in SCENARIO_IDS
            },
        },
        "bounded_paths": {
            path_id: {"observed": True, "actual": 3, "expected": 3}
            for path_id in BOUNDED_PATHS
        },
        "model_components": {
            **{
                component_id: _curator_metrics()
                for component_id in MODEL_COMPONENTS
                if component_id != "curators"
            },
            "curators": {
                "repo-memory": _curator_metrics(),
                "resolution": _curator_metrics(),
            },
        },
        "contamination": {
            "checks": 26,
            "violations": [],
            "observed_ids": list(SCENARIO_IDS),
        },
        "primary_audit": {
            "checks": [
                {
                    "scenario_id": scenario_id,
                    "allowed": True,
                    "target_is_primary": False,
                }
                for scenario_id in sorted(LIVE_GITHUB_IDS)
            ],
            "primary_mutations": [],
        },
    }


def _curator_metrics():
    return {
        "first_pass_rate": 0.95,
        "first_pass_threshold": 0.95,
        "eventual_rate": 1.0,
        "eventual_threshold": 1.0,
        "class_a_count": 0,
        "unclassified_count": 0,
    }


def test_empty_status_cannot_satisfy_any_exit_condition():
    report = evaluate_exit_conditions({})
    assert report["ready"] is False
    assert report["summary"] == {"MET": 0, "UNMET": 0, "CANNOT_EVALUATE": 8}
    assert set(_states(report).values()) == {"CANNOT_EVALUATE"}


def test_complete_positive_evidence_satisfies_all_eight_conditions():
    report = evaluate_exit_conditions(_complete_status())
    assert report["ready"] is True
    assert report["summary"] == {"MET": 8, "UNMET": 0, "CANNOT_EVALUATE": 0}
    assert set(_states(report).values()) == {"MET"}


def test_aggregate_curator_metric_cannot_cover_two_independent_tracks():
    status = _complete_status()
    status["model_components"]["curators"] = _curator_metrics()
    report = evaluate_exit_conditions(status)
    assert _states(report)["E5"] == "CANNOT_EVALUATE"


def test_partial_two_run_campaign_is_precise_about_unknown_and_unmet():
    status = {
        "scenarios": [{"id": "S14", "ok": True, "checks": [{"ok": True}]}],
        "repetitions": [
            {"index": 1, "status": {"scenarios": [{"id": "S14", "ok": True}]}},
            {"index": 2, "status": {"scenarios": [{"id": "S14", "ok": True}]}},
        ],
        "reproducibility": {
            "required_runs": 2,
            "identical": True,
            "scenarios": {"S14": {"identical": True, "runs": 2}},
        },
    }
    report = evaluate_exit_conditions(status)
    states = _states(report)
    assert states["E3"] == "UNMET"
    assert all(states[item] == "CANNOT_EVALUATE" for item in states if item != "E3")


def test_explicit_negative_evidence_is_unmet_not_unknown():
    status = _complete_status()
    status["scenarios"][0]["ok"] = False
    status["execution_integrity"]["skipped"] = ["S2"]
    status["bounded_paths"]["S8_TIMEOUT"]["actual"] = 4
    status["model_components"]["planner"]["first_pass_rate"] = 0.5
    status["contamination"]["violations"] = [{"scenario_id": "S11"}]
    status["repetitions"][-1]["status"]["scenarios"] = _scenario_records(retries=True)
    status["primary_audit"]["primary_mutations"] = [{"repo": "PRIMARY"}]

    states = _states(evaluate_exit_conditions(status))
    assert states == {
        "E1": "UNMET",
        "E2": "UNMET",
        "E3": "MET",
        "E4": "UNMET",
        "E5": "UNMET",
        "E6": "UNMET",
        "E7": "UNMET",
        "E8": "UNMET",
    }
