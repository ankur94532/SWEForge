"""An observation is grounded by the read ledger, not by a redundant citation.

The inspector read DiscountPolicy.java and PricingCalculator.java -- the read
ledger records both -- and made CODE observations about them. The guard
refused those because the model had not ALSO emitted an INSPECTED_FILE ref
pointing at reads the ledger already proves. The ledger is the trusted record,
so grounding against it directly is stronger than trusting a model-supplied
reference.
"""

from sweforge.reviewer import (
    InspectionObservation,
    InspectionStatus,
    RequirementInspection,
    _inspection_authority_problems,
)

PATH = "src/main/java/com/sweforge/pricing/DiscountPolicy.java"
REQ = {"requirement_id": "plan:step:1", "classification": "BEHAVIORAL", "text": "x"}


def _observation(path=PATH):
    return InspectionObservation(
        observation_id="obs-1",
        requirement_id="plan:step:1",
        kind="CODE",
        path=path,
        start_line=2,
        end_line=11,
        fact="the threshold branch is present",
        assertion_or_signal="",
    )


def _inspection():
    return RequirementInspection(
        requirement_id="plan:step:1",
        status=InspectionStatus.VERIFIED,
        concise_summary="checked",
        evidence_refs=[],
    )


def _problems(*, ledger, changed):
    return [
        item.code.value
        for item in _inspection_authority_problems(
            _inspection(),
            requirement=REQ,
            observations={"obs-1": _observation()},
            ledger_by_id=ledger,
            changed_files=changed,
            has_execution=False,
            refs=[],
        )
    ]


def test_a_read_in_the_ledger_grounds_the_observation():
    ledger = {
        "read:1": {
            "read_id": "read:1",
            "normalized_path": PATH,
            "returned_lines": [2, 11],
        }
    }
    assert "IA-UNGROUNDED-OBSERVATION" not in _problems(ledger=ledger, changed=set())


def test_a_changed_file_still_grounds_the_observation():
    assert "IA-UNGROUNDED-OBSERVATION" not in _problems(ledger={}, changed={PATH})


def test_an_unread_unchanged_file_is_still_ungrounded():
    """Positive control: the guard must still catch an observation about a
    file the inspector never read. That is the authority violation it exists
    to prevent, and consulting the ledger must not disable it."""
    ledger = {
        "read:1": {
            "read_id": "read:1",
            "normalized_path": "src/other/Thing.java",
            "returned_lines": [2, 11],
        }
    }
    assert "IA-UNGROUNDED-OBSERVATION" in _problems(ledger=ledger, changed=set())


def test_an_empty_ledger_and_empty_diff_is_ungrounded():
    assert "IA-UNGROUNDED-OBSERVATION" in _problems(ledger={}, changed=set())


def test_a_read_outside_the_cited_lines_does_not_ground_it():
    """Positive control on the range: an observation about lines 2-11 cannot
    rest on a read of lines 40-50. Grounding on the path alone would let an
    inspector assert facts about lines it never saw."""
    ledger = {
        "read:1": {
            "read_id": "read:1",
            "normalized_path": PATH,
            "returned_lines": [40, 50],
        }
    }
    assert "IA-UNGROUNDED-OBSERVATION" in _problems(ledger=ledger, changed=set())


def test_a_read_with_no_recorded_range_grounds_nothing():
    ledger = {"read:1": {"read_id": "read:1", "normalized_path": PATH}}
    assert "IA-UNGROUNDED-OBSERVATION" in _problems(ledger=ledger, changed=set())
