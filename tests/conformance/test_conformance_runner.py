import json
from collections import deque
from pathlib import Path

from acceptance.runner.conformance import (
    classify_guard_codes,
    evaluate_runs,
    fixture_class,
    main,
)
from sweforge.guard_codes import GuardCode, GuardProblem
from sweforge.reviewer import (
    ExecutionReviewResult,
    ReviewAttemptObservation,
    ReviewFinalizationError,
)

CLASSIFICATIONS = {
    GuardCode.RC_REQUIREMENT_COVERAGE.value: "B",
    GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION.value: "UNKNOWN",
    GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE.value: "A",
}


def _observation(stage, *codes, attempt=1, raw_verdict="NEEDS_FIXES"):
    problems = tuple(
        GuardProblem(code=code, detail=f"literal condition for {code.value}")
        for code in codes
    )
    artifact = (
        {"inspections": []}
        if stage == "INSPECTION"
        else {"finalizer": {"verdict": raw_verdict}, "inspection": {}}
    )
    evaluated = (
        artifact
        if stage == "INSPECTION"
        else {"verdict": "BLOCKED" if problems else raw_verdict}
    )
    return ReviewAttemptObservation(
        stage=stage,
        attempt=attempt,
        artifact=artifact,
        evaluated_artifact=evaluated,
        guard_problems=problems,
    )


def _scripted_runner(first_passes, eventuals=None, verdicts=None):
    first = deque(first_passes)
    eventual = deque(eventuals or [()] * len(first_passes))
    results = deque(verdicts or ["NEEDS_FIXES"] * len(first_passes))
    calls = []

    def run_once(observer):
        calls.append(1)
        first_codes = first.popleft()
        observer(_observation("INSPECTION", *first_codes))
        final_codes = eventual.popleft()
        scripted_verdict = results.popleft()
        raw_verdict = "ACCEPT" if final_codes else scripted_verdict
        observer(_observation("FINALIZATION", *final_codes, raw_verdict=raw_verdict))
        return ExecutionReviewResult(
            verdict=("BLOCKED" if final_codes else scripted_verdict),
            summary="scripted result",
        )

    return run_once, calls


def test_multicode_precedence_is_unclassified_then_a_then_b():
    unknown = GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION.value
    class_a = GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE.value
    class_b = GuardCode.RC_REQUIREMENT_COVERAGE.value
    assert classify_guard_codes([], CLASSIFICATIONS) == "OK"
    assert classify_guard_codes([class_b], CLASSIFICATIONS) == "B"
    assert classify_guard_codes([class_a, class_b], CLASSIFICATIONS) == "A"
    assert (
        classify_guard_codes([unknown, class_a, class_b], CLASSIFICATIONS)
        == "UNCLASSIFIED"
    )
    assert classify_guard_codes(["MISSING-CODE"], CLASSIFICATIONS) == "UNCLASSIFIED"


def test_nineteen_of_twenty_first_pass_and_all_eventual_passes():
    class_b = GuardCode.RC_REQUIREMENT_COVERAGE
    run_once, calls = _scripted_runner([()] * 19 + [(class_b,)])
    report = evaluate_runs(
        fixture_id="RF-10-5ad6d25f",
        model="scripted",
        runs=20,
        expected_verdict="NEEDS_FIXES",
        classifications=CLASSIFICATIONS,
        run_once=run_once,
    )
    assert report["first_pass"] == {"counts": {"B": 1, "OK": 19}, "rate": 0.95}
    assert report["eventual"]["rate"] == 1.0
    assert report["verdict"] == "PASS"
    assert len(calls) == 20


def test_twelve_of_twenty_first_pass_fails_even_when_eventual_is_perfect():
    class_b = GuardCode.RC_REQUIREMENT_COVERAGE
    run_once, _calls = _scripted_runner([()] * 12 + [(class_b,)] * 8)
    report = evaluate_runs(
        fixture_id="RF-10-5ad6d25f",
        model="scripted",
        runs=20,
        expected_verdict="NEEDS_FIXES",
        classifications=CLASSIFICATIONS,
        run_once=run_once,
    )
    assert report["first_pass"]["rate"] == 0.6
    assert report["eventual"]["rate"] == 1.0
    assert report["verdict"].startswith("FAIL: first-pass 0.600")


def test_stable_fixture_requires_verdict_match_but_rf016_requires_only_structure():
    stable_once, _calls = _scripted_runner([()], verdicts=["BLOCKED"])
    stable = evaluate_runs(
        fixture_id="RF-10-5ad6d25f",
        model="scripted",
        runs=1,
        expected_verdict="NEEDS_FIXES",
        classifications=CLASSIFICATIONS,
        run_once=stable_once,
    )
    rf016_once, _calls = _scripted_runner([()], verdicts=["BLOCKED"])
    rf016 = evaluate_runs(
        fixture_id="RF-016-inspector-authority",
        model="scripted",
        runs=1,
        expected_verdict=None,
        classifications=CLASSIFICATIONS,
        run_once=rf016_once,
    )
    assert stable["eventual"]["rate"] == 0.0
    assert rf016["eventual"]["rate"] == 1.0


def test_a_and_unknown_block_and_histograms_retain_every_code():
    class_a = GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE
    unknown = GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION
    class_b = GuardCode.RC_REQUIREMENT_COVERAGE
    run_once, _calls = _scripted_runner(
        [(class_a, class_b), (unknown, class_b)],
        eventuals=[(class_a,), (unknown,)],
    )
    report = evaluate_runs(
        fixture_id="RF-7-a34d33a8",
        model="scripted",
        runs=2,
        expected_verdict="BLOCKED",
        classifications=CLASSIFICATIONS,
        run_once=run_once,
    )
    assert report["guard_histogram"] == {
        class_a.value: 2,
        unknown.value: 2,
        class_b.value: 2,
    }
    assert report["guard_run_histogram"] == {
        class_a.value: 1,
        unknown.value: 1,
        class_b.value: 2,
    }
    assert "Class A guard rejection observed" in report["verdict"]
    assert "unclassified guard rejection observed" in report["verdict"]


def test_failed_invocations_are_measured_once_and_never_retried():
    calls = []

    def run_once(observer):
        calls.append(1)
        observer(
            _observation("INSPECTION", GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION)
        )
        raise ReviewFinalizationError("scripted rejection")

    report = evaluate_runs(
        fixture_id="RF-016-inspector-authority",
        model="scripted",
        runs=3,
        expected_verdict=None,
        classifications=CLASSIFICATIONS,
        run_once=run_once,
    )
    assert len(calls) == 3
    assert len(report["results"]) == 3
    assert report["eventual"]["rate"] == 0.0
    assert report["eventual"]["counts"] == {"UNCLASSIFIED": 3}


def test_a_guard_on_correction_attempt_cannot_hide_behind_eventual_success():
    def run_once(observer):
        observer(_observation("INSPECTION", GuardCode.RC_REQUIREMENT_COVERAGE))
        observer(
            _observation(
                "INSPECTION",
                GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE,
                attempt=2,
            )
        )
        observer(_observation("FINALIZATION", raw_verdict="NEEDS_FIXES"))
        return ExecutionReviewResult(verdict="NEEDS_FIXES", summary="scripted result")

    report = evaluate_runs(
        fixture_id="RF-10-5ad6d25f",
        model="scripted",
        runs=1,
        expected_verdict="NEEDS_FIXES",
        classifications=CLASSIFICATIONS,
        run_once=run_once,
    )
    assert report["eventual"]["rate"] == 1.0
    assert report["class_a"]["runs"] == 1
    assert "Class A guard rejection observed" in report["verdict"]


def test_fixture_policy_is_explicit_for_all_eight_cases():
    assert fixture_class("RF-016-inspector-authority") == "RF016"
    assert fixture_class("RF-10-5ad6d25f") == "STABLE"
    assert fixture_class("RF-7-a34d33a8") == "CONTESTED"


def test_offline_cli_validates_all_eight_stored_baselines(tmp_path, capsys):
    fixture_root = Path(__file__).parents[2] / "acceptance/fixtures/review/v1"
    report_path = tmp_path / "offline.json"
    assert (
        main(
            [
                str(fixture_root),
                "--offline",
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    stdout = json.loads(capsys.readouterr().out)
    report = json.loads(report_path.read_text())
    assert stdout["verdict"] == "OFFLINE_BASELINE_VALID"
    assert len(report["fixtures"]) == 8
    assert {item["fixture_class"] for item in report["fixtures"]} == {
        "RF016",
        "STABLE",
        "CONTESTED",
    }
