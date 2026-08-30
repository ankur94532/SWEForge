from sweforge.github_store import AcceptedTaskLifecycle
from sweforge.lifecycle_context import (
    MAX_ACCEPTED_HISTORY_CHARS,
    lifecycle_columns,
    render_accepted_lifecycle,
)


def _item(cycle_id: int, task_id: str) -> AcceptedTaskLifecycle:
    return AcceptedTaskLifecycle(
        cycle_id=cycle_id,
        workflow_cycle_id=f"cycle-{cycle_id}",
        cycle_kind="INITIAL" if cycle_id == 1 else "REVISION",
        revision_sequence=None if cycle_id == 1 else cycle_id - 1,
        task_id=task_id,
        declaration_index=0,
        plan_id=f"plan-{cycle_id}",
        plan_text=f"plan {cycle_id} " + "p" * 5_000,
        execution_id=f"execution-{cycle_id}",
        execution_summary=f"execution {cycle_id} " + "e" * 3_000,
        validation_id=f"validation-{cycle_id}",
        validation_summary=f"validation {cycle_id} " + "v" * 3_000,
    )


def test_accepted_history_bounds_are_deterministic_and_keep_first_and_latest():
    material = [_item(index, f"task-{index}") for index in range(1, 30)]

    first = render_accepted_lifecycle(material)
    second = render_accepted_lifecycle(material)

    assert first == second
    assert len(first) <= MAX_ACCEPTED_HISTORY_CHARS
    assert "task-1" in first
    assert "task-29" in first
    assert "deterministically truncated" in first

    plans, executions, validations = lifecycle_columns(material)
    for rendered in (plans, executions, validations):
        assert len(rendered) <= 20_000
        assert "task-1" in rendered
        assert "task-29" in rendered
