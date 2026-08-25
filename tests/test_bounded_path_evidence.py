"""E4 reads bounded-path evidence that a run actually observed.

A bound is only evidence when a run reached it. Asserting the constant would
prove the constant, not the behaviour, so scenarios record what they saw.
"""

from harness.observation import Observation
from harness.scenario import Layer, ScenarioResult, campaign_status


def test_recording_a_bound_marks_it_observed():
    observation = Observation()
    observation.record_bound("S17_EXHAUSTION", actual=3, expected=3)
    assert observation.bounded_paths["S17_EXHAUSTION"] == {
        "observed": True,
        "actual": 3,
        "expected": 3,
    }


def test_an_unobserved_path_is_absent_rather_than_defaulted():
    """Absent means CANNOT_EVALUATE downstream; a default would assert a
    bound nothing measured."""
    assert Observation().bounded_paths == {}


def test_bounds_reach_the_campaign_status():
    result = ScenarioResult(
        "S17",
        Layer.L1,
        True,
        bounded_paths={
            "S17_EXHAUSTION": {"observed": True, "actual": 3, "expected": 3}
        },
    )
    status = campaign_status([result])
    assert status["bounded_paths"]["S17_EXHAUSTION"]["actual"] == 3


def test_bounds_from_several_scenarios_merge():
    """Each path is driven by whichever scenario exercises it."""
    results = [
        ScenarioResult(
            "S17",
            Layer.L1,
            True,
            bounded_paths={
                "S17_EXHAUSTION": {"observed": True, "actual": 3, "expected": 3}
            },
        ),
        ScenarioResult(
            "S26",
            Layer.L1,
            True,
            bounded_paths={
                "S26_BACKOFF": {"observed": True, "actual": 3, "expected": 3}
            },
        ),
    ]
    merged = campaign_status(results)["bounded_paths"]
    assert set(merged) == {"S17_EXHAUSTION", "S26_BACKOFF"}


def test_a_scenario_recording_nothing_contributes_nothing():
    status = campaign_status([ScenarioResult("S5", Layer.L1, True)])
    assert status["bounded_paths"] == {}


def test_a_divergent_bound_is_preserved_not_normalised():
    """E4 compares actual against expected; the harness must not hide a
    mismatch by rewriting one to match the other."""
    result = ScenarioResult(
        "S17",
        Layer.L1,
        True,
        bounded_paths={
            "S17_EXHAUSTION": {"observed": True, "actual": 5, "expected": 3}
        },
    )
    evidence = campaign_status([result])["bounded_paths"]["S17_EXHAUSTION"]
    assert (evidence["actual"], evidence["expected"]) == (5, 3)
