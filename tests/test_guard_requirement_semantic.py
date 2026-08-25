"""Reachability and satisfiability for requirement and specialist guards."""

import hashlib

import pytest

from sweforge.guard_codes import GuardCode
from sweforge.reviewer import (
    CandidateAssociationBasis,
    EvidenceCluster,
    EvidenceClusterRange,
    EvidenceClusterRole,
    EvidenceFinding,
    EvidenceFindingProvenance,
    EvidenceFindingSeverity,
    ExecutionReviewResult,
    FindingCandidateAssociation,
    ReviewRequirementCheck,
    ReviewRequirementStatus,
    SemanticReviewArtifact,
    SpecialistStage,
    SpecialistStageReport,
    SpecialistStageStatus,
    _accept_coverage_problems,
    _semantic_artifact_problems,
)


def _codes(problems):
    return {problem.code for problem in problems}


RC_CODES = (
    GuardCode.RC_REQUIREMENT_COVERAGE,
    GuardCode.RC_DUPLICATE_REQUIREMENT,
    GuardCode.RC_UNEXPECTED_REQUIREMENT,
    GuardCode.RC_UNSATISFIED_REQUIREMENT,
)


def _check(
    requirement_id: str,
    status: ReviewRequirementStatus = ReviewRequirementStatus.SATISFIED,
) -> ReviewRequirementCheck:
    return ReviewRequirementCheck(
        requirement_id=requirement_id,
        status=status,
        evidence="direct evidence",
    )


def _coverage_problems(code: GuardCode | None):
    contract = [{"requirement_id": "r1"}]
    checks = [_check("r1")]
    if code is GuardCode.RC_REQUIREMENT_COVERAGE:
        contract.append({"requirement_id": "r2"})
    elif code is GuardCode.RC_DUPLICATE_REQUIREMENT:
        checks.append(_check("r1"))
    elif code is GuardCode.RC_UNEXPECTED_REQUIREMENT:
        checks.append(_check("r2"))
    elif code is GuardCode.RC_UNSATISFIED_REQUIREMENT:
        checks = [_check("r1", ReviewRequirementStatus.UNVERIFIED)]
    result = ExecutionReviewResult(
        verdict="ACCEPT", summary="complete review", requirement_checks=checks
    )
    return _accept_coverage_problems(result, contract)


@pytest.mark.parametrize("code", RC_CODES, ids=lambda code: code.value)
def test_requirement_guard_reachability(code):
    assert code in _codes(_coverage_problems(code))


@pytest.mark.parametrize("code", RC_CODES, ids=lambda code: code.value)
def test_requirement_guard_satisfiability(code):
    assert _coverage_problems(None) == [], f"well-behaved result triggered {code}"


SP_CODES = (
    GuardCode.SP_DUPLICATE_EVIDENCE_CLUSTER,
    GuardCode.SP_CLUSTER_HASH_MISMATCH,
    GuardCode.SP_REQUIRED_STAGE_INCOMPLETE,
    GuardCode.SP_INAPPLICABLE_STAGE_NOT_SKIPPED,
    GuardCode.SP_STAGE_APPLICABILITY_MISMATCH,
    GuardCode.SP_INVALID_FINDING_PROVENANCE,
    GuardCode.SP_CURRENT_BLOCKING_FINDING,
    GuardCode.SP_DUPLICATE_FINDING,
    GuardCode.SP_UNKNOWN_ASSOCIATED_FINDING,
    GuardCode.SP_DUPLICATE_ASSOCIATION,
    GuardCode.SP_MISSING_ASSOCIATION,
)


def _cluster(cluster_id: str, role: EvidenceClusterRole, path: str) -> EvidenceCluster:
    excerpt = f"bounded evidence for {cluster_id}"
    return EvidenceCluster(
        cluster_id=cluster_id,
        role=role,
        ranges=[
            EvidenceClusterRange(
                evidence_id=f"e-{cluster_id}",
                path=path,
                start_line=1,
                end_line=2,
                hunk_identity=f"h-{cluster_id}",
            )
        ],
        evidence_ids=[f"e-{cluster_id}"],
        bounded_raw_excerpt=excerpt,
        content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
    )


def _report(
    stage: SpecialistStage,
    *,
    status: SpecialistStageStatus = SpecialistStageStatus.COMPLETED,
    applicable: bool = True,
    findings: list[EvidenceFinding] | None = None,
) -> SpecialistStageReport:
    return SpecialistStageReport(
        stage=stage,
        status=status,
        applicable=applicable,
        findings=findings or [],
    )


def _finding(
    finding_id: str,
    cluster: EvidenceCluster,
    *,
    severity: EvidenceFindingSeverity = EvidenceFindingSeverity.WARNING,
    path: str | None = None,
) -> EvidenceFinding:
    evidence_range = cluster.ranges[0]
    return EvidenceFinding(
        finding_id=finding_id,
        severity=severity,
        cluster_ids=[cluster.cluster_id],
        provenance=[
            EvidenceFindingProvenance(
                cluster_id=cluster.cluster_id,
                evidence_id=evidence_range.evidence_id,
                path=path or evidence_range.path,
                start_line=evidence_range.start_line,
                end_line=evidence_range.end_line,
                hunk_identity=evidence_range.hunk_identity,
            )
        ],
        concise_summary="specific specialist finding",
        concrete_source_facts=["the bounded range shows the condition"],
        behavioral_consequence="the behavior is observably affected",
    )


def _association(finding_id: str) -> FindingCandidateAssociation:
    return FindingCandidateAssociation(
        finding_id=finding_id,
        candidate_requirement_ids=[],
        basis=CandidateAssociationBasis.NONE,
    )


def _semantic_problems(code: GuardCode | None):
    implementation_cluster = _cluster(
        "impl", EvidenceClusterRole.IMPLEMENTATION, "src/app.py"
    )
    test_cluster = _cluster(
        "test", EvidenceClusterRole.TEST_VALIDATION, "tests/test_app.py"
    )
    clusters = [implementation_cluster, test_cluster]
    implementation = _report(SpecialistStage.IMPLEMENTATION)
    test_validation = _report(SpecialistStage.TEST_VALIDATION)
    associations = []

    if code is GuardCode.SP_DUPLICATE_EVIDENCE_CLUSTER:
        clusters.insert(1, implementation_cluster.model_copy(deep=True))
    elif code is GuardCode.SP_CLUSTER_HASH_MISMATCH:
        implementation_cluster.content_hash = "0" * 64
    elif code is GuardCode.SP_REQUIRED_STAGE_INCOMPLETE:
        implementation.status = SpecialistStageStatus.UNVERIFIED
    elif code is GuardCode.SP_INAPPLICABLE_STAGE_NOT_SKIPPED:
        clusters = [implementation_cluster]
        test_validation = _report(SpecialistStage.TEST_VALIDATION, applicable=False)
    elif code is GuardCode.SP_STAGE_APPLICABILITY_MISMATCH:
        implementation.applicable = False
    elif code is GuardCode.SP_INVALID_FINDING_PROVENANCE:
        finding = _finding("f1", implementation_cluster, path="src/other.py")
        implementation.findings = [finding]
        associations = [_association("f1")]
    elif code is GuardCode.SP_CURRENT_BLOCKING_FINDING:
        finding = _finding(
            "f1", implementation_cluster, severity=EvidenceFindingSeverity.BLOCKING
        )
        implementation.findings = [finding]
        associations = [_association("f1")]
    elif code is GuardCode.SP_DUPLICATE_FINDING:
        implementation.findings = [_finding("f1", implementation_cluster)]
        test_validation.findings = [_finding("f1", test_cluster)]
        associations = [_association("f1")]
    elif code is GuardCode.SP_UNKNOWN_ASSOCIATED_FINDING:
        associations = [_association("ghost")]
    elif code is GuardCode.SP_DUPLICATE_ASSOCIATION:
        implementation.findings = [_finding("f1", implementation_cluster)]
        associations = [_association("f1"), _association("f1")]
    elif code is GuardCode.SP_MISSING_ASSOCIATION:
        implementation.findings = [_finding("f1", implementation_cluster)]

    semantic = SemanticReviewArtifact(
        clusters=clusters,
        implementation=implementation,
        test_validation=test_validation,
        candidate_associations=associations,
    )
    return _semantic_artifact_problems(semantic)


@pytest.mark.parametrize("code", SP_CODES, ids=lambda code: code.value)
def test_specialist_guard_reachability(code):
    assert code in _codes(_semantic_problems(code))


@pytest.mark.parametrize("code", SP_CODES, ids=lambda code: code.value)
def test_specialist_guard_satisfiability(code):
    assert _semantic_problems(None) == [], f"well-behaved artifact triggered {code}"
