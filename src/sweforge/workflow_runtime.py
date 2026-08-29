"""Transactional generic task lifecycle and strict serial scheduler."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .workflow_spec import PhaseSpec, TaskSpec, WorkflowSpec


class TaskPhase(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    DONE = "DONE"
    FAILED = "FAILED"


class WorkflowCycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    AWAITING_PUBLICATION = "AWAITING_PUBLICATION"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ValidationVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    NEEDS_FIXES = "NEEDS_FIXES"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class WorkflowCycle:
    workflow_cycle_id: str
    thread_id: str
    cycle_id: int
    root_input_id: str
    workflow_id: str
    workflow_version: int
    workflow_digest: str
    status: WorkflowCycleStatus
    active_task_id: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class TaskRun:
    task_run_id: str
    workflow_cycle_id: str
    thread_id: str
    cycle_id: int
    workflow_id: str
    task_id: str
    declaration_index: int
    dependencies: tuple[str, ...]
    status: TaskPhase
    phase: TaskPhase
    current_plan_id: str | None
    execution_attempt: int
    validation_round: int
    repair_feedback: tuple[dict[str, Any], ...]
    failure_reason: str | None
    waiting_from_phase: TaskPhase | None
    clarification_occurrence_key: str | None


@dataclass(frozen=True, slots=True)
class TaskPlan:
    plan_id: str
    task_run_id: str
    task_id: str
    version: int
    plan_text: str
    plan_digest: str
    status: str
    posted_at: str
    posted_comment_id: int
    approval_occurrence_key: str
    approved_at: str | None
    approved_by: str | None
    approval_event_key: str | None


@dataclass(frozen=True, slots=True)
class TaskPermit:
    permit_id: str
    task_run_id: str
    workflow_cycle_id: str
    plan_id: str
    plan_version: int
    plan_digest: str
    approval_event_key: str
    approved_by: str
    created_at: str
    invalidated_at: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _stable(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(material.encode()).hexdigest()[:24]}"


def task_run_id_for(
    *, thread_id: str, cycle_id: int, workflow_id: str, task_id: str
) -> str:
    return _stable("task-run", thread_id, cycle_id, workflow_id, task_id)


def workflow_cycle_id_for(*, thread_id: str, cycle_id: int, workflow_id: str) -> str:
    return _stable("workflow-cycle", thread_id, cycle_id, workflow_id)


class WorkflowRuntime:
    """Application-owned workflow authority over a ``SQLiteGitHubStore``.

    Model output never selects tasks or writes phase fields. Every transition is
    checked and committed under ``BEGIN IMMEDIATE``.
    """

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], str] = _now,
        validation_guard: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.db: sqlite3.Connection = store.connection
        self.clock = clock
        self.validation_guard = validation_guard

    def initialize_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_input_id: str,
        spec: WorkflowSpec,
        spec_ref: str | None = None,
    ) -> WorkflowCycle:
        identity = workflow_cycle_id_for(
            thread_id=thread_id, cycle_id=cycle_id, workflow_id=spec.workflow_id
        )
        timestamp = self.clock()
        canonical = json.dumps(
            spec.canonical_document(), sort_keys=True, separators=(",", ":")
        )
        with self.store.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM workflow_cycles_v1 WHERE thread_id=? AND cycle_id=?",
                (thread_id, cycle_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["workflow_cycle_id"] != identity
                    or existing["workflow_digest"] != spec.digest
                    or existing["root_input_id"] != root_input_id
                ):
                    raise ValueError("workflow cycle identity/specification mismatch")
                return self._cycle(existing)
            db.execute(
                """INSERT INTO workflow_cycles_v1(
                   workflow_cycle_id,thread_id,cycle_id,root_input_id,workflow_id,
                   workflow_version,workflow_digest,workflow_spec_json,
                   workflow_spec_ref,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity,
                    thread_id,
                    cycle_id,
                    root_input_id,
                    spec.workflow_id,
                    spec.version,
                    spec.digest,
                    canonical,
                    spec_ref,
                    WorkflowCycleStatus.ACTIVE.value,
                    timestamp,
                    timestamp,
                ),
            )
            for index, task in enumerate(spec.tasks):
                task_run_id = task_run_id_for(
                    thread_id=thread_id,
                    cycle_id=cycle_id,
                    workflow_id=spec.workflow_id,
                    task_id=task.id,
                )
                db.execute(
                    """INSERT INTO workflow_task_runs_v1(
                       task_run_id,workflow_cycle_id,thread_id,cycle_id,workflow_id,
                       task_id,declaration_index,dependencies_json,status,phase,
                       created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_run_id,
                        identity,
                        thread_id,
                        cycle_id,
                        spec.workflow_id,
                        task.id,
                        index,
                        json.dumps(task.depends_on),
                        TaskPhase.PENDING.value,
                        TaskPhase.PENDING.value,
                        timestamp,
                        timestamp,
                    ),
                )
        return self.cycle(identity)

    def cycle(self, workflow_cycle_id: str) -> WorkflowCycle:
        row = self.db.execute(
            "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
            (workflow_cycle_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown workflow cycle")
        return self._cycle(row)

    def task_runs(self, workflow_cycle_id: str) -> tuple[TaskRun, ...]:
        rows = self.db.execute(
            """SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?
               ORDER BY declaration_index""",
            (workflow_cycle_id,),
        ).fetchall()
        return tuple(self._task(row) for row in rows)

    def active_task(self, workflow_cycle_id: str) -> TaskRun | None:
        cycle = self.cycle(workflow_cycle_id)
        if cycle.active_task_id is None:
            return None
        row = self.db.execute(
            """SELECT * FROM workflow_task_runs_v1
               WHERE workflow_cycle_id=? AND task_id=?""",
            (workflow_cycle_id, cycle.active_task_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("active task pointer is corrupt")
        return self._task(row)

    def select_active_task(self, workflow_cycle_id: str) -> TaskRun | None:
        """Select one ready task in declaration order, or retain the owner."""
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            cycle = db.execute(
                "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
                (workflow_cycle_id,),
            ).fetchone()
            if cycle is None:
                raise ValueError("unknown workflow cycle")
            if cycle["status"] != WorkflowCycleStatus.ACTIVE.value:
                return None
            if cycle["active_task_id"] is not None:
                row = db.execute(
                    """SELECT * FROM workflow_task_runs_v1
                       WHERE workflow_cycle_id=? AND task_id=?""",
                    (workflow_cycle_id, cycle["active_task_id"]),
                ).fetchone()
                if row is None:
                    raise RuntimeError("active task pointer is corrupt")
                return self._task(row)
            rows = db.execute(
                """SELECT * FROM workflow_task_runs_v1
                   WHERE workflow_cycle_id=? ORDER BY declaration_index""",
                (workflow_cycle_id,),
            ).fetchall()
            statuses = {row["task_id"]: row["status"] for row in rows}
            selected = next(
                (
                    row
                    for row in rows
                    if row["status"] == TaskPhase.PENDING.value
                    and all(
                        statuses.get(dep) == TaskPhase.DONE.value
                        for dep in json.loads(row["dependencies_json"])
                    )
                ),
                None,
            )
            if selected is None:
                if rows and all(row["status"] == TaskPhase.DONE.value for row in rows):
                    db.execute(
                        """UPDATE workflow_cycles_v1 SET status=?,updated_at=?
                           WHERE workflow_cycle_id=? AND active_task_id IS NULL""",
                        (
                            WorkflowCycleStatus.AWAITING_PUBLICATION.value,
                            timestamp,
                            workflow_cycle_id,
                        ),
                    )
                return None
            if (
                db.execute(
                    """UPDATE workflow_cycles_v1 SET active_task_id=?,updated_at=?
                   WHERE workflow_cycle_id=? AND active_task_id IS NULL
                     AND status=?""",
                    (
                        selected["task_id"],
                        timestamp,
                        workflow_cycle_id,
                        WorkflowCycleStatus.ACTIVE.value,
                    ),
                ).rowcount
                != 1
            ):
                raise RuntimeError("concurrent active task selection rejected")
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,updated_at=?
                   WHERE task_run_id=? AND status=?""",
                (
                    TaskPhase.PLANNING.value,
                    TaskPhase.PLANNING.value,
                    timestamp,
                    selected["task_run_id"],
                    TaskPhase.PENDING.value,
                ),
            )
        return self.active_task(workflow_cycle_id)

    def phase_spec(self, spec: WorkflowSpec, task: TaskRun) -> PhaseSpec:
        task_spec: TaskSpec = spec.task_map[task.task_id]
        phase = (
            task.waiting_from_phase
            if task.phase == TaskPhase.WAITING_FOR_INPUT
            else task.phase
        )
        if phase == TaskPhase.PLANNING:
            return task_spec.planning
        if phase == TaskPhase.EXECUTING:
            return task_spec.execution
        if phase == TaskPhase.VALIDATING:
            return task_spec.validation
        raise ValueError(f"phase {task.phase} has no runnable policy")

    def submit_posted_plan(
        self,
        *,
        task_run_id: str,
        plan_text: str,
        posted_comment_id: int,
        posted_at: str,
    ) -> TaskPlan:
        """Bind a visible plan and pause; publication must already have succeeded."""
        if not isinstance(posted_comment_id, int) or posted_comment_id <= 0:
            raise ValueError("a reconciled GitHub plan comment is required")
        if not isinstance(plan_text, str) or not plan_text.strip():
            raise ValueError("canonical plan must not be blank")
        if len(plan_text) > 12_000:
            raise ValueError("canonical plan exceeds the 12000-character limit")
        canonical = plan_text.strip()
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.PLANNING)
            version = int(
                db.execute(
                    """SELECT COALESCE(MAX(version),0)+1 FROM workflow_task_plans_v1
                       WHERE task_run_id=?""",
                    (task_run_id,),
                ).fetchone()[0]
            )
            plan_id = _stable("task-plan", task_run_id, version, digest)
            occurrence = f"plan-approval:{task_run_id}:{plan_id}:v{version}:{digest}"
            db.execute(
                """UPDATE workflow_task_plans_v1 SET status='SUPERSEDED'
                   WHERE task_run_id=? AND status IN ('POSTED','APPROVED')""",
                (task_run_id,),
            )
            db.execute(
                """UPDATE workflow_task_permits_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """INSERT INTO workflow_task_plans_v1(
                   plan_id,task_run_id,workflow_cycle_id,task_id,version,plan_text,
                   plan_digest,status,posted_at,posted_comment_id,
                   approval_occurrence_key,created_at)
                   VALUES(?,?,?,?,?,?,?,'POSTED',?,?,?,?)""",
                (
                    plan_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    task.task_id,
                    version,
                    canonical,
                    digest,
                    posted_at,
                    posted_comment_id,
                    occurrence,
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1
                   SET status=?,phase=?,current_plan_id=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.WAITING_FOR_APPROVAL.value,
                    TaskPhase.WAITING_FOR_APPROVAL.value,
                    plan_id,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.plan(plan_id)

    def approve_plan(
        self,
        *,
        task_run_id: str,
        occurrence_key: str,
        approval_event_key: str,
        approved_by: str,
        approval_is_authorized: bool,
        approval_occurred_at: str,
    ) -> TaskPermit:
        if not approval_is_authorized:
            raise PermissionError("approver permission could not be proven")
        if not approval_event_key or not approved_by:
            raise ValueError("human approval identity is required")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.WAITING_FOR_APPROVAL)
            plan = db.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task.current_plan_id,),
            ).fetchone()
            if plan is None or plan["status"] != "POSTED":
                raise ValueError("current posted plan is missing")
            if plan["approval_occurrence_key"] != occurrence_key:
                raise ValueError("approval interrupt occurrence is stale")
            if approval_occurred_at <= plan["posted_at"]:
                raise ValueError("approval predates the visible plan")
            permit_id = _stable(
                "task-permit",
                task_run_id,
                plan["plan_id"],
                plan["version"],
                plan["plan_digest"],
                approval_event_key,
            )
            db.execute(
                """UPDATE workflow_task_plans_v1 SET status='APPROVED',
                   approved_at=?,approved_by=?,approval_event_key=? WHERE plan_id=?""",
                (
                    approval_occurred_at,
                    approved_by,
                    approval_event_key,
                    plan["plan_id"],
                ),
            )
            db.execute(
                """INSERT OR IGNORE INTO workflow_task_permits_v1(
                   permit_id,task_run_id,workflow_cycle_id,plan_id,plan_version,
                   plan_digest,approval_event_key,approved_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    permit_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    plan["plan_id"],
                    plan["version"],
                    plan["plan_digest"],
                    approval_event_key,
                    approved_by,
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.EXECUTING.value,
                    TaskPhase.EXECUTING.value,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.permit(permit_id)

    def replan_from_feedback(self, task_run_id: str) -> TaskRun:
        """Invalidate approval and return the same waiting owner to planning."""
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.WAITING_FOR_APPROVAL)
            db.execute(
                """UPDATE workflow_task_plans_v1 SET status='SUPERSEDED'
                   WHERE plan_id=?""",
                (task.current_plan_id,),
            )
            db.execute(
                """UPDATE workflow_task_permits_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.PLANNING.value,
                    TaskPhase.PLANNING.value,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.task(task_run_id)

    def assert_execution_authorized(self, task_run_id: str) -> TaskPermit:
        task = self.task(task_run_id)
        if task.phase != TaskPhase.EXECUTING:
            raise PermissionError("task is not executing")
        cycle = self.cycle(task.workflow_cycle_id)
        if (
            cycle.active_task_id != task.task_id
            or cycle.status != WorkflowCycleStatus.ACTIVE
        ):
            raise PermissionError("task does not own the active workflow")
        plan = self.plan(task.current_plan_id or "")
        row = self.db.execute(
            """SELECT * FROM workflow_task_permits_v1 WHERE task_run_id=?
               AND plan_id=? AND plan_version=? AND plan_digest=?
               AND invalidated_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            (task_run_id, plan.plan_id, plan.version, plan.plan_digest),
        ).fetchone()
        if row is None or plan.status != "APPROVED":
            raise PermissionError("exact current-plan permit is missing or stale")
        return self._permit(row)

    def finish_execution(
        self,
        task_run_id: str,
        *,
        summary: str = "execution completed",
        evidence: dict[str, Any] | None = None,
    ) -> TaskRun:
        permit = self.assert_execution_authorized(task_run_id)
        if not summary.strip():
            raise ValueError("execution summary is required")
        evidence = evidence or {"gateway": "finish_execution"}
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.EXECUTING)
            attempt = task.execution_attempt + 1
            execution_id = _stable("task-execution", task_run_id, attempt)
            db.execute(
                """INSERT INTO workflow_task_executions_v1(
                   execution_id,task_run_id,workflow_cycle_id,plan_id,permit_id,
                   attempt,status,summary,evidence_json,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,'SUCCEEDED',?,?,?,?)""",
                (
                    execution_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    task.current_plan_id,
                    permit.permit_id,
                    attempt,
                    summary.strip(),
                    json.dumps(evidence, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   execution_attempt=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.VALIDATING.value,
                    TaskPhase.VALIDATING.value,
                    attempt,
                    timestamp,
                    task.task_run_id,
                ),
            )
        return self.task(task_run_id)

    def finish_validation(
        self,
        *,
        task_run_id: str,
        verdict: ValidationVerdict,
        summary: str,
        findings: list[dict[str, Any]],
        repair_instructions: list[str],
        evidence: dict[str, Any],
    ) -> TaskRun:
        if not summary.strip() or not isinstance(evidence, dict) or not evidence:
            raise ValueError("validation summary and evidence are required")
        payload = {
            "verdict": verdict.value,
            "summary": summary,
            "findings": findings,
            "repair_instructions": repair_instructions,
            "evidence": evidence,
        }
        if self.validation_guard is not None:
            self.validation_guard(payload)
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.VALIDATING)
            if task.current_plan_id is None:
                raise RuntimeError("validated task has no plan")
            round_number = task.validation_round + 1
            validation_id = _stable(
                "validation", task_run_id, round_number, verdict.value
            )
            db.execute(
                """INSERT INTO workflow_task_validations_v1(
                   validation_id,task_run_id,workflow_cycle_id,plan_id,
                   execution_attempt,validation_round,verdict,summary,findings_json,
                   repair_instructions_json,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    validation_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    task.current_plan_id,
                    task.execution_attempt,
                    round_number,
                    verdict.value,
                    summary.strip(),
                    json.dumps(findings, sort_keys=True),
                    json.dumps(repair_instructions),
                    json.dumps(evidence, sort_keys=True),
                    timestamp,
                ),
            )
            if verdict == ValidationVerdict.ACCEPT:
                next_phase = TaskPhase.DONE
                db.execute(
                    """UPDATE workflow_cycles_v1 SET active_task_id=NULL,updated_at=?
                       WHERE workflow_cycle_id=? AND active_task_id=?""",
                    (timestamp, task.workflow_cycle_id, task.task_id),
                )
            elif verdict == ValidationVerdict.NEEDS_FIXES:
                next_phase = TaskPhase.EXECUTING
            elif verdict == ValidationVerdict.REPLAN:
                next_phase = TaskPhase.PLANNING
                db.execute(
                    """UPDATE workflow_task_permits_v1 SET invalidated_at=?
                       WHERE task_run_id=? AND invalidated_at IS NULL""",
                    (timestamp, task_run_id),
                )
                db.execute(
                    """UPDATE workflow_task_plans_v1 SET status='SUPERSEDED'
                       WHERE plan_id=?""",
                    (task.current_plan_id,),
                )
            else:
                next_phase = TaskPhase.FAILED
                db.execute(
                    """UPDATE workflow_cycles_v1 SET status=?,failure_reason=?,
                       updated_at=? WHERE workflow_cycle_id=?""",
                    (
                        WorkflowCycleStatus.FAILED.value,
                        summary.strip(),
                        timestamp,
                        task.workflow_cycle_id,
                    ),
                )
            feedback = (
                json.dumps(
                    [{"summary": summary, "instructions": repair_instructions}],
                    sort_keys=True,
                )
                if verdict in (ValidationVerdict.NEEDS_FIXES, ValidationVerdict.REPLAN)
                else "[]"
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   validation_round=?,repair_feedback_json=?,failure_reason=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    next_phase.value,
                    next_phase.value,
                    round_number,
                    feedback,
                    summary.strip() if next_phase == TaskPhase.FAILED else None,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.task(task_run_id)

    def pause_for_clarification(
        self, *, task_run_id: str, occurrence_key: str
    ) -> TaskRun:
        task = self.task(task_run_id)
        if task.phase not in (
            TaskPhase.PLANNING,
            TaskPhase.EXECUTING,
            TaskPhase.VALIDATING,
        ):
            raise ValueError("clarification is not valid in this phase")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            self._owned_task(db, task_run_id, task.phase)
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   waiting_from_phase=?,clarification_occurrence_key=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.WAITING_FOR_INPUT.value,
                    TaskPhase.WAITING_FOR_INPUT.value,
                    task.phase.value,
                    occurrence_key,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.task(task_run_id)

    def resume_clarification(self, *, task_run_id: str, occurrence_key: str) -> TaskRun:
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.WAITING_FOR_INPUT)
            if task.clarification_occurrence_key != occurrence_key:
                raise ValueError("clarification interrupt occurrence is stale")
            if task.waiting_from_phase is None:
                raise RuntimeError("clarification origin phase is missing")
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   waiting_from_phase=NULL,clarification_occurrence_key=NULL,updated_at=?
                   WHERE task_run_id=?""",
                (
                    task.waiting_from_phase.value,
                    task.waiting_from_phase.value,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.task(task_run_id)

    def publication_is_eligible(self, workflow_cycle_id: str) -> bool:
        cycle = self.cycle(workflow_cycle_id)
        tasks = self.task_runs(workflow_cycle_id)
        if (
            cycle.status != WorkflowCycleStatus.AWAITING_PUBLICATION
            or cycle.active_task_id is not None
            or not tasks
            or any(task.status != TaskPhase.DONE for task in tasks)
        ):
            return False
        for task in tasks:
            if task.current_plan_id is None:
                return False
            plan = self.plan(task.current_plan_id)
            if plan.status != "APPROVED":
                return False
            accepted = self.db.execute(
                """SELECT 1 FROM workflow_task_validations_v1
                   WHERE task_run_id=? AND plan_id=? AND verdict='ACCEPT'
                   ORDER BY validation_round DESC LIMIT 1""",
                (task.task_run_id, plan.plan_id),
            ).fetchone()
            permit = self.db.execute(
                """SELECT 1 FROM workflow_task_permits_v1
                   WHERE task_run_id=? AND plan_id=? AND plan_version=?
                     AND plan_digest=? AND invalidated_at IS NULL""",
                (task.task_run_id, plan.plan_id, plan.version, plan.plan_digest),
            ).fetchone()
            if accepted is None or permit is None:
                return False
        return True

    def task(self, task_run_id: str) -> TaskRun:
        row = self.db.execute(
            "SELECT * FROM workflow_task_runs_v1 WHERE task_run_id=?", (task_run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown task run")
        return self._task(row)

    def plan(self, plan_id: str) -> TaskPlan:
        row = self.db.execute(
            "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown task plan")
        return self._plan(row)

    def permit(self, permit_id: str) -> TaskPermit:
        row = self.db.execute(
            "SELECT * FROM workflow_task_permits_v1 WHERE permit_id=?", (permit_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown task permit")
        return self._permit(row)

    def _owned_task(
        self, db: sqlite3.Connection, task_run_id: str, expected: TaskPhase
    ) -> TaskRun:
        row = db.execute(
            "SELECT * FROM workflow_task_runs_v1 WHERE task_run_id=?", (task_run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown task run")
        task = self._task(row)
        cycle = db.execute(
            "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
            (task.workflow_cycle_id,),
        ).fetchone()
        if (
            cycle is None
            or cycle["status"] != WorkflowCycleStatus.ACTIVE.value
            or cycle["active_task_id"] != task.task_id
            or task.phase != expected
        ):
            raise ValueError("task does not own the expected workflow phase")
        return task

    @staticmethod
    def _cycle(row: sqlite3.Row) -> WorkflowCycle:
        return WorkflowCycle(
            workflow_cycle_id=row["workflow_cycle_id"],
            thread_id=row["thread_id"],
            cycle_id=row["cycle_id"],
            root_input_id=row["root_input_id"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            workflow_digest=row["workflow_digest"],
            status=WorkflowCycleStatus(row["status"]),
            active_task_id=row["active_task_id"],
            failure_reason=row["failure_reason"],
        )

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            task_run_id=row["task_run_id"],
            workflow_cycle_id=row["workflow_cycle_id"],
            thread_id=row["thread_id"],
            cycle_id=row["cycle_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            declaration_index=row["declaration_index"],
            dependencies=tuple(json.loads(row["dependencies_json"])),
            status=TaskPhase(row["status"]),
            phase=TaskPhase(row["phase"]),
            current_plan_id=row["current_plan_id"],
            execution_attempt=row["execution_attempt"],
            validation_round=row["validation_round"],
            repair_feedback=tuple(json.loads(row["repair_feedback_json"])),
            failure_reason=row["failure_reason"],
            waiting_from_phase=(
                TaskPhase(row["waiting_from_phase"])
                if row["waiting_from_phase"]
                else None
            ),
            clarification_occurrence_key=row["clarification_occurrence_key"],
        )

    @staticmethod
    def _plan(row: sqlite3.Row) -> TaskPlan:
        return TaskPlan(
            plan_id=row["plan_id"],
            task_run_id=row["task_run_id"],
            task_id=row["task_id"],
            version=row["version"],
            plan_text=row["plan_text"],
            plan_digest=row["plan_digest"],
            status=row["status"],
            posted_at=row["posted_at"],
            posted_comment_id=row["posted_comment_id"],
            approval_occurrence_key=row["approval_occurrence_key"],
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
            approval_event_key=row["approval_event_key"],
        )

    @staticmethod
    def _permit(row: sqlite3.Row) -> TaskPermit:
        return TaskPermit(
            permit_id=row["permit_id"],
            task_run_id=row["task_run_id"],
            workflow_cycle_id=row["workflow_cycle_id"],
            plan_id=row["plan_id"],
            plan_version=row["plan_version"],
            plan_digest=row["plan_digest"],
            approval_event_key=row["approval_event_key"],
            approved_by=row["approved_by"],
            created_at=row["created_at"],
            invalidated_at=row["invalidated_at"],
        )
