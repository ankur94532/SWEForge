from types import SimpleNamespace

import pytest

from sweforge.workflow_agent_runtime import (
    approval_resume_for_interrupt,
    invoke_workflow_phase,
)
from sweforge.workflow_runtime import TaskPhase


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


@pytest.mark.parametrize(
    "active_after",
    [
        SimpleNamespace(
            task_run_id="task-run-A",
            phase=TaskPhase.EXECUTING,
            waiting_from_phase=None,
        ),
        None,
    ],
)
def test_post_gateway_error_is_accepted_only_after_durable_advance(active_after):
    class Runtime:
        active = SimpleNamespace(
            task_run_id="task-run-A",
            phase=TaskPhase.PLANNING,
            waiting_from_phase=None,
        )

        def active_task(self, _workflow_cycle_id):
            return self.active

    runtime = Runtime()
    before = SimpleNamespace(
        workflow_cycle_id="cycle-A",
        task_run_id="task-run-A",
        phase=TaskPhase.PLANNING,
    )
    authority = SimpleNamespace(runtime=runtime, snapshot=lambda: before)

    class Agent:
        def invoke(self, *_args, **_kwargs):
            runtime.active = active_after
            raise PermissionError("stale post-gateway phase authority")

    assert (
        invoke_workflow_phase(
            Agent(), authority=authority, thread_id="thread-A", prompt="plan"
        )
        == {}
    )


def test_pre_gateway_agent_error_still_fails_closed():
    active = SimpleNamespace(
        task_run_id="task-run-A",
        phase=TaskPhase.PLANNING,
        waiting_from_phase=None,
    )
    before = SimpleNamespace(
        workflow_cycle_id="cycle-A",
        task_run_id="task-run-A",
        phase=TaskPhase.PLANNING,
    )
    runtime = SimpleNamespace(active_task=lambda _workflow_cycle_id: active)
    authority = SimpleNamespace(runtime=runtime, snapshot=lambda: before)

    class Agent:
        def invoke(self, *_args, **_kwargs):
            raise RuntimeError("model failed before lifecycle gateway")

    with pytest.raises(RuntimeError, match="before lifecycle gateway"):
        invoke_workflow_phase(
            Agent(), authority=authority, thread_id="thread-A", prompt="plan"
        )


def test_resume_error_fails_closed_when_nothing_durably_advanced():
    """A feedback resume must not be able to swallow its own failure.

    ``resume_snapshot`` reports the synthetic phase the replayed gateway runs
    under (PLANNING for a plan-feedback review) while the durable task stays
    in WAITING_FOR_PLAN_APPROVAL. Comparing the durable phase against that
    synthetic value is unconditionally true, so any exception raised during a
    resume was reported as a completed turn and the worker re-dispatched the
    same review forever.
    """
    active = SimpleNamespace(
        task_run_id="task-run-A",
        phase=TaskPhase.WAITING_FOR_PLAN_APPROVAL,
        waiting_from_phase=None,
    )
    resume_snapshot = SimpleNamespace(
        workflow_cycle_id="cycle-A",
        task_run_id="task-run-A",
        phase=TaskPhase.PLANNING,
    )
    runtime = SimpleNamespace(active_task=lambda _workflow_cycle_id: active)
    authority = SimpleNamespace(
        runtime=runtime,
        snapshot=lambda: resume_snapshot,
        resume_snapshot=lambda _kind: resume_snapshot,
    )

    class Agent:
        def invoke(self, *_args, **_kwargs):
            raise PermissionError("tool 'submit_plan' is forbidden for PLANNING")

    with pytest.raises(PermissionError, match="forbidden"):
        invoke_workflow_phase(
            Agent(),
            authority=authority,
            thread_id="thread-A",
            prompt="",
            resume={"kind": "PLAN_FEEDBACK", "occurrence_key": "plan-approval:x"},
        )


def test_resume_error_is_still_accepted_after_a_real_durable_advance():
    """The post-gateway allowance must survive for genuine transitions."""
    state = SimpleNamespace(
        task_run_id="task-run-A",
        phase=TaskPhase.WAITING_FOR_PLAN_APPROVAL,
        waiting_from_phase=None,
    )
    resume_snapshot = SimpleNamespace(
        workflow_cycle_id="cycle-A",
        task_run_id="task-run-A",
        phase=TaskPhase.PLANNING,
    )
    runtime = SimpleNamespace(active_task=lambda _workflow_cycle_id: state)
    authority = SimpleNamespace(
        runtime=runtime,
        snapshot=lambda: resume_snapshot,
        resume_snapshot=lambda _kind: resume_snapshot,
    )

    class Agent:
        def invoke(self, *_args, **_kwargs):
            # The approval gateway committed before the agent saw the result.
            state.phase = TaskPhase.EXECUTING
            raise PermissionError("stale post-gateway phase authority")

    assert (
        invoke_workflow_phase(
            Agent(),
            authority=authority,
            thread_id="thread-A",
            prompt="",
            resume={"kind": "PLAN_APPROVAL", "occurrence_key": "plan-approval:x"},
        )
        == {}
    )
