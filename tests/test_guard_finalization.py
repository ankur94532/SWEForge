"""Reachability and satisfiability for finalization artifact guards."""

import pytest

from sweforge.guard_codes import GuardCode
from sweforge.reviewer import (
    ChallengeReport,
    EvidenceKind,
    EvidenceRef,
    ExecutionReviewResult,
    InspectionObservation,
    InspectionReport,
    InspectionStatus,
    RequirementChallenge,
    RequirementChallengeVerdict,
    RequirementInspection,
    ReviewRequirementCheck,
    ReviewRequirementClassification,
    ReviewRequirementStatus,
    _artifact_problems,
)


def _codes(problems):
    return {problem.code for problem in problems}


FA_CODES = (
    GuardCode.FA_DUPLICATE_IDENTITY,
    GuardCode.FA_INSPECTION_COVERAGE,
    GuardCode.FA_CHALLENGE_COVERAGE,
    GuardCode.FA_UNKNOWN_OBSERVATION_REQUIREMENT,
    GuardCode.FA_UNKNOWN_INSPECTION_REQUIREMENT,
    GuardCode.FA_MISSING_INSPECTION,
    GuardCode.FA_INSPECTION_NOT_VERIFIED,
    GuardCode.FA_MISSING_EVIDENCE,
    GuardCode.FA_MISSING_CHALLENGE,
    GuardCode.FA_CHALLENGE_NOT_SUPPORTED,
)


def _finalization_problems(code: GuardCode | None):
    requirement_id = "r1"
    contract = [
        {
            "requirement_id": requirement_id,
            "classification": ReviewRequirementClassification.BEHAVIORAL.value,
            "text": "The application returns the configured result.",
        }
    ]
    diff_ref = EvidenceRef(
        ref_id="diff-1",
        requirement_id=requirement_id,
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/app.py",
    )
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="all requirements verified",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id=requirement_id,
                status=ReviewRequirementStatus.SATISFIED,
                evidence="the changed code directly implements the requirement",
            )
        ],
    )
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status=InspectionStatus.VERIFIED,
                evidence_refs=[diff_ref],
            )
        ],
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/app.py",
                fact="the configured result is returned",
            )
        ],
    )
    challenge = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id=requirement_id,
                verdict=RequirementChallengeVerdict.SUPPORTED,
                challenge_summary="the direct code evidence supports the behavior",
                evidence_refs=[diff_ref],
            )
        ]
    )

    if code is GuardCode.FA_DUPLICATE_IDENTITY:
        inspection.observations.append(inspection.observations[0].model_copy(deep=True))
    elif code is GuardCode.FA_INSPECTION_COVERAGE:
        inspection.inspections = []
    elif code is GuardCode.FA_CHALLENGE_COVERAGE:
        challenge.challenges = []
    elif code is GuardCode.FA_UNKNOWN_OBSERVATION_REQUIREMENT:
        inspection.observations.append(
            InspectionObservation(
                observation_id="ghost-observation",
                requirement_id="ghost",
                kind="CODE",
                path="src/app.py",
            )
        )
    elif code is GuardCode.FA_UNKNOWN_INSPECTION_REQUIREMENT:
        inspection.inspections.append(
            RequirementInspection(
                requirement_id="ghost", status=InspectionStatus.UNVERIFIED
            )
        )
    elif code is GuardCode.FA_MISSING_INSPECTION:
        inspection.inspections = []
    elif code is GuardCode.FA_INSPECTION_NOT_VERIFIED:
        inspection.inspections[0].status = InspectionStatus.UNVERIFIED
    elif code is GuardCode.FA_MISSING_EVIDENCE:
        inspection.inspections[0].evidence_refs = []
    elif code is GuardCode.FA_MISSING_CHALLENGE:
        challenge.challenges = []
    elif code is GuardCode.FA_CHALLENGE_NOT_SUPPORTED:
        challenge.challenges[0].verdict = RequirementChallengeVerdict.CHALLENGED

    return _artifact_problems(
        result,
        contract,
        inspection,
        challenge,
        ledger=[],
        evidence={"changed_files": ["src/app.py"], "execution": {}},
    )


@pytest.mark.parametrize("code", FA_CODES, ids=lambda code: code.value)
def test_finalization_guard_reachability(code):
    assert code in _codes(_finalization_problems(code))


@pytest.mark.parametrize("code", FA_CODES, ids=lambda code: code.value)
def test_finalization_guard_satisfiability(code):
    assert _finalization_problems(None) == [], (
        f"well-behaved finalizer artifact triggered {code}"
    )


def test_guard_audit_tables_cover_the_complete_enum_once():
    from test_guard_inspection_authority import (
        AUTHORITY_CODES,
        INSPECTION_IDENTITY_CODES,
        REFERENCE_CODES,
    )
    from test_guard_requirement_semantic import RC_CODES, SP_CODES

    audited = (
        *RC_CODES,
        *SP_CODES,
        *REFERENCE_CODES,
        *AUTHORITY_CODES,
        *INSPECTION_IDENTITY_CODES,
        *FA_CODES,
    )
    assert len(audited) == len(set(audited))
    assert set(audited) == set(GuardCode)
