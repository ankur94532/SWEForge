"""Root-only lifecycle gateway tools for the generic workflow agent."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool
from langgraph.types import interrupt

from .workflow_runtime import TaskPhase, ValidationVerdict, WorkflowRuntime


def build_lifecycle_tools(
    *,
    runtime: WorkflowRuntime,
    workflow_cycle_id: str,
    publish_plan: Callable[..., tuple[int, str]],
) -> list[Any]:
    """Build gateways bound to one authoritative cycle.

    ``submit_plan`` publishes before changing state and uses a native interrupt.
    LangGraph resumes tool functions from their start, so the matching posted
    plan is reconciled rather than versioned again on replay.
    """

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
                TaskPhase.WAITING_FOR_APPROVAL,
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
        if not isinstance(approval, dict) or approval.get("kind") != "PLAN_APPROVAL":
            raise PermissionError("plan approval resume payload is invalid")
        runtime.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=str(approval.get("occurrence_key") or ""),
            approval_event_key=str(approval.get("event_key") or ""),
            approved_by=str(approval.get("approved_by") or ""),
            approval_is_authorized=approval.get("authorized") is True,
            approval_occurred_at=str(approval.get("approved_at") or ""),
        )
        return "Exact plan approval accepted. Continue in EXECUTING."

    @tool
    def finish_execution(summary: str, evidence: dict[str, Any]) -> str:
        """Finish implementation work; this enters validation, never DONE."""
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("execution evidence is required")
        task = runtime.active_task(workflow_cycle_id)
        if task is None:
            raise PermissionError("workflow has no active task")
        runtime.finish_execution(task.task_run_id, summary=summary, evidence=evidence)
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
        result = runtime.finish_validation(
            task_run_id=task.task_run_id,
            verdict=ValidationVerdict(verdict),
            summary=summary,
            findings=findings,
            repair_instructions=repair_instructions,
            evidence=evidence,
        )
        return f"Validation recorded. Task phase is now {result.phase.value}."

    return [submit_plan, finish_execution, finish_validation]
