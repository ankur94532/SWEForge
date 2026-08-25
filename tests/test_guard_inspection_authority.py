"""Reachability and satisfiability for inspector authority guards."""

import pytest

from sweforge.guard_codes import GuardCode
from sweforge.reviewer import (
    EvidenceKind,
    EvidenceRef,
    InspectionObservation,
    InspectionReport,
    InspectionStatus,
    RequirementInspection,
    ReviewRequirementClassification,
    _absence_source_id,
    _inspection_artifact_problems,
    _inspection_authority_problems,
    _reference_problems,
)


def _codes(problems):
    return {problem.code for problem in problems}


REFERENCE_CODES = (
    GuardCode.IA_WRONG_REQUIREMENT_REFERENCE,
    GuardCode.IA_INVALID_READ_REFERENCE,
    GuardCode.IA_UNTRUSTED_DIFF_PATH,
    GuardCode.IA_MISSING_EXECUTION_SOURCE,
    GuardCode.IA_UNKNOWN_EXECUTION_SOURCE,
    GuardCode.IA_INVALID_OBSERVATION_REFERENCE,
    GuardCode.IA_WRONG_OBSERVATION_REQUIREMENT,
    GuardCode.IA_OBSERVATION_PATH_MISMATCH,
    GuardCode.IA_INVALID_EVIDENCE_RANGE,
    GuardCode.IA_INVALID_ABSENCE_OF_CHANGE,
)


def _reference_case(code: GuardCode, *, violates: bool):
    requirement_id = "r1"
    changed_files = {"src/app.py"}
    observations = {
        "obs-1": InspectionObservation(
            observation_id="obs-1",
            requirement_id=requirement_id,
            kind="CODE",
            path="src/app.py",
        )
    }
    ledger = {
        "read-1": {
            "read_id": "read-1",
            "normalized_path": "src/app.py",
            "returned_lines": [1, 20],
        }
    }
    has_execution = True
    execution_ids = {"exec-1"}

    if code is GuardCode.IA_WRONG_REQUIREMENT_REFERENCE:
        ref = EvidenceRef(
            ref_id="diff",
            requirement_id="r2" if violates else requirement_id,
            kind=EvidenceKind.TRUSTED_DIFF,
            path="src/app.py",
        )
    elif code is GuardCode.IA_INVALID_READ_REFERENCE:
        ref = EvidenceRef(
            ref_id="read",
            requirement_id=requirement_id,
            kind=EvidenceKind.INSPECTED_FILE,
            source_id="missing" if violates else "read-1",
            path="src/app.py",
        )
    elif code is GuardCode.IA_UNTRUSTED_DIFF_PATH:
        ref = EvidenceRef(
            ref_id="diff",
            requirement_id=requirement_id,
            kind=EvidenceKind.TRUSTED_DIFF,
            path="src/other.py" if violates else "src/app.py",
        )
    elif code is GuardCode.IA_MISSING_EXECUTION_SOURCE:
        ref = EvidenceRef(
            ref_id="execution",
            requirement_id=requirement_id,
            kind=EvidenceKind.EXECUTION,
            source_id="exec-1",
        )
        has_execution = not violates
    elif code is GuardCode.IA_UNKNOWN_EXECUTION_SOURCE:
        ref = EvidenceRef(
            ref_id="execution",
            requirement_id=requirement_id,
            kind=EvidenceKind.EXECUTION,
            source_id="ghost" if violates else "exec-1",
        )
    elif code is GuardCode.IA_INVALID_OBSERVATION_REFERENCE:
        ref = EvidenceRef(
            ref_id="observation",
            requirement_id=requirement_id,
            kind=EvidenceKind.INSPECTOR_OBSERVATION,
            source_id="ghost" if violates else "obs-1",
            path="src/app.py",
        )
    elif code is GuardCode.IA_WRONG_OBSERVATION_REQUIREMENT:
        if violates:
            observations["obs-1"].requirement_id = "r2"
        ref = EvidenceRef(
            ref_id="observation",
            requirement_id=requirement_id,
            kind=EvidenceKind.INSPECTOR_OBSERVATION,
            source_id="obs-1",
            path="src/app.py",
        )
    elif code is GuardCode.IA_OBSERVATION_PATH_MISMATCH:
        ref = EvidenceRef(
            ref_id="observation",
            requirement_id=requirement_id,
            kind=EvidenceKind.INSPECTOR_OBSERVATION,
            source_id="obs-1",
            path="src/other.py" if violates else "src/app.py",
        )
    elif code is GuardCode.IA_INVALID_EVIDENCE_RANGE:
        ref = EvidenceRef(
            ref_id="diff",
            requirement_id=requirement_id,
            kind=EvidenceKind.TRUSTED_DIFF,
            path="src/app.py",
            start_line=2 if violates else 1,
            end_line=1 if violates else 2,
        )
    elif code is GuardCode.IA_INVALID_ABSENCE_OF_CHANGE:
        changed_files = {"tests/test_app.py"}
        source_id = _absence_source_id("src/app.py", changed_files)
        ref = EvidenceRef(
            ref_id="absence",
            requirement_id=requirement_id,
            kind=EvidenceKind.ABSENCE_OF_CHANGE,
            source_id="" if violates else source_id,
            path="src/app.py",
        )
    else:
        raise AssertionError(f"unhandled reference guard: {code}")

    return _reference_problems(
        [ref],
        requirement_id=requirement_id,
        observations=observations,
        ledger_by_id=ledger,
        changed_files=changed_files,
        has_execution=has_execution,
        execution_ids=execution_ids,
    )


@pytest.mark.parametrize("code", REFERENCE_CODES, ids=lambda code: code.value)
def test_reference_guard_reachability(code):
    assert code in _codes(_reference_case(code, violates=True))


@pytest.mark.parametrize("code", REFERENCE_CODES, ids=lambda code: code.value)
def test_reference_guard_satisfiability(code):
    assert _reference_case(code, violates=False) == [], (
        f"well-behaved reference triggered {code}"
    )


AUTHORITY_CODES = (
    GuardCode.IA_UNGROUNDED_OBSERVATION,
    GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION,
    GuardCode.IA_MISSING_ASSERTION_OR_SIGNAL,
    GuardCode.IA_MISSING_DIRECT_EXECUTION_EVIDENCE,
    GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE,
    GuardCode.IA_MISSING_ABSENCE_OF_CHANGE,
)


def _authority_case(code: GuardCode, *, violates: bool):
    requirement_id = "r1"
    observations = {}
    refs = []
    changed_files = set()
    has_execution = False
    execution_ids = None

    if code is GuardCode.IA_UNGROUNDED_OBSERVATION:
        classification = ReviewRequirementClassification.BEHAVIORAL
        observations["code-1"] = InspectionObservation(
            observation_id="code-1",
            requirement_id=requirement_id,
            kind="CODE",
            path="src/app.py",
        )
        if not violates:
            changed_files.add("src/app.py")
        text = "The application returns the configured result."
    elif code is GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION:
        classification = ReviewRequirementClassification.BEHAVIORAL
        if not violates:
            observations["code-1"] = InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/app.py",
            )
            changed_files.add("src/app.py")
        text = "The application returns the configured result."
    elif code is GuardCode.IA_MISSING_ASSERTION_OR_SIGNAL:
        classification = ReviewRequirementClassification.BEHAVIORAL
        observations["code-1"] = InspectionObservation(
            observation_id="code-1",
            requirement_id=requirement_id,
            kind="CODE",
            path="src/app.py",
        )
        observations["test-1"] = InspectionObservation(
            observation_id="test-1",
            requirement_id=requirement_id,
            kind="TEST",
            path="tests/test_app.py",
            assertion_or_signal="" if violates else "assert result == configured",
        )
        changed_files.update({"src/app.py", "tests/test_app.py"})
        text = "The application returns the configured result."
    elif code is GuardCode.IA_MISSING_DIRECT_EXECUTION_EVIDENCE:
        classification = ReviewRequirementClassification.VALIDATION
        if not violates:
            refs.append(
                EvidenceRef(
                    ref_id="execution",
                    requirement_id=requirement_id,
                    kind=EvidenceKind.EXECUTION,
                    source_id="exec-1",
                )
            )
            has_execution = True
            execution_ids = {"exec-1"}
        text = "Run the focused test suite successfully."
    elif code is GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE:
        classification = ReviewRequirementClassification.STRUCTURAL
        if not violates:
            refs.append(
                EvidenceRef(
                    ref_id="diff",
                    requirement_id=requirement_id,
                    kind=EvidenceKind.TRUSTED_DIFF,
                    path="config/new.toml",
                )
            )
            changed_files.add("config/new.toml")
        text = "Create the config/new.toml file."
    elif code is GuardCode.IA_MISSING_ABSENCE_OF_CHANGE:
        classification = ReviewRequirementClassification.STRUCTURAL
        changed_files.add("tests/test_app.py")
        if not violates:
            refs.append(
                EvidenceRef(
                    ref_id="absence",
                    requirement_id=requirement_id,
                    kind=EvidenceKind.ABSENCE_OF_CHANGE,
                    source_id=_absence_source_id("src/app.py", changed_files),
                    path="src/app.py",
                )
            )
        text = "Do not modify src/app.py."
    else:
        raise AssertionError(f"unhandled authority guard: {code}")

    inspection = RequirementInspection(
        requirement_id=requirement_id,
        status=InspectionStatus.VERIFIED,
        evidence_refs=refs,
    )
    return _inspection_authority_problems(
        inspection,
        requirement={
            "requirement_id": requirement_id,
            "classification": classification.value,
            "text": text,
        },
        observations=observations,
        ledger_by_id={},
        changed_files=changed_files,
        has_execution=has_execution,
        execution_ids=execution_ids,
    )


@pytest.mark.parametrize("code", AUTHORITY_CODES, ids=lambda code: code.value)
def test_inspection_authority_guard_reachability(code):
    assert code in _codes(_authority_case(code, violates=True))


@pytest.mark.parametrize("code", AUTHORITY_CODES, ids=lambda code: code.value)
def test_inspection_authority_guard_satisfiability(code):
    assert _authority_case(code, violates=False) == [], (
        f"well-behaved inspection triggered {code}"
    )


INSPECTION_IDENTITY_CODES = (
    GuardCode.II_UNKNOWN_OBSERVATION_REQUIREMENT,
    GuardCode.II_UNKNOWN_INSPECTION_REQUIREMENT,
)


def _inspection_identity_case(code: GuardCode | None):
    requirement_id = "r1"
    contract = [
        {
            "requirement_id": requirement_id,
            "classification": ReviewRequirementClassification.BEHAVIORAL.value,
            "text": "The application returns the configured result.",
        }
    ]
    inspections = [
        RequirementInspection(
            requirement_id=requirement_id, status=InspectionStatus.UNVERIFIED
        )
    ]
    observations = []
    if code is GuardCode.II_UNKNOWN_OBSERVATION_REQUIREMENT:
        observations.append(
            InspectionObservation(
                observation_id="ghost-observation",
                requirement_id="ghost",
                kind="CODE",
                path="src/app.py",
            )
        )
    elif code is GuardCode.II_UNKNOWN_INSPECTION_REQUIREMENT:
        inspections.append(
            RequirementInspection(
                requirement_id="ghost", status=InspectionStatus.UNVERIFIED
            )
        )
    return _inspection_artifact_problems(
        contract,
        InspectionReport(inspections=inspections, observations=observations),
        ledger=[],
        evidence={"changed_files": []},
    )


@pytest.mark.parametrize("code", INSPECTION_IDENTITY_CODES, ids=lambda code: code.value)
def test_inspection_identity_guard_reachability(code):
    assert code in _codes(_inspection_identity_case(code))


@pytest.mark.parametrize("code", INSPECTION_IDENTITY_CODES, ids=lambda code: code.value)
def test_inspection_identity_guard_satisfiability(code):
    assert _inspection_identity_case(None) == [], (
        f"well-behaved inspection report triggered {code}"
    )
