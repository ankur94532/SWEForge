import pytest

from sweforge.guard_codes import GuardCode
from sweforge.reviewer import (
    EvidenceKind,
    EvidenceRef,
    ExecutionReviewResult,
    InspectionReport,
    RequirementInspection,
    ReviewAttemptObservation,
    ReviewerContext,
    ReviewFinalizationError,
    SemanticReviewArtifact,
    SpecialistStageReport,
    review_execution,
    review_requirement_contract,
)


class _FakeAgent:
    def __init__(self, result=None):
        self.result = result

    def invoke(self, payload):
        del payload
        return self.result


def _review_evidence():
    return {
        "plan": {"id": "plan-1", "version": 1, "text": "edit src/main.py"},
        "source": {"event_key": "root"},
        "attempt": {"attempt_id": "attempt-1"},
        "execution": {"status": "SUCCEEDED"},
        "current_head": "head",
        "base_head": "base",
        "dirty": True,
        "changed_files": ["src/main.py"],
        "diff": "-old\n+new\n",
    }


def _structural_report(evidence, *, grounded):
    requirement_id = review_requirement_contract(evidence)[0]["requirement_id"]
    refs = (
        [
            EvidenceRef(
                ref_id="model-diff-ref",
                requirement_id=requirement_id,
                kind=EvidenceKind.TRUSTED_DIFF,
                path="src/main.py",
            )
        ]
        if grounded
        else []
    )
    return InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status="VERIFIED",
                evidence_refs=refs,
            )
        ]
    )


def _semantic_report():
    return SemanticReviewArtifact(
        implementation=SpecialistStageReport(
            stage="IMPLEMENTATION", status="SKIPPED", applicable=False
        ),
        test_validation=SpecialistStageReport(
            stage="TEST_VALIDATION", status="SKIPPED", applicable=False
        ),
    )


def _install_review_agents(monkeypatch, inspections):
    agents = [
        _FakeAgent({"structured_response": inspection}) for inspection in inspections
    ]
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="NEEDS_FIXES", summary="model requested a repair"
            )
        }
    )
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: agents.pop(0)
    )
    monkeypatch.setattr(
        "sweforge.reviewer._split_semantic_review", lambda **kwargs: _semantic_report()
    )
    monkeypatch.setattr(
        "sweforge.reviewer._build_finalizer", lambda *args, **kwargs: finalizer
    )


def test_observer_records_first_pass_before_correction_and_finalization(monkeypatch):
    evidence = _review_evidence()
    _install_review_agents(
        monkeypatch,
        [
            _structural_report(evidence, grounded=False),
            _structural_report(evidence, grounded=True),
        ],
    )
    observations: list[ReviewAttemptObservation] = []

    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=evidence,
        attempt_observer=observations.append,
    )

    assert result.verdict == "NEEDS_FIXES"
    assert [(item.stage, item.attempt) for item in observations] == [
        ("INSPECTION", 1),
        ("INSPECTION", 2),
        ("FINALIZATION", 1),
    ]
    assert [problem.code for problem in observations[0].guard_problems] == [
        GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION
    ]
    assert observations[0].artifact["inspections"][0]["evidence_refs"] == []
    assert observations[1].guard_problems == ()
    assert observations[2].artifact["finalizer"]["verdict"] == "NEEDS_FIXES"
    assert observations[2].guard_problems == ()


def test_observer_records_both_rejected_attempts_before_exhaustion(monkeypatch):
    evidence = _review_evidence()
    malformed = _structural_report(evidence, grounded=False)
    _install_review_agents(monkeypatch, [malformed, malformed])
    observations: list[ReviewAttemptObservation] = []

    with pytest.raises(ReviewFinalizationError, match="invalid inspection artifact"):
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=evidence,
            attempt_observer=observations.append,
        )

    assert [(item.stage, item.attempt) for item in observations] == [
        ("INSPECTION", 1),
        ("INSPECTION", 2),
    ]
    assert all(item.guard_problems for item in observations)


def test_observer_is_optional_and_callback_failures_are_visible(monkeypatch):
    evidence = _review_evidence()
    valid = _structural_report(evidence, grounded=True)
    _install_review_agents(monkeypatch, [valid])
    assert (
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=evidence,
        ).verdict
        == "NEEDS_FIXES"
    )

    _install_review_agents(monkeypatch, [valid])

    def broken_observer(_observation):
        raise RuntimeError("observer failed")

    with pytest.raises(RuntimeError, match="observer failed"):
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=evidence,
            attempt_observer=broken_observer,
        )
