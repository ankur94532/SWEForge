import json
import threading
import time
from collections import deque
from pathlib import Path

from acceptance.runner.conformance import (
    REPLAY_CAVEAT,
    classify_guard_codes,
    evaluate_runs,
    fixture_class,
    main,
    replay_recorded_run,
    run_live_fixtures,
)
from sweforge.guard_codes import GuardCode, GuardProblem
from sweforge.reviewer import (
    EvidenceKind,
    EvidenceRef,
    ExecutionReviewResult,
    InspectionReport,
    InspectionStatus,
    RequirementInspection,
    ReviewAttemptObservation,
    ReviewFinalizationError,
    ReviewRequirementCheck,
    ReviewRequirementStatus,
    SemanticReviewArtifact,
    SpecialistStageReport,
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


def test_live_fixture_jobs_run_concurrently_and_preserve_order(monkeypatch):
    barrier = threading.Barrier(2)

    def fake_run_fixture(path, **_kwargs):
        barrier.wait(timeout=1)
        time.sleep(0.01)
        return {"fixture": path.name}

    monkeypatch.setattr("acceptance.runner.conformance.run_fixture", fake_run_fixture)
    paths = [Path("second"), Path("first")]
    assert run_live_fixtures(
        paths,
        model="scripted",
        runs=1,
        classifications={},
        workers=2,
    ) == [{"fixture": "second"}, {"fixture": "first"}]


def test_runs_can_execute_concurrently_and_preserve_sample_order():
    barrier = threading.Barrier(2)
    call_lock = threading.Lock()
    next_call = 0

    def run_once(observer):
        nonlocal next_call
        with call_lock:
            next_call += 1
            call = next_call
        barrier.wait(timeout=1)
        observer(_observation("INSPECTION"))
        observer(_observation("FINALIZATION", raw_verdict="BLOCKED"))
        return ExecutionReviewResult(verdict="BLOCKED", summary=f"call {call}")

    report = evaluate_runs(
        fixture_id="RF-016-inspector-authority",
        model="scripted",
        runs=2,
        expected_verdict=None,
        classifications=CLASSIFICATIONS,
        run_once=run_once,
        run_workers=2,
    )
    assert [record["run"] for record in report["results"]] == [1, 2]
    assert report["eventual"]["rate"] == 1.0


def test_recorded_artifact_replay_is_deterministic_and_provider_free(tmp_path):
    requirement_id = "plan:step:1"
    changed_path = "src/example.py"
    ref = EvidenceRef(
        ref_id="diff-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.TRUSTED_DIFF,
        path=changed_path,
    )
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status=InspectionStatus.VERIFIED,
                evidence_refs=[ref],
            )
        ]
    )
    finalizer = ExecutionReviewResult(
        verdict="NEEDS_FIXES",
        summary="repair needed",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id=requirement_id,
                status=ReviewRequirementStatus.SATISFIED,
                evidence="diff",
                evidence_refs=[ref],
            )
        ],
    )
    semantic = SemanticReviewArtifact(
        implementation=SpecialistStageReport(
            stage="IMPLEMENTATION", status="SKIPPED", applicable=False
        ),
        test_validation=SpecialistStageReport(
            stage="TEST_VALIDATION", status="SKIPPED", applicable=False
        ),
    )
    recorded = {
        "run": 1,
        "first_pass": "OK",
        "eventual": "OK",
        "eventual_success": True,
        "result_verdict": "NEEDS_FIXES",
        "raw_model_verdict": "NEEDS_FIXES",
        "guard_veto": False,
        "guard_codes": [],
        "observations": [
            {
                "stage": "INSPECTION",
                "attempt": 1,
                "artifact": inspection.model_dump(mode="json"),
                "ledger": [],
                "error_type": None,
            },
            {
                "stage": "FINALIZATION",
                "attempt": 1,
                "artifact": {
                    "finalizer": finalizer.model_dump(mode="json"),
                    "inspection": inspection.model_dump(mode="json"),
                    "semantic_review": semantic.model_dump(mode="json"),
                },
                "ledger": [],
                "error_type": None,
            },
        ],
    }
    contract = [
        {
            "requirement_id": requirement_id,
            "classification": "STRUCTURAL",
            "text": "Create the requested file.",
        }
    ]

    replayed = replay_recorded_run(
        recorded,
        fixture_id="RF-7-a34d33a8",
        expected_verdict="BLOCKED",
        evidence={"changed_files": [changed_path], "diff": "+example\n"},
        contract=contract,
        classifications=CLASSIFICATIONS,
        worktree=tmp_path,
    )

    assert replayed["outcome_changed"] is False
    assert replayed["verdict_changed"] is False
    assert REPLAY_CAVEAT.startswith("Replay proves what current code does")
