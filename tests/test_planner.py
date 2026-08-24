import pytest
from pydantic import ValidationError

from sweforge.planner import (
    MAX_PLAN_CHARS,
    MAX_STEP_CHARS,
    PlanResult,
    render_plan,
    validate_canonical_plan_text,
)


def test_render_plan_preserves_complete_items():
    result = PlanResult(
        summary="Implement the requested change",
        steps=["Inspect the relevant module", "Add focused regression coverage"],
        validation=["Run the focused test suite"],
    )

    rendered = render_plan(result)

    assert "Inspect the relevant module" in rendered
    assert "Add focused regression coverage" in rendered
    assert "Run the focused test suite" in rendered


def test_render_plan_preserves_item_at_exact_boundary():
    item = "x" * MAX_STEP_CHARS

    rendered = render_plan(PlanResult(summary="summary", steps=[item]))

    assert item in rendered


def test_oversized_step_is_rejected_before_rendering():
    with pytest.raises(ValidationError):
        PlanResult(summary="summary", steps=["x" * (MAX_STEP_CHARS + 1)])


def test_oversized_validation_item_is_rejected_before_rendering():
    with pytest.raises(ValidationError):
        PlanResult(
            summary="summary",
            steps=["valid"],
            validation=["x" * (MAX_STEP_CHARS + 1)],
        )


def test_total_plan_oversize_is_rejected_without_partial_rendering():
    result = PlanResult(
        summary="summary",
        steps=["step " + ("x" * (MAX_STEP_CHARS - 5))] * 20,
        validation=["check " + ("x" * (MAX_STEP_CHARS - 6))] * 10,
    )

    with pytest.raises(ValueError, match="canonical plan exceeds"):
        render_plan(result)


def test_issue_14_shape_cannot_create_mid_requirement_prefix():
    result = PlanResult(
        summary="Add focused boundary tests",
        steps=[
            "Confirm threshold logic",
            "Edit the test file to add three tests",
            "Leave production code files untouched and use Item(1" + "x" * 443,
        ],
        validation=["Run the focused Maven test"],
    )

    rendered = render_plan(result)

    assert "Item(1" + "x" * 443 in rendered
    assert len(rendered) < MAX_PLAN_CHARS


def test_render_plan_rejects_blank_structured_step():
    with pytest.raises(ValidationError):
        PlanResult(summary="summary", steps=["   "])


def test_canonical_plan_text_rejects_oversized_workflow_input():
    with pytest.raises(ValueError, match="canonical plan exceeds"):
        validate_canonical_plan_text("x" * (MAX_PLAN_CHARS + 1))
