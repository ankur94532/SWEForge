from sweforge.workflow_agent_runtime import approval_resume_for_interrupt


def test_plan_approval_cannot_consume_clarification_or_stale_occurrence():
    pending = (
        {"kind": "CLARIFICATION", "occurrence_key": "clarification:one"},
        {"kind": "PLAN_APPROVAL", "occurrence_key": "approval:task-a:v2"},
    )
    assert (
        approval_resume_for_interrupt(
            pending,
            occurrence_key="clarification:one",
            event_key="event-1",
            approved_by="owner",
            approved_at="2026-01-01T00:00:00Z",
            authorized=True,
        )
        is None
    )
    assert (
        approval_resume_for_interrupt(
            pending,
            occurrence_key="approval:task-a:v1",
            event_key="event-1",
            approved_by="owner",
            approved_at="2026-01-01T00:00:00Z",
            authorized=True,
        )
        is None
    )


def test_exact_authorized_plan_occurrence_builds_bound_resume():
    value = approval_resume_for_interrupt(
        ({"kind": "PLAN_APPROVAL", "occurrence_key": "approval:task-a:v2"},),
        occurrence_key="approval:task-a:v2",
        event_key="event-2",
        approved_by="maintainer",
        approved_at="2026-01-01T00:01:00Z",
        authorized=True,
    )
    assert value == {
        "kind": "PLAN_APPROVAL",
        "occurrence_key": "approval:task-a:v2",
        "event_key": "event-2",
        "approved_by": "maintainer",
        "approved_at": "2026-01-01T00:01:00Z",
        "authorized": True,
    }


def test_unproven_approver_never_gets_resume_value():
    assert (
        approval_resume_for_interrupt(
            ({"kind": "PLAN_APPROVAL", "occurrence_key": "approval"},),
            occurrence_key="approval",
            event_key="event",
            approved_by="reader",
            approved_at="2026-01-01T00:01:00Z",
            authorized=False,
        )
        is None
    )
