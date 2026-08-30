"""Root-only lifecycle gateway tools for the generic workflow agent."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool
from langgraph.types import interrupt

from .agent_trace import AgentTracer, TraceContext
from .github_models import InteractionMode
from .workflow_runtime import TaskPhase, TaskRun, ValidationVerdict, WorkflowRuntime


def build_lifecycle_tools(
    *,
    runtime: WorkflowRuntime,
    workflow_cycle_id: str,
    publish_plan: Callable[..., tuple[int, str]],
    publish_result: Callable[..., tuple[int, str]],
    execution_evidence: Callable[[], list[dict[str, Any]]] | None = None,
    validation_evidence: Callable[[], list[dict[str, Any]]] | None = None,
    tracer: AgentTracer | None = None,
    trace_context: Callable[[TaskRun], TraceContext] | None = None,
) -> list[Any]:
    """Build gateways bound to one authoritative cycle.

    ``submit_plan`` publishes before changing state and uses a native interrupt.
    LangGraph resumes tool functions from their start, so the matching posted
    plan is reconciled rather than versioned again on replay.
    """

    def context(task: TaskRun) -> TraceContext:
        if trace_context is not None:
            try:
                return trace_context(task)
            except Exception:
                pass
        return TraceContext(
            thread_id=task.thread_id,
            workflow_cycle_id=task.workflow_cycle_id,
            cycle_id=task.cycle_id,
            task_id=task.task_id,
            task_run_id=task.task_run_id,
            phase=task.phase.value,
        )

    def trace_transition(before: TaskRun) -> TaskRun:
        if tracer is None:
            return before
        try:
            after = runtime.task(before.task_run_id)
        except Exception:
            return before
        tracer.transition(context(before), before.phase, after.phase)
        return after

    @tool
    def submit_plan(plan_text: str) -> str:
        """Publish the canonical plan and wait for exact authorized approval."""
        task = runtime.active_task(workflow_cycle_id)
        if task is None:
            raise PermissionError("workflow has no active task")
        digest = hashlib.sha256(plan_text.strip().encode()).hexdigest()
        plan = None
        if task.current_plan_id:
            current = runtime.plan(task.current_plan_id)
            if current.plan_digest == digest and task.phase in (
                TaskPhase.WAITING_FOR_PLAN_APPROVAL,
                TaskPhase.EXECUTING,
            ):
                plan = current
        if plan is None:
            if task.phase != TaskPhase.PLANNING:
                raise PermissionError("only planning may submit a new plan")
            # The callback owns deterministic GitHub marker reconciliation.
            comment_id, posted_at = publish_plan(
                task_run_id=task.task_run_id,
                task_id=task.task_id,
                plan_text=plan_text,
            )
            plan = runtime.submit_posted_plan(
                task_run_id=task.task_run_id,
                plan_text=plan_text,
                posted_comment_id=comment_id,
                posted_at=posted_at,
            )
            task = trace_transition(task)
        if runtime.store.interaction_mode(task.thread_id) == InteractionMode.AUTO:
            waiting = runtime.task(task.task_run_id)
            if waiting.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL:
                runtime.auto_authorize_plan(task.task_run_id)
                if tracer is not None:
                    tracer.authorization(context(waiting), "AUTO", "plan")
                trace_transition(waiting)
            return "Exact AUTO plan authorization recorded. Continue in EXECUTING."
        approval = interrupt(
            {
                "kind": "PLAN_APPROVAL",
                "occurrence_key": plan.approval_occurrence_key,
                "workflow_cycle_id": workflow_cycle_id,
                "task_run_id": task.task_run_id,
                "task_id": task.task_id,
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "plan_digest": plan.plan_digest,
            }
        )
        if isinstance(approval, dict) and approval.get("kind") == "PLAN_FEEDBACK":
            if approval.get("occurrence_key") != plan.approval_occurrence_key:
                raise PermissionError("plan feedback occurrence is stale")
            before = task
            runtime.replan_from_feedback(task.task_run_id)
            trace_transition(before)
            return (
                "Plan feedback received. Replan the same task and call submit_plan "
                f"again. Feedback: {str(approval.get('feedback') or '')[:2_000]}"
            )
        if not isinstance(approval, dict) or approval.get("kind") != "PLAN_APPROVAL":
            raise PermissionError("plan approval resume payload is invalid")
        before = task
        runtime.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=str(approval.get("occurrence_key") or ""),
            approval_event_key=str(approval.get("event_key") or ""),
            approved_by=str(approval.get("approved_by") or ""),
            approval_is_authorized=approval.get("authorized") is True,
            approval_occurred_at=str(approval.get("approved_at") or ""),
        )
        if tracer is not None:
            tracer.authorization(
                context(before),
                "HUMAN",
                "plan",
                str(approval.get("approved_by") or ""),
            )
        trace_transition(before)
        return "Exact plan approval accepted. Continue in EXECUTING."

    @tool
    def finish_execution(summary: str, evidence: dict[str, Any]) -> str:
        """Finish implementation work; this enters validation, never DONE."""
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("execution evidence is required")
        task = runtime.active_task(workflow_cycle_id)
        if task is None:
            raise PermissionError("workflow has no active task")
        captured = execution_evidence() if execution_evidence is not None else []
        before = task
        runtime.finish_execution(
            task.task_run_id,
            summary=summary,
            evidence={"reported": evidence, "tool_observations": captured},
        )
        trace_transition(before)
        return "Execution recorded. Continue in VALIDATING."

    @tool
    def finish_validation(
        verdict: str,
        summary: str,
        findings: list[dict[str, Any]],
        repair_instructions: list[str],
        evidence: dict[str, Any],
    ) -> str:
        """Submit guarded validation evidence and request a legal transition."""
        task = runtime.active_task(workflow_cycle_id)
        if task is None:
            raise PermissionError("workflow has no active task")
        parsed_verdict = ValidationVerdict(verdict)
        if tracer is not None:
            tracer.emit("VALIDATION", parsed_verdict.value, context(task))
        if task.phase == TaskPhase.VALIDATING:
            captured = validation_evidence() if validation_evidence is not None else []
            if validation_evidence is not None and not captured:
                raise ValueError("run_validation evidence is required")
            before = task
            result = runtime.finish_validation(
                task_run_id=task.task_run_id,
                verdict=parsed_verdict,
                summary=summary,
                findings=findings,
                repair_instructions=repair_instructions,
                evidence={"reported": evidence, "validation_runs": captured},
            )
            trace_transition(before)
            if parsed_verdict != ValidationVerdict.ACCEPT:
                return f"Validation recorded. Task phase is now {result.phase.value}."
            comment_id, posted_at = publish_result(
                task_run_id=task.task_run_id,
                task_id=task.task_id,
            )
            before = task
            accepted_result = runtime.publish_validated_result(
                task_run_id=task.task_run_id,
                posted_comment_id=comment_id,
                posted_at=posted_at,
            )
            trace_transition(before)
        elif task.phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL:
            accepted_result = runtime.current_result(task.task_run_id)
            if accepted_result is None:
                raise RuntimeError("waiting task has no exact result")
        else:
            raise PermissionError("only validation may submit a result")
        if runtime.store.interaction_mode(task.thread_id) == InteractionMode.AUTO:
            before = runtime.task(task.task_run_id) if tracer is not None else task
            runtime.auto_accept_result(task.task_run_id)
            if tracer is not None:
                tracer.authorization(context(before), "AUTO", "result")
            trace_transition(before)
            return "Exact AUTO result acceptance recorded. Task is DONE."
        approval = interrupt(
            {
                "kind": "RESULT_APPROVAL",
                "occurrence_key": accepted_result.result_occurrence_key,
                "workflow_cycle_id": workflow_cycle_id,
                "task_run_id": task.task_run_id,
                "task_id": task.task_id,
                "plan_id": accepted_result.plan_id,
                "execution_id": accepted_result.execution_id,
                "validation_id": accepted_result.validation_id,
                "result_id": accepted_result.result_id,
            }
        )
        if isinstance(approval, dict) and approval.get("kind") == "RESULT_FEEDBACK":
            if approval.get("occurrence_key") != accepted_result.result_occurrence_key:
                raise PermissionError("result feedback occurrence is stale")
            before = task
            runtime.replan_from_result_feedback(
                task_run_id=task.task_run_id,
                event_key=str(approval.get("event_key") or ""),
                feedback=str(approval.get("feedback") or ""),
            )
            trace_transition(before)
            return "Result feedback recorded. Replan the same cumulative task."
        if not isinstance(approval, dict) or approval.get("kind") != "RESULT_APPROVAL":
            raise PermissionError("result approval resume payload is invalid")
        before = task
        runtime.approve_result(
            task_run_id=task.task_run_id,
            occurrence_key=str(approval.get("occurrence_key") or ""),
            approval_event_key=str(approval.get("event_key") or ""),
            approved_by=str(approval.get("approved_by") or ""),
            approval_is_authorized=approval.get("authorized") is True,
            approval_occurred_at=str(approval.get("approved_at") or ""),
        )
        if tracer is not None:
            tracer.authorization(
                context(before),
                "HUMAN",
                "result",
                str(approval.get("approved_by") or ""),
            )
        trace_transition(before)
        return "Exact validated result approval accepted. Task is DONE."

    @tool
    def request_clarification(
        question: str,
        reason: str,
        answer_type: str = "TEXT",
        choices: list[str] | None = None,
    ) -> str:
        """Pause the same active task to request narrowly scoped human input."""
        task = runtime.active_task(workflow_cycle_id)
        if task is None:
            raise PermissionError("workflow has no active task")
        normalized = answer_type.upper()
        if normalized not in {"TEXT", "VALUE", "BOOLEAN", "CHOICE"}:
            raise ValueError("unsupported clarification answer type")
        normalized_choices = tuple(choices or ())
        if normalized == "CHOICE" and not normalized_choices:
            raise ValueError("CHOICE clarification requires choices")
        occurrence = task.clarification_occurrence_key
        if occurrence is None:
            suffix = hashlib.sha256(
                f"{task.task_run_id}\0{task.phase.value}\0{question}\0{reason}".encode()
            ).hexdigest()[:24]
            occurrence = f"clarification:{task.task_run_id}:{suffix}"
        if task.phase != TaskPhase.WAITING_FOR_INPUT:
            before = task
            runtime.pause_for_clarification(
                task_run_id=task.task_run_id, occurrence_key=occurrence
            )
            trace_transition(before)
        response = interrupt(
            {
                "kind": "CLARIFICATION",
                "occurrence_key": occurrence,
                "workflow_cycle_id": workflow_cycle_id,
                "task_run_id": task.task_run_id,
                "task_id": task.task_id,
                "question": question[:2_000],
                "reason": reason[:2_000],
                "answer_type": normalized,
                "choices": normalized_choices,
            }
        )
        if (
            not isinstance(response, dict)
            or response.get("kind") != "CLARIFICATION_RESPONSE"
            or response.get("occurrence_key") != occurrence
        ):
            raise PermissionError("clarification resume payload is invalid")
        before = task
        runtime.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key=occurrence
        )
        trace_transition(before)
        return f"Clarification answer received: {response.get('answer', '')}"

    return [
        submit_plan,
        finish_execution,
        finish_validation,
        request_clarification,
    ]
