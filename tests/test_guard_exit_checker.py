"""The campaign exit checker must fail closed on absent evidence."""

from acceptance.runner.exit_conditions import (
    BOUNDED_PATHS,
    LIVE_GITHUB_IDS,
    MODEL_COMPONENTS,
    SCENARIO_IDS,
    evaluate_exit_conditions,
)

DETERMINISTIC_IDS = (*SCENARIO_IDS, "S48", "S49")


def _states(report):
    return {item["condition_id"]: item["state"] for item in report["conditions"]}


def _scenario_records(*, retries=False):
    return [
        {
            "id": scenario_id,
            "layer": (
                "LIVE_GITHUB" if scenario_id in LIVE_GITHUB_IDS else "LIVE_PROCESS"
            ),
            "ok": True,
            "checks": [
                {
                    "invariant": "CONTROL",
                    "ok": True,
                    "status": "PASS",
                    "detail": "observed",
                }
            ],
            "harness_retries": 1 if retries and scenario_id == "S1" else 0,
            "outcome": "PASS_WITH_RETRY" if retries and scenario_id == "S1" else "PASS",
        }
        for scenario_id in SCENARIO_IDS
    ]


def _deterministic_records(*, retries=False):
    records = _scenario_records(retries=retries)
    records.extend(
        {
            "id": scenario_id,
            "layer": "L1",
            "ok": True,
            "checks": [
                {
                    "invariant": "CONTROL",
                    "ok": True,
                    "status": "PASS",
                    "detail": "observed",
                }
            ],
            "harness_retries": 0,
            "outcome": "PASS",
        }
        for scenario_id in ("S48", "S49")
    )
    return records


def _complete_status():
    records = _scenario_records()
    return {
        "scenarios": records,
        "requested_scenarios": list(SCENARIO_IDS),
        "deterministic_scenarios": list(DETERMINISTIC_IDS),
        "execution_integrity": {
            "observed_ids": list(SCENARIO_IDS),
            "skipped": [],
            "substitutions": [],
        },
        "repetitions": [
            {"index": number, "status": {"scenarios": _deterministic_records()}}
            for number in range(1, 4)
        ],
        "reproducibility": {
            "required_runs": 3,
            "identical": True,
            "scenarios": {
                scenario_id: {"identical": True, "runs": 3}
                for scenario_id in DETERMINISTIC_IDS
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
    """E5 is excluded by recorded decision, so an empty status leaves seven
    conditions unevaluable and one out of scope -- and is still not ready."""
    report = evaluate_exit_conditions({})
    assert report["ready"] is False
    assert report["summary"] == {
        "MET": 0,
        "UNMET": 0,
        "CANNOT_EVALUATE": 7,
        "OUT_OF_SCOPE": 1,
    }
    assert set(_states(report).values()) == {"CANNOT_EVALUATE", "OUT_OF_SCOPE"}
    assert _states(report)["E5"] == "OUT_OF_SCOPE"


def test_complete_positive_evidence_satisfies_all_eight_conditions():
    """Complete evidence includes model_components, which takes E5 back out of
    exclusion and evaluates it: the exclusion cannot mask a real result."""
    report = evaluate_exit_conditions(_complete_status())
    assert report["ready"] is True
    assert report["summary"] == {
        "MET": 8,
        "UNMET": 0,
        "CANNOT_EVALUATE": 0,
        "OUT_OF_SCOPE": 0,
    }
    assert set(_states(report).values()) == {"MET"}


def test_aggregate_curator_metric_cannot_cover_two_independent_tracks():
    status = _complete_status()
    status["model_components"]["curators"] = _curator_metrics()
    report = evaluate_exit_conditions(status)
    assert _states(report)["E5"] == "CANNOT_EVALUATE"


def test_deterministic_l1_results_do_not_substitute_for_live_github_evidence():
    status = _complete_status()
    for record in status["scenarios"]:
        record["layer"] = "L1"
    for repetition in status["repetitions"]:
        for record in repetition["status"]["scenarios"]:
            record["layer"] = "L1"

    report = evaluate_exit_conditions(status)
    states = _states(report)
    assert states["E3"] == "MET"
    assert states["E1"] == states["E2"] == states["E7"] == "CANNOT_EVALUATE"
    for condition_id in ("E1", "E2", "E7"):
        condition = next(
            item
            for item in report["conditions"]
            if item["condition_id"] == condition_id
        )
        assert condition["evidence"]["missing"] == sorted(LIVE_GITHUB_IDS)


def test_vacuous_invariant_cannot_satisfy_deterministic_repetition_evidence():
    status = _complete_status()
    status["repetitions"][1]["status"]["scenarios"][0]["checks"][0]["status"] = (
        "VACUOUS"
    )

    condition = next(
        item
        for item in evaluate_exit_conditions(status)["conditions"]
        if item["condition_id"] == "E3"
    )
    assert condition["state"] == "CANNOT_EVALUATE"
    assert condition["evidence"]["vacuous"] == [
        {"scenario_id": "S1", "invariant": "CONTROL"}
    ]


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
    # E5 is out of scope by recorded decision; everything else is unevaluable.
    assert states["E5"] == "OUT_OF_SCOPE"
    assert all(
        states[item] == "CANNOT_EVALUATE" for item in states if item not in ("E3", "E5")
    )


def test_explicit_negative_evidence_is_unmet_not_unknown():
    status = _complete_status()
    status["scenarios"][0]["ok"] = False
    status["execution_integrity"]["skipped"] = ["S2"]
    status["bounded_paths"]["S8_TIMEOUT"]["actual"] = 4
    status["model_components"]["planner"]["first_pass_rate"] = 0.5
    status["contamination"]["violations"] = [{"scenario_id": "S11"}]
    status["repetitions"][-1]["status"]["scenarios"] = _deterministic_records(
        retries=True
    )
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
