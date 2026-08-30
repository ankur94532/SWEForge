"""Invocation/resume protocol for the durable workflow-owning agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.types import Command

from .agent import pending_interrupt_values
from .agent_trace import AgentTraceCallbackHandler, AgentTracer, TraceContext
from .workflow_middleware import WorkflowAuthority
from .workflow_runtime import TaskPhase

MAX_PROTOCOL_NUDGES = 2


class WorkflowProtocolError(RuntimeError):
    """The model stopped without using the required lifecycle gateway."""


def _phase_advanced(authority: WorkflowAuthority, before: Any) -> bool:
    active = authority.runtime.active_task(before.workflow_cycle_id)
    if active is None or active.task_run_id != before.task_run_id:
        return True
    phase = (
        active.waiting_from_phase
        if active.phase == TaskPhase.WAITING_FOR_INPUT
        else active.phase
    )
    return phase != before.phase


def approval_resume_for_interrupt(
    pending: tuple[dict[str, Any], ...],
    *,
    occurrence_key: str,
    event_key: str,
    approved_by: str,
    approved_at: str,
    authorized: bool,
) -> dict[str, Any] | None:
    """Build a resume only for an exact pending approval occurrence."""
    matches = [
        item
        for item in pending
        if item.get("kind") in {"PLAN_APPROVAL", "RESULT_APPROVAL"}
        and item.get("occurrence_key") == occurrence_key
    ]
    if len(matches) != 1 or not authorized:
        return None
    return {
        "kind": str(matches[0]["kind"]),
        "occurrence_key": occurrence_key,
        "event_key": event_key,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "authorized": True,
    }


def invoke_workflow_phase(
    agent: Any,
    *,
    authority: WorkflowAuthority,
    thread_id: str,
    prompt: str,
    context: Any = None,
    resume: Mapping[str, Any] | None = None,
    trace_callback: AgentTraceCallbackHandler | None = None,
    tracer: AgentTracer | None = None,
    trace_context: TraceContext | None = None,
) -> dict[str, Any]:
    """Invoke one phase and fail closed on natural-language completion."""
    config = {"configurable": {"thread_id": thread_id}}
    if trace_callback is not None:
        config["callbacks"] = [trace_callback]
    before = (
        authority.resume_snapshot(str(resume.get("kind") or ""))
        if resume is not None
        else authority.snapshot()
    )
    state: Any = (
        Command(resume=dict(resume))
        if resume is not None
        else {"messages": [{"role": "user", "content": prompt}]}
    )
    if resume is not None and tracer is not None:
        tracer.resume(
            trace_context or TraceContext(thread_id=thread_id),
            str(resume.get("kind") or "UNKNOWN"),
            str(resume.get("occurrence_key") or "unknown"),
        )
    for attempt in range(MAX_PROTOCOL_NUDGES + 1):
        try:
            result = agent.invoke(
                state,
                config=config,
                durability="sync",
                context=context,
            )
        except Exception as exc:
            # A lifecycle gateway commits its transition before the agent sees
            # the tool result. The next model turn therefore runs under stale
            # phase authority and may fail closed. Once durable state proves
            # the exact task advanced, that post-gateway error must not turn a
            # successful transition into a dispatcher failure.
            if _phase_advanced(authority, before):
                return {}
            if tracer is not None:
                tracer.emit(
                    "AGENT ERROR",
                    f"{type(exc).__name__}: {exc}",
                    trace_context or TraceContext(thread_id=thread_id),
                )
            raise
        if result.get("__interrupt__"):
            if tracer is not None:
                for item in result["__interrupt__"]:
                    payload = getattr(item, "value", item)
                    if not isinstance(payload, Mapping):
                        continue
                    tracer.interrupt(
                        trace_context or TraceContext(thread_id=thread_id),
                        str(payload.get("kind") or "UNKNOWN"),
                        str(payload.get("occurrence_key") or "unknown"),
                    )
            return result
        if _phase_advanced(authority, before):
            return result
        if attempt == MAX_PROTOCOL_NUDGES:
            raise WorkflowProtocolError(
                f"agent stopped in {before.phase.value} without its lifecycle gateway"
            )
        state = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Protocol correction: the phase is still active. Use the "
                        "required lifecycle gateway after completing phase work."
                    ),
                }
            ]
        }
    raise AssertionError("unreachable")


def pending_plan_approval_interrupts(agent: Any, thread_id: str):
    """Expose structural pending values for exact GitHub event routing."""
    values = pending_interrupt_values(agent, {"configurable": {"thread_id": thread_id}})
    return tuple(item for item in values if item.get("kind") == "PLAN_APPROVAL")
