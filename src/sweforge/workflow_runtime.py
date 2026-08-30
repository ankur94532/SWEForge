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

from .github_models import InteractionMode
from .workflow_spec import PhaseSpec, TaskSpec, WorkflowSpec, parse_workflow_spec


class TaskPhase(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    WAITING_FOR_PLAN_APPROVAL = "WAITING_FOR_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    WAITING_FOR_RESULT_APPROVAL = "WAITING_FOR_RESULT_APPROVAL"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    DONE = "DONE"
    FAILED = "FAILED"


class WorkflowCycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    AWAITING_PUBLICATION = "AWAITING_PUBLICATION"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class WorkflowCycleKind(StrEnum):
    INITIAL = "INITIAL"
    REVISION = "REVISION"


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
    cycle_kind: WorkflowCycleKind
    revision_sequence: int | None
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
    approval_mode: str
    created_at: str
    invalidated_at: str | None


@dataclass(frozen=True, slots=True)
class TaskResult:
    result_id: str
    task_run_id: str
    workflow_cycle_id: str
    plan_id: str
    execution_id: str
    validation_id: str
    result_occurrence_key: str
    posted_at: str
    posted_comment_id: int


@dataclass(frozen=True, slots=True)
class TaskResultApproval:
    result_approval_id: str
    result_id: str
    task_run_id: str
    workflow_cycle_id: str
    plan_id: str
    execution_id: str
    validation_id: str
    result_occurrence_key: str
    mode: str
    approved_by: str
    approval_event_key: str | None
    approved_at: str
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
        cycle_kind: WorkflowCycleKind = WorkflowCycleKind.INITIAL,
        revision_sequence: int | None = None,
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
                    or existing["cycle_kind"] != cycle_kind.value
                    or existing["revision_sequence"] != revision_sequence
                ):
                    raise ValueError("workflow cycle identity/specification mismatch")
                return self._cycle(existing)
            active = db.execute(
                """SELECT workflow_cycle_id FROM workflow_cycles_v1
                   WHERE thread_id=? AND status='ACTIVE' LIMIT 1""",
                (thread_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("IssueThread already has an active workflow owner")
            db.execute(
                """INSERT INTO workflow_cycles_v1(
                   workflow_cycle_id,thread_id,cycle_id,root_input_id,workflow_id,
                   workflow_version,workflow_digest,workflow_spec_json,
                   workflow_spec_ref,cycle_kind,revision_sequence,status,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    cycle_kind.value,
                    revision_sequence,
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
        if cycle_kind == WorkflowCycleKind.INITIAL:
            self.store.activate_initial_workflow(
                thread_id=thread_id,
                workflow_cycle_id=identity,
                workflow_digest=spec.digest,
                now=timestamp,
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

    def cycle_for_thread(self, thread_id: str) -> WorkflowCycle | None:
        """Return the latest cycle unless it is fully published."""
        row = self.db.execute(
            """SELECT * FROM workflow_cycles_v1 WHERE thread_id=?
               ORDER BY cycle_id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if row is None or row["status"] == WorkflowCycleStatus.PUBLISHED.value:
            return None
        return self._cycle(row)

    def next_cycle_id(self, thread_id: str) -> int:
        row = self.db.execute(
            """SELECT COALESCE(MAX(cycle_id),0)+1 FROM workflow_cycles_v1
               WHERE thread_id=?""",
            (thread_id,),
        ).fetchone()
        return int(row[0])

    def next_revision_sequence(self, thread_id: str) -> int:
        row = self.db.execute(
            """SELECT COALESCE(MAX(revision_sequence),0)+1
               FROM workflow_cycles_v1 WHERE thread_id=? AND cycle_kind='REVISION'""",
            (thread_id,),
        ).fetchone()
        return int(row[0])

    def spec_for_cycle(self, workflow_cycle_id: str) -> WorkflowSpec:
        """Rehydrate and verify the exact specification persisted for a cycle."""
        row = self.db.execute(
            "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
            (workflow_cycle_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown workflow cycle")
        try:
            document = json.loads(row["workflow_spec_json"])
            task_documents = document["tasks"]
            known_tools = {
                tool_name
                for task in task_documents
                for phase in ("planning", "execution", "validation")
                for tool_name in task[phase]["tools"]
            }
            spec = parse_workflow_spec(document, known_tools=known_tools)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("persisted workflow specification is invalid") from exc
        if (
            spec.workflow_id != row["workflow_id"]
            or spec.version != row["workflow_version"]
            or spec.digest != row["workflow_digest"]
        ):
            raise ValueError("persisted workflow specification identity mismatch")
        return spec

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
                    if cycle["cycle_kind"] == WorkflowCycleKind.INITIAL.value:
                        db.execute(
                            """UPDATE thread_workflow_lifecycle_v1
                               SET initial_state='COMPLETE',updated_at=?
                               WHERE thread_id=? AND initial_state='ACTIVE'
                                 AND initial_workflow_cycle_id=?""",
                            (timestamp, cycle["thread_id"], workflow_cycle_id),
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
        if phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL:
            phase = TaskPhase.PLANNING
        if phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL:
            phase = TaskPhase.VALIDATING
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
                    TaskPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    TaskPhase.WAITING_FOR_PLAN_APPROVAL.value,
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
        if (
            self.store.interaction_mode(self.task(task_run_id).thread_id)
            != InteractionMode.MANUAL
        ):
            raise PermissionError("human plan approval requires MANUAL mode")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(
                db, task_run_id, TaskPhase.WAITING_FOR_PLAN_APPROVAL
            )
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
                   plan_digest,approval_event_key,approved_by,approval_mode,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    permit_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    plan["plan_id"],
                    plan["version"],
                    plan["plan_digest"],
                    approval_event_key,
                    approved_by,
                    "HUMAN",
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

    def auto_authorize_plan(self, task_run_id: str) -> TaskPermit:
        """Application-owned exact authorization for an immutable AUTO thread."""
        if (
            self.store.interaction_mode(self.task(task_run_id).thread_id)
            != InteractionMode.AUTO
        ):
            raise PermissionError("AUTO plan authorization requires AUTO mode")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(
                db, task_run_id, TaskPhase.WAITING_FOR_PLAN_APPROVAL
            )
            plan = db.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task.current_plan_id,),
            ).fetchone()
            if plan is None or plan["status"] != "POSTED":
                raise ValueError("current posted plan is missing")
            authority_id = f"auto-plan-authority:{plan['approval_occurrence_key']}"
            permit_id = _stable(
                "task-permit",
                task_run_id,
                plan["plan_id"],
                plan["version"],
                plan["plan_digest"],
                authority_id,
            )
            db.execute(
                """UPDATE workflow_task_plans_v1 SET status='APPROVED',
                   approved_at=?,approved_by=?,approval_event_key=? WHERE plan_id=?""",
                (timestamp, "sweforge:auto-policy", authority_id, plan["plan_id"]),
            )
            db.execute(
                """INSERT OR IGNORE INTO workflow_task_permits_v1(
                   permit_id,task_run_id,workflow_cycle_id,plan_id,plan_version,
                   plan_digest,approval_event_key,approved_by,approval_mode,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    permit_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    plan["plan_id"],
                    plan["version"],
                    plan["plan_digest"],
                    authority_id,
                    "sweforge:auto-policy",
                    "AUTO",
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
            task = self._owned_task(
                db, task_run_id, TaskPhase.WAITING_FOR_PLAN_APPROVAL
            )
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

    def resolve_current_feedback_replan(self, feedback_review_id: str) -> TaskRun:
        """Apply the relevant-feedback outcome for one exact durable review."""
        review = self.store.feedback_review(feedback_review_id)
        if review is None:
            raise ValueError("feedback review does not exist")
        task = self.task(review.task_run_id)
        if review.status == "REPLAN":
            return task
        if review.status != "REVIEWING":
            raise ValueError("feedback review is no longer replannable")
        if task.phase == TaskPhase.PLANNING:
            # Recovery after the authoritative transition committed but before
            # the review outcome was recorded.
            pass
        elif review.feedback_kind == "PLAN":
            self.replan_from_feedback(review.task_run_id)
        else:
            self.replan_from_result_feedback(
                task_run_id=review.task_run_id,
                event_key=review.source_event_key,
                feedback=review.feedback_text,
            )
        with self.store.transaction(immediate=True) as db:
            changed = db.execute(
                """UPDATE workflow_feedback_reviews_v1
                   SET status='REPLAN',updated_at=?
                   WHERE feedback_review_id=? AND status='REVIEWING'""",
                (self.clock(), feedback_review_id),
            )
            if changed.rowcount not in {0, 1}:
                raise RuntimeError("feedback review transition was ambiguous")
        return self.task(review.task_run_id)

    def close_deferred_feedback(self, feedback_review_id: str) -> None:
        with self.store.transaction(immediate=True) as db:
            db.execute(
                """UPDATE workflow_feedback_reviews_v1
                   SET status='CLOSED',updated_at=?
                   WHERE feedback_review_id=? AND status='DEFERRED_WAITING'""",
                (self.clock(), feedback_review_id),
            )

    def replan_for_revision_inputs(
        self, *, task_run_id: str, revision_input_ids: list[str]
    ) -> TaskRun:
        """Supersede a visible stale revision occurrence at a safe boundary."""
        if not revision_input_ids:
            raise ValueError("revision replan requires durable input identities")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM workflow_task_runs_v1 WHERE task_run_id=?",
                (task_run_id,),
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
                or cycle["cycle_kind"] != WorkflowCycleKind.REVISION.value
                or cycle["status"] != WorkflowCycleStatus.ACTIVE.value
                or cycle["active_task_id"] != task.task_id
                or task.phase
                not in {
                    TaskPhase.WAITING_FOR_PLAN_APPROVAL,
                    TaskPhase.WAITING_FOR_RESULT_APPROVAL,
                }
            ):
                raise ValueError("revision task is not at a replanning boundary")
            history = list(task.repair_feedback)
            history.append(
                {
                    "kind": "QUEUED_REVISION_INPUTS",
                    "revision_input_ids": list(revision_input_ids),
                    "superseded_phase": task.phase.value,
                }
            )
            if task.current_plan_id:
                db.execute(
                    "UPDATE workflow_task_plans_v1 SET status='SUPERSEDED' "
                    "WHERE plan_id=?",
                    (task.current_plan_id,),
                )
            db.execute(
                """UPDATE workflow_task_permits_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """UPDATE workflow_task_result_approvals_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   repair_feedback_json=?,updated_at=? WHERE task_run_id=?""",
                (
                    TaskPhase.PLANNING.value,
                    TaskPhase.PLANNING.value,
                    json.dumps(history, sort_keys=True),
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
        expected_mode = (
            "AUTO"
            if self.store.interaction_mode(task.thread_id) == InteractionMode.AUTO
            else "HUMAN"
        )
        if (
            row is None
            or plan.status != "APPROVED"
            or row["approval_mode"] != expected_mode
        ):
            raise PermissionError("exact current-plan permit is missing or stale")
        if expected_mode == "AUTO" and (
            row["approved_by"] != "sweforge:auto-policy"
            or not row["approval_event_key"].startswith(
                "auto-plan-authority:plan-approval:"
            )
        ):
            raise PermissionError("AUTO plan authority provenance is invalid")
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
            prior = db.execute(
                """SELECT * FROM workflow_task_validations_v1
                   WHERE task_run_id=? AND plan_id=? AND execution_attempt=?
                   ORDER BY validation_round DESC LIMIT 1""",
                (task_run_id, task.current_plan_id, task.execution_attempt),
            ).fetchone()
            if prior is not None:
                same = (
                    prior["verdict"] == verdict.value
                    and prior["summary"] == summary.strip()
                    and prior["findings_json"] == json.dumps(findings, sort_keys=True)
                    and prior["repair_instructions_json"]
                    == json.dumps(repair_instructions)
                    and prior["evidence_json"] == json.dumps(evidence, sort_keys=True)
                )
                if same and verdict == ValidationVerdict.ACCEPT:
                    return task
                raise ValueError(
                    "this execution attempt already has a validation decision"
                )
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
                # Publication of the exact validated result is an external
                # side effect. Keep ownership in VALIDATING until its GitHub
                # comment has been reconciled and durably bound below.
                next_phase = TaskPhase.VALIDATING
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
            feedback_history = list(task.repair_feedback)
            if verdict in (ValidationVerdict.NEEDS_FIXES, ValidationVerdict.REPLAN):
                feedback_history.append(
                    {
                        "kind": "VALIDATION_FEEDBACK",
                        "summary": summary,
                        "instructions": repair_instructions,
                        "verdict": verdict.value,
                    }
                )
            feedback = json.dumps(feedback_history, sort_keys=True)
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

    def publish_validated_result(
        self,
        *,
        task_run_id: str,
        posted_comment_id: int,
        posted_at: str,
    ) -> TaskResult:
        """Bind the visible result for the latest exact ACCEPT validation."""
        if not isinstance(posted_comment_id, int) or posted_comment_id <= 0:
            raise ValueError("a reconciled GitHub result comment is required")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(db, task_run_id, TaskPhase.VALIDATING)
            validation = db.execute(
                """SELECT * FROM workflow_task_validations_v1
                   WHERE task_run_id=? AND plan_id=? AND execution_attempt=?
                   ORDER BY validation_round DESC LIMIT 1""",
                (task_run_id, task.current_plan_id, task.execution_attempt),
            ).fetchone()
            execution = db.execute(
                """SELECT * FROM workflow_task_executions_v1
                   WHERE task_run_id=? AND plan_id=? AND attempt=?""",
                (task_run_id, task.current_plan_id, task.execution_attempt),
            ).fetchone()
            if (
                validation is None
                or validation["verdict"] != ValidationVerdict.ACCEPT.value
                or execution is None
                or execution["status"] != "SUCCEEDED"
            ):
                raise ValueError("latest exact validated result is not acceptable")
            result_id = _stable(
                "task-result",
                task_run_id,
                task.current_plan_id,
                execution["execution_id"],
                validation["validation_id"],
            )
            occurrence = (
                f"result-approval:{task_run_id}:{result_id}:"
                f"{execution['execution_id']}:{validation['validation_id']}"
            )
            db.execute(
                """INSERT OR IGNORE INTO workflow_task_results_v1(
                   result_id,task_run_id,workflow_cycle_id,plan_id,execution_id,
                   validation_id,result_occurrence_key,posted_at,posted_comment_id,
                   created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    result_id,
                    task_run_id,
                    task.workflow_cycle_id,
                    task.current_plan_id,
                    execution["execution_id"],
                    validation["validation_id"],
                    occurrence,
                    posted_at,
                    posted_comment_id,
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,updated_at=?
                   WHERE task_run_id=?""",
                (
                    TaskPhase.WAITING_FOR_RESULT_APPROVAL.value,
                    TaskPhase.WAITING_FOR_RESULT_APPROVAL.value,
                    timestamp,
                    task_run_id,
                ),
            )
        return self.result(result_id)

    def approve_result(
        self,
        *,
        task_run_id: str,
        occurrence_key: str,
        approval_event_key: str | None,
        approved_by: str,
        approval_is_authorized: bool,
        approval_occurred_at: str,
        mode: str = "HUMAN",
    ) -> TaskResultApproval:
        """Accept one exact plan/execution/validation result occurrence."""
        if not approval_is_authorized:
            raise PermissionError("result approver permission could not be proven")
        normalized_mode = mode.upper()
        if normalized_mode not in {"HUMAN", "AUTO"}:
            raise ValueError("result approval mode is invalid")
        if not approved_by or (normalized_mode == "HUMAN" and not approval_event_key):
            raise ValueError("result approval identity is required")
        expected_mode = (
            "AUTO"
            if self.store.interaction_mode(self.task(task_run_id).thread_id)
            == InteractionMode.AUTO
            else "HUMAN"
        )
        if normalized_mode != expected_mode:
            raise PermissionError(
                f"{normalized_mode} result approval is invalid for {expected_mode} mode"
            )
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(
                db, task_run_id, TaskPhase.WAITING_FOR_RESULT_APPROVAL
            )
            result = db.execute(
                """SELECT * FROM workflow_task_results_v1
                   WHERE task_run_id=? AND result_occurrence_key=?""",
                (task_run_id, occurrence_key),
            ).fetchone()
            current = self._current_result_row(db, task_run_id)
            if (
                result is None
                or current is None
                or result["result_id"] != current["result_id"]
                or result["plan_id"] != task.current_plan_id
            ):
                raise ValueError("result approval interrupt occurrence is stale")
            if (
                normalized_mode == "HUMAN"
                and approval_occurred_at <= result["posted_at"]
            ):
                raise ValueError("result approval predates the visible result")
            approval_id = _stable(
                "task-result-approval",
                result["result_id"],
                occurrence_key,
                normalized_mode,
                approval_event_key or approved_by,
            )
            db.execute(
                """INSERT OR IGNORE INTO workflow_task_result_approvals_v1(
                   result_approval_id,result_id,task_run_id,workflow_cycle_id,
                   plan_id,execution_id,validation_id,result_occurrence_key,mode,
                   approved_by,approval_event_key,approved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    result["result_id"],
                    task_run_id,
                    task.workflow_cycle_id,
                    result["plan_id"],
                    result["execution_id"],
                    result["validation_id"],
                    occurrence_key,
                    normalized_mode,
                    approved_by,
                    approval_event_key,
                    approval_occurred_at,
                ),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,updated_at=?
                   WHERE task_run_id=?""",
                (TaskPhase.DONE.value, TaskPhase.DONE.value, timestamp, task_run_id),
            )
            released = db.execute(
                """UPDATE workflow_cycles_v1 SET active_task_id=NULL,updated_at=?
                   WHERE workflow_cycle_id=? AND active_task_id=?""",
                (timestamp, task.workflow_cycle_id, task.task_id),
            )
            if released.rowcount != 1:
                raise RuntimeError(
                    "active task ownership changed during result approval"
                )
        return self.result_approval(approval_id)

    def auto_accept_result(self, task_run_id: str) -> TaskResultApproval:
        result = self.current_result(task_run_id)
        if result is None:
            raise ValueError("validated result is missing")
        return self.approve_result(
            task_run_id=task_run_id,
            occurrence_key=result.result_occurrence_key,
            approval_event_key=None,
            approved_by="sweforge:auto-policy",
            approval_is_authorized=True,
            approval_occurred_at=self.clock(),
            mode="AUTO",
        )

    def replan_from_result_feedback(
        self, *, task_run_id: str, event_key: str, feedback: str
    ) -> TaskRun:
        """Carry the exact rejected result into same-owner cumulative planning."""
        if not event_key or not feedback.strip():
            raise ValueError("durable result feedback identity and text are required")
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            task = self._owned_task(
                db, task_run_id, TaskPhase.WAITING_FOR_RESULT_APPROVAL
            )
            plan = db.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task.current_plan_id,),
            ).fetchone()
            result = self._current_result_row(db, task_run_id)
            if plan is None or result is None:
                raise RuntimeError("result feedback context is incomplete")
            execution = db.execute(
                "SELECT * FROM workflow_task_executions_v1 WHERE execution_id=?",
                (result["execution_id"],),
            ).fetchone()
            validation = db.execute(
                "SELECT * FROM workflow_task_validations_v1 WHERE validation_id=?",
                (result["validation_id"],),
            ).fetchone()
            history = list(task.repair_feedback)
            history.append(
                {
                    "kind": "RESULT_FEEDBACK",
                    "event_key": event_key,
                    "feedback": feedback.strip()[:4_000],
                    "previous_plan": plan["plan_text"],
                    "execution_summary": execution["summary"] if execution else "",
                    "validation_summary": validation["summary"] if validation else "",
                    "validation_id": result["validation_id"],
                    "execution_id": result["execution_id"],
                }
            )
            db.execute(
                "UPDATE workflow_task_plans_v1 SET status='SUPERSEDED' WHERE plan_id=?",
                (plan["plan_id"],),
            )
            db.execute(
                """UPDATE workflow_task_permits_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """UPDATE workflow_task_result_approvals_v1 SET invalidated_at=?
                   WHERE task_run_id=? AND invalidated_at IS NULL""",
                (timestamp, task_run_id),
            )
            db.execute(
                """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                   repair_feedback_json=?,updated_at=? WHERE task_run_id=?""",
                (
                    TaskPhase.PLANNING.value,
                    TaskPhase.PLANNING.value,
                    json.dumps(history, sort_keys=True),
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
            if (
                plan.status != "APPROVED"
                or hashlib.sha256(plan.plan_text.strip().encode()).hexdigest()
                != plan.plan_digest
            ):
                return False
            execution = self.db.execute(
                """SELECT * FROM workflow_task_executions_v1
                   WHERE task_run_id=? AND plan_id=? AND attempt=?
                   ORDER BY completed_at DESC LIMIT 1""",
                (task.task_run_id, plan.plan_id, task.execution_attempt),
            ).fetchone()
            accepted = self.db.execute(
                """SELECT * FROM workflow_task_validations_v1
                   WHERE task_run_id=? ORDER BY validation_round DESC LIMIT 1""",
                (task.task_run_id,),
            ).fetchone()
            permit = self.db.execute(
                """SELECT * FROM workflow_task_permits_v1
                   WHERE task_run_id=? AND plan_id=? AND plan_version=?
                     AND plan_digest=? AND invalidated_at IS NULL""",
                (task.task_run_id, plan.plan_id, plan.version, plan.plan_digest),
            ).fetchone()
            expected_mode = (
                "AUTO"
                if self.store.interaction_mode(task.thread_id) == InteractionMode.AUTO
                else "HUMAN"
            )
            result = self.db.execute(
                """SELECT * FROM workflow_task_results_v1
                   WHERE task_run_id=? AND plan_id=? AND execution_id=?
                     AND validation_id=?""",
                (
                    task.task_run_id,
                    plan.plan_id,
                    execution["execution_id"] if execution else "",
                    accepted["validation_id"] if accepted else "",
                ),
            ).fetchone()
            result_approval = (
                self.db.execute(
                    """SELECT * FROM workflow_task_result_approvals_v1
                       WHERE result_id=? AND invalidated_at IS NULL""",
                    (result["result_id"],),
                ).fetchone()
                if result is not None
                else None
            )
            if execution is None or accepted is None:
                return False
            try:
                execution_evidence = json.loads(execution["evidence_json"])
                validation_evidence = json.loads(accepted["evidence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            if (
                execution["status"] != "SUCCEEDED"
                or permit is None
                or permit["approval_mode"] != expected_mode
                or execution["permit_id"] != permit["permit_id"]
                or not isinstance(execution_evidence, dict)
                or not isinstance(execution_evidence.get("tool_observations"), list)
                or accepted["plan_id"] != plan.plan_id
                or accepted["execution_attempt"] != execution["attempt"]
                or accepted["verdict"] != ValidationVerdict.ACCEPT.value
                or not isinstance(validation_evidence, dict)
                or not isinstance(validation_evidence.get("validation_runs"), list)
                or not validation_evidence["validation_runs"]
                or result is None
                or result_approval is None
                or result_approval["task_run_id"] != task.task_run_id
                or result_approval["plan_id"] != plan.plan_id
                or result_approval["execution_id"] != execution["execution_id"]
                or result_approval["validation_id"] != accepted["validation_id"]
                or result_approval["result_occurrence_key"]
                != result["result_occurrence_key"]
                or result_approval["mode"]
                != (
                    "AUTO"
                    if self.store.interaction_mode(task.thread_id)
                    == InteractionMode.AUTO
                    else "HUMAN"
                )
            ):
                return False
        return True

    def fail_active_task(self, workflow_cycle_id: str, *, reason: str) -> WorkflowCycle:
        """Terminally fail the current owner after bounded infrastructure retries."""
        message = reason.strip()[:1_000] or "workflow driver failed"
        timestamp = self.clock()
        with self.store.transaction(immediate=True) as db:
            cycle = db.execute(
                "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
                (workflow_cycle_id,),
            ).fetchone()
            if cycle is None:
                raise ValueError("unknown workflow cycle")
            if cycle["status"] == WorkflowCycleStatus.FAILED.value:
                return self._cycle(cycle)
            if cycle["status"] != WorkflowCycleStatus.ACTIVE.value:
                raise ValueError("only an active workflow cycle can fail")
            if cycle["active_task_id"] is not None:
                db.execute(
                    """UPDATE workflow_task_runs_v1 SET status=?,phase=?,
                       failure_reason=?,updated_at=?
                       WHERE workflow_cycle_id=? AND task_id=?""",
                    (
                        TaskPhase.FAILED.value,
                        TaskPhase.FAILED.value,
                        message,
                        timestamp,
                        workflow_cycle_id,
                        cycle["active_task_id"],
                    ),
                )
            db.execute(
                """UPDATE workflow_cycles_v1 SET status=?,failure_reason=?,updated_at=?
                   WHERE workflow_cycle_id=?""",
                (
                    WorkflowCycleStatus.FAILED.value,
                    message,
                    timestamp,
                    workflow_cycle_id,
                ),
            )
        return self.cycle(workflow_cycle_id)

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

    def result(self, result_id: str) -> TaskResult:
        row = self.db.execute(
            "SELECT * FROM workflow_task_results_v1 WHERE result_id=?", (result_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown task result")
        return self._result(row)

    def current_result(self, task_run_id: str) -> TaskResult | None:
        row = self._current_result_row(self.db, task_run_id)
        return self._result(row) if row is not None else None

    @staticmethod
    def _current_result_row(
        db: sqlite3.Connection, task_run_id: str
    ) -> sqlite3.Row | None:
        return db.execute(
            """SELECT r.* FROM workflow_task_results_v1 AS r
               JOIN workflow_task_validations_v1 AS v
                 ON v.validation_id=r.validation_id
               WHERE r.task_run_id=?
               ORDER BY v.validation_round DESC,r.result_id DESC LIMIT 1""",
            (task_run_id,),
        ).fetchone()

    def result_approval(self, approval_id: str) -> TaskResultApproval:
        row = self.db.execute(
            """SELECT * FROM workflow_task_result_approvals_v1
               WHERE result_approval_id=?""",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown task result approval")
        return self._result_approval(row)

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
            cycle_kind=WorkflowCycleKind(row["cycle_kind"]),
            revision_sequence=row["revision_sequence"],
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
            approval_mode=row["approval_mode"],
            created_at=row["created_at"],
            invalidated_at=row["invalidated_at"],
        )

    @staticmethod
    def _result(row: sqlite3.Row) -> TaskResult:
        return TaskResult(
            result_id=row["result_id"],
            task_run_id=row["task_run_id"],
            workflow_cycle_id=row["workflow_cycle_id"],
            plan_id=row["plan_id"],
            execution_id=row["execution_id"],
            validation_id=row["validation_id"],
            result_occurrence_key=row["result_occurrence_key"],
            posted_at=row["posted_at"],
            posted_comment_id=row["posted_comment_id"],
        )

    @staticmethod
    def _result_approval(row: sqlite3.Row) -> TaskResultApproval:
        return TaskResultApproval(
            result_approval_id=row["result_approval_id"],
            result_id=row["result_id"],
            task_run_id=row["task_run_id"],
            workflow_cycle_id=row["workflow_cycle_id"],
            plan_id=row["plan_id"],
            execution_id=row["execution_id"],
            validation_id=row["validation_id"],
            result_occurrence_key=row["result_occurrence_key"],
            mode=row["mode"],
            approved_by=row["approved_by"],
            approval_event_key=row["approval_event_key"],
            approved_at=row["approved_at"],
            invalidated_at=row["invalidated_at"],
        )
