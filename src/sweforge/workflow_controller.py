"""Production GitHub integration over the declarative workflow runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .execution import normalize_task
from .execution_locks import ThreadLockUnavailable, thread_lock
from .github_models import (
    InteractionMode,
    is_actionable_source_event,
    is_exact_agent_approval,
    parse_timestamp,
    starts_with_agent_invocation,
)
from .github_store import SQLiteGitHubStore, ThreadWorkspaceRecord
from .workflow_runtime import (
    TaskPhase,
    TaskRun,
    WorkflowCycle,
    WorkflowCycleStatus,
    WorkflowRuntime,
)
from .workflow_spec import WorkflowSpec
from .workspace import ThreadWorkspace, WorkspaceError


@dataclass(frozen=True, slots=True)
class ControllerResult:
    thread_id: str
    status: str
    workflow_cycle_id: str | None = None
    active_task_id: str | None = None
    phase: TaskPhase | None = None


class WorkflowAgentDriver(Protocol):
    """One durable root-agent driver; deterministic doubles implement this too."""

    def drive(
        self,
        *,
        cycle: WorkflowCycle,
        task: TaskRun,
        prompt: str,
        resume: dict[str, Any] | None = None,
    ) -> None: ...

    def reconcile_interrupts(self, *, cycle: WorkflowCycle, task: TaskRun) -> None: ...

    def has_pending_interrupt(
        self, *, cycle: WorkflowCycle, task: TaskRun, kind: str, occurrence_key: str
    ) -> bool: ...


class DeclarativeWorkflowController:
    """Thin GitHub/control facade; ``WorkflowRuntime`` owns all transitions."""

    def __init__(
        self,
        *,
        store: SQLiteGitHubStore,
        client: Any,
        spec: WorkflowSpec,
        spec_ref: str | None,
        repo_paths: dict[str, str | Path],
        workspace_root: str | Path,
        lock_root: str | Path,
        driver_factory: Callable[
            [WorkflowRuntime, str, Path, WorkflowSpec], WorkflowAgentDriver
        ],
        clock: Callable[[], str],
    ) -> None:
        self.store = store
        self.client = client
        self.spec = spec
        self.spec_ref = spec_ref
        self.repo_paths = repo_paths
        self.workspace_root = workspace_root
        self.lock_root = lock_root
        self.driver_factory = driver_factory
        self.clock = clock
        self.runtime = WorkflowRuntime(store, clock=clock)

    def advance(self, thread_id: str) -> ControllerResult:
        try:
            with thread_lock(self.lock_root, thread_id):
                return self._advance_locked(thread_id)
        except ThreadLockUnavailable:
            return ControllerResult(thread_id, "BUSY")

    def _advance_locked(self, thread_id: str) -> ControllerResult:
        cycle = self.runtime.cycle_for_thread(thread_id)
        if cycle is None:
            cycle = self._begin_cycle(thread_id)
            if cycle is None:
                return ControllerResult(thread_id, "IDLE")
        if cycle.status == WorkflowCycleStatus.FAILED:
            failed_task = self.runtime.active_task(cycle.workflow_cycle_id)
            return ControllerResult(
                thread_id,
                "FAILED",
                cycle.workflow_cycle_id,
                cycle.active_task_id,
                failed_task.phase if failed_task else None,
            )
        if cycle.status == WorkflowCycleStatus.AWAITING_PUBLICATION:
            return ControllerResult(
                thread_id,
                "AWAITING_PUBLICATION",
                cycle.workflow_cycle_id,
            )
        task = self.runtime.select_active_task(cycle.workflow_cycle_id)
        cycle = self.runtime.cycle(cycle.workflow_cycle_id)
        if task is None:
            return ControllerResult(
                thread_id,
                cycle.status.value,
                cycle.workflow_cycle_id,
            )
        workspace = self._workspace(cycle)
        cycle_spec = self.runtime.spec_for_cycle(cycle.workflow_cycle_id)
        driver = self.driver_factory(
            self.runtime, cycle.workflow_cycle_id, workspace.path, cycle_spec
        )
        mode = self.store.interaction_mode(cycle.thread_id)
        if task.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL:
            if mode == InteractionMode.AUTO:
                self.runtime.auto_authorize_plan(task.task_run_id)
                return self._result(
                    cycle, self.runtime.task(task.task_run_id), "ACTIVE"
                )
            resume = self._plan_wait_resume(cycle, task, driver)
            if resume is None:
                driver.reconcile_interrupts(cycle=cycle, task=task)
                return self._result(cycle, task, "WAITING_FOR_PLAN_APPROVAL")
            driver.drive(cycle=cycle, task=task, prompt="", resume=resume)
        elif task.phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL:
            if mode == InteractionMode.AUTO:
                self.runtime.auto_accept_result(task.task_run_id)
                fresh = self.runtime.select_active_task(cycle.workflow_cycle_id)
                return ControllerResult(
                    cycle.thread_id,
                    self.runtime.cycle(cycle.workflow_cycle_id).status.value,
                    cycle.workflow_cycle_id,
                    fresh.task_id if fresh else None,
                    fresh.phase if fresh else None,
                )
            resume = self._result_wait_resume(cycle, task, driver)
            if resume is None:
                driver.reconcile_interrupts(cycle=cycle, task=task)
                return self._result(cycle, task, "WAITING_FOR_RESULT_APPROVAL")
            driver.drive(cycle=cycle, task=task, prompt="", resume=resume)
        elif task.phase == TaskPhase.WAITING_FOR_INPUT:
            resume = self._clarification_resume(cycle, task, driver)
            if resume is None:
                driver.reconcile_interrupts(cycle=cycle, task=task)
                return self._result(cycle, task, "WAITING_FOR_INPUT")
            driver.drive(cycle=cycle, task=task, prompt="", resume=resume)
        else:
            driver.drive(
                cycle=cycle,
                task=task,
                prompt=self._phase_prompt(cycle, task),
            )
        fresh_cycle = self.runtime.cycle(cycle.workflow_cycle_id)
        fresh_task = self.runtime.active_task(cycle.workflow_cycle_id)
        if fresh_task is None:
            # Release/selection is deliberately a separate scheduler action,
            # but do it in this bounded tick so a completed task can expose the
            # next declaration-order owner without involving legacy state.
            fresh_task = self.runtime.select_active_task(cycle.workflow_cycle_id)
            fresh_cycle = self.runtime.cycle(cycle.workflow_cycle_id)
        return ControllerResult(
            thread_id,
            fresh_cycle.status.value,
            fresh_cycle.workflow_cycle_id,
            fresh_cycle.active_task_id,
            fresh_task.phase if fresh_task else None,
        )

    def _begin_cycle(self, thread_id: str) -> WorkflowCycle | None:
        deferred = list(self.store.deferred_followups(thread_id))
        candidates = deferred + [
            row
            for row in self.store.unconsumed_inputs(thread_id)
            if is_actionable_source_event(row["source_kind"], row["body"])
        ]
        if not candidates:
            return None
        event = candidates[0]
        deferred_id = event["deferred_id"] if "deferred_id" in event.keys() else None
        root_input_id = deferred_id or event["event_key"]
        cycle_id = self.runtime.next_cycle_id(thread_id)
        cycle = self.runtime.initialize_cycle(
            thread_id=thread_id,
            cycle_id=cycle_id,
            root_input_id=root_input_id,
            spec=self.spec,
            spec_ref=self.spec_ref,
        )
        if deferred_id:
            self.store.consume_deferred_followup(
                event["event_key"],
                deferred_id=deferred_id,
                cycle_id=cycle_id,
                consumed_at=self.clock(),
            )
        else:
            self.store.record_input_disposition(
                event["event_key"],
                thread_id=thread_id,
                cycle_id=cycle_id,
                status="CYCLE_ROOT",
                recorded_at=self.clock(),
            )
        self._workspace(cycle)
        return cycle

    def _root_event(self, cycle: WorkflowCycle):
        root = self.store.source_event(cycle.root_input_id)
        if root is not None:
            return root
        deferred = self.store.deferred_followup_by_id(cycle.root_input_id)
        if deferred is None:
            raise RuntimeError("workflow root input disappeared")
        root = self.store.source_event(deferred["event_key"])
        if root is None:
            raise RuntimeError("workflow root SourceEvent disappeared")
        return root

    def _root_body(self, cycle: WorkflowCycle, root: Any) -> str:
        if cycle.root_input_id.startswith("deferred-"):
            return (
                self.store.deferred_text_for_event(
                    root["event_key"], deferred_id=cycle.root_input_id
                )
                or root["body"]
            )
        return root["body"]

    def _workspace(self, cycle: WorkflowCycle) -> ThreadWorkspace:
        thread = self.store.issue_thread(cycle.thread_id)
        if thread is None:
            raise WorkspaceError("IssueThread metadata disappeared")
        repository = self.repo_paths.get(thread["repo_full_name"])
        if repository is None:
            raise WorkspaceError("no trusted local checkout configured")
        existing = self.store.thread_workspace(cycle.thread_id)
        workspace = ThreadWorkspace.create(
            repository=Path(repository).expanduser().resolve(),
            workspace_root=self.workspace_root,
            repo_id=thread["repo_id"],
            issue_number=thread["issue_number"],
            existing_path=existing.workspace_path if existing else None,
            expected_branch=existing.branch_name if existing else None,
            expected_base=existing.base_commit if existing else None,
            lock_root=self.lock_root,
            fetch_remote_main=existing is None,
        )
        if existing is None:
            now = self.clock()
            self.store.save_thread_workspace(
                ThreadWorkspaceRecord(
                    thread_id=cycle.thread_id,
                    repo_id=thread["repo_id"],
                    repo_full_name=thread["repo_full_name"],
                    issue_number=thread["issue_number"],
                    source_repository_path=str(Path(repository).expanduser().resolve()),
                    workspace_path=str(workspace.path),
                    branch_name=workspace.branch_name,
                    base_commit=workspace.base_commit,
                    created_at=now,
                    updated_at=now,
                )
            )
        return workspace

    def _plan_wait_resume(
        self,
        cycle: WorkflowCycle,
        task: TaskRun,
        driver: WorkflowAgentDriver,
    ) -> dict[str, Any] | None:
        if task.current_plan_id is None:
            raise RuntimeError("waiting task has no current plan")
        plan = self.runtime.plan(task.current_plan_id)
        root = self._root_event(cycle)
        for event in self.store.unconsumed_inputs(cycle.thread_id):
            if not self._same_target(event, root) or not self._after(
                event["source_created_at"], plan.posted_at
            ):
                continue
            if not self._has_pending_interrupt(
                driver, cycle, task, "PLAN_APPROVAL", plan.approval_occurrence_key
            ):
                driver.reconcile_interrupts(cycle=cycle, task=task)
                return None
            if is_exact_agent_approval(event["body"]):
                authorized = self._authorized_approver(
                    root["repo_full_name"], event["author_login"]
                )
                self._consume(event, cycle, "PLAN_APPROVAL" if authorized else "STALE")
                if not authorized:
                    continue
                return {
                    "kind": "PLAN_APPROVAL",
                    "occurrence_key": plan.approval_occurrence_key,
                    "event_key": event["event_key"],
                    "approved_by": event["author_login"],
                    "approved_at": event["source_created_at"],
                    "authorized": True,
                }
            if starts_with_agent_invocation(event["body"]):
                self._consume(event, cycle, "PLAN_FEEDBACK")
                return {
                    "kind": "PLAN_FEEDBACK",
                    "occurrence_key": plan.approval_occurrence_key,
                    "event_key": event["event_key"],
                    "feedback": normalize_task(event["body"]),
                }
        return None

    def _clarification_resume(
        self,
        cycle: WorkflowCycle,
        task: TaskRun,
        driver: WorkflowAgentDriver,
    ) -> dict[str, Any] | None:
        occurrence = task.clarification_occurrence_key
        if not occurrence:
            raise RuntimeError("waiting clarification occurrence is missing")
        root = self._root_event(cycle)
        if not self._has_pending_interrupt(
            driver, cycle, task, "CLARIFICATION", occurrence
        ):
            driver.reconcile_interrupts(cycle=cycle, task=task)
            return None
        updated = self.store.connection.execute(
            "SELECT updated_at FROM workflow_task_runs_v1 WHERE task_run_id=?",
            (task.task_run_id,),
        ).fetchone()["updated_at"]
        for event in self.store.unconsumed_inputs(cycle.thread_id):
            if not self._same_target(event, root) or not self._after(
                event["source_created_at"], updated
            ):
                continue
            if is_exact_agent_approval(event["body"]):
                self._consume(event, cycle, "STALE_APPROVAL")
                continue
            if starts_with_agent_invocation(event["body"]):
                self._consume(event, cycle, "CLARIFICATION_RESPONSE")
                return {
                    "kind": "CLARIFICATION_RESPONSE",
                    "occurrence_key": occurrence,
                    "event_key": event["event_key"],
                    "answer": normalize_task(event["body"]),
                }
        return None

    def _result_wait_resume(
        self,
        cycle: WorkflowCycle,
        task: TaskRun,
        driver: WorkflowAgentDriver,
    ) -> dict[str, Any] | None:
        result = self.runtime.current_result(task.task_run_id)
        if result is None:
            raise RuntimeError("waiting task has no current validated result")
        root = self._root_event(cycle)
        for event in self.store.unconsumed_inputs(cycle.thread_id):
            if not self._same_target(event, root) or not self._after(
                event["source_created_at"], result.posted_at
            ):
                continue
            if not self._has_pending_interrupt(
                driver,
                cycle,
                task,
                "RESULT_APPROVAL",
                result.result_occurrence_key,
            ):
                driver.reconcile_interrupts(cycle=cycle, task=task)
                return None
            if is_exact_agent_approval(event["body"]):
                authorized = self._authorized_approver(
                    root["repo_full_name"], event["author_login"]
                )
                self._consume(
                    event, cycle, "RESULT_APPROVAL" if authorized else "STALE"
                )
                if not authorized:
                    continue
                return {
                    "kind": "RESULT_APPROVAL",
                    "occurrence_key": result.result_occurrence_key,
                    "event_key": event["event_key"],
                    "approved_by": event["author_login"],
                    "approved_at": event["source_created_at"],
                    "authorized": True,
                }
            if starts_with_agent_invocation(event["body"]):
                self._consume(event, cycle, "RESULT_FEEDBACK")
                return {
                    "kind": "RESULT_FEEDBACK",
                    "occurrence_key": result.result_occurrence_key,
                    "event_key": event["event_key"],
                    "feedback": normalize_task(event["body"]),
                }
        return None

    @staticmethod
    def _has_pending_interrupt(
        driver: WorkflowAgentDriver,
        cycle: WorkflowCycle,
        task: TaskRun,
        kind: str,
        occurrence_key: str,
    ) -> bool:
        check = getattr(driver, "has_pending_interrupt", None)
        if check is None:
            # Deterministic offline drivers can implement transitions directly;
            # the production driver always supplies structural checkpoint proof.
            return True
        return bool(
            check(
                cycle=cycle,
                task=task,
                kind=kind,
                occurrence_key=occurrence_key,
            )
        )

    def _phase_prompt(self, cycle: WorkflowCycle, task: TaskRun) -> str:
        root = self._root_event(cycle)
        completed = [
            item.task_id
            for item in self.runtime.task_runs(cycle.workflow_cycle_id)
            if item.status == TaskPhase.DONE
        ]
        feedback = "\n".join(
            str(
                item.get("feedback")
                or item.get("summary")
                or item.get("instructions")
                or ""
            )
            for item in task.repair_feedback
        )
        cumulative_history = str(list(task.repair_feedback))[:10_000]
        root_request = normalize_task(self._root_body(cycle, root))
        return (
            f"Workflow task: {task.task_id}\n"
            f"Phase: {task.phase.value}\n"
            f"Completed dependencies/tasks: {completed}\n"
            f"Root request (untrusted): {root_request}\n"
            f"Validation/repair feedback: {feedback or '(none)'}\n"
            "Durable cumulative task history (preserve prior intended and "
            f"implemented behavior when replanning): {cumulative_history}"
        )

    def _authorized_approver(self, repo_name: str, login: str | None) -> bool:
        if not login or self.client is None:
            return False
        try:
            repo = self.client.repository(repo_name)
            permission = self.client.collaborator_permission(repo, login)
        except Exception:
            return False
        return str(permission).lower() in {"admin", "maintain", "write"}

    def _consume(self, event: Any, cycle: WorkflowCycle, status: str) -> None:
        self.store.record_input_disposition(
            event["event_key"],
            thread_id=cycle.thread_id,
            cycle_id=cycle.cycle_id,
            status=status,
            recorded_at=self.clock(),
        )

    @staticmethod
    def _same_target(event: Any, root: Any) -> bool:
        if (
            event["origin_surface"] != root["origin_surface"]
            or event["subject_number"] != root["subject_number"]
        ):
            return False
        if root["origin_surface"] == "PR_INLINE_REVIEW":
            return event["review_thread_root_id"] == root["review_thread_root_id"]
        return True

    @staticmethod
    def _after(value: str | None, threshold: str) -> bool:
        try:
            return bool(value and parse_timestamp(value) > parse_timestamp(threshold))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _result(cycle: WorkflowCycle, task: TaskRun, status: str) -> ControllerResult:
        return ControllerResult(
            cycle.thread_id,
            status,
            cycle.workflow_cycle_id,
            task.task_id,
            task.phase,
        )
