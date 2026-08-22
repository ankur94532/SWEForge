from dataclasses import replace
from types import SimpleNamespace

from sweforge.execution import thread_lock
from sweforge.github_store import (
    AttemptKind,
    AttemptStatus,
    PlanStatus,
    WorkflowMode,
    WorkflowPhase,
    WorkflowStateRecord,
)
from sweforge.workflow import WorkflowEngine


def state():
    return WorkflowStateRecord(
        thread_id="thread-1",
        repo_id=1,
        repo_full_name="example/repo",
        issue_number=7,
        phase=WorkflowPhase.EXECUTING,
        cycle_id=1,
        root_event_key="root-1",
        current_plan_id="plan-1",
        mode=WorkflowMode.INTERACTIVE,
        created_at="now",
        updated_at="now",
    )


def attempt(kind, status=AttemptStatus.RUNNING):
    return SimpleNamespace(
        attempt_id="attempt-2" if kind is AttemptKind.REVIEW_REPAIR else "attempt-1",
        kind=kind,
        status=status,
    )


class RecoveryStore:
    def __init__(self, current):
        self.current = current
        self.state = state()
        self.recovered = False
        self.failed_closed = False

    def workflow_state(self, _thread_id):
        return self.state

    def current_plan(self, _thread_id):
        return None

    def latest_attempt(self, _thread_id, _cycle_id):
        return self.current

    def execution_for_event(self, _event_key):
        return {
            "status": "SUCCEEDED",
            "completed_at": "now",
            "response_text": "done",
            "start_head_sha": "base",
            "end_head_sha": "head",
            "end_dirty": 0,
        }

    def unconsumed_inputs(self, *_args, **_kwargs):
        return []

    def recover_orphaned_repair_attempt(self, _attempt_id, *, now):
        self.recovered = True
        self.current.status = AttemptStatus.FAILED
        self.state = replace(self.state, phase=WorkflowPhase.REPAIR_READY)

    def fail_closed_repair_recovery(self, _attempt_id, *, now):
        self.failed_closed = True
        self.state = replace(self.state, phase=WorkflowPhase.REVIEW_BLOCKED)


def test_active_repair_recovery_returns_busy_without_using_root_success(tmp_path):
    repair = attempt(AttemptKind.REVIEW_REPAIR)
    store = RecoveryStore(repair)
    reviewer_called = False
    engine = WorkflowEngine(
        store=store,
        reviewer=lambda **_: reviewer_called,
    )
    with thread_lock(tmp_path / "locks", store.state.thread_id):
        result = engine.advance(
            thread_id=store.state.thread_id,
            model="model",
            repo_paths={},
            workspace_root=tmp_path,
            execute_kwargs={"lock_root": tmp_path / "locks"},
        )
    assert result.phase is WorkflowPhase.EXECUTING
    assert result.message == "busy"
    assert repair.status is AttemptStatus.RUNNING
    assert not reviewer_called
    assert not store.recovered


def test_orphaned_repair_recovery_preserves_attempt_for_retry(tmp_path):
    repair = attempt(AttemptKind.REVIEW_REPAIR)
    store = RecoveryStore(repair)
    engine = WorkflowEngine(store=store)
    result = engine.advance(
        thread_id=store.state.thread_id,
        model="model",
        repo_paths={},
        workspace_root=tmp_path,
        execute_kwargs={"lock_root": tmp_path / "locks"},
    )
    assert result.phase is WorkflowPhase.REPAIR_READY
    assert repair.attempt_id == "attempt-2"
    assert repair.status is AttemptStatus.FAILED
    assert store.recovered
    assert not store.failed_closed


def test_initial_recovery_remains_separate_from_repair_recovery(tmp_path):
    initial = attempt(AttemptKind.INITIAL)
    store = RecoveryStore(initial)
    plan = SimpleNamespace(plan_id="plan-1", version=1, status=PlanStatus.APPROVED)
    permit = SimpleNamespace(
        permit_id="permit-1",
        thread_id="thread-1",
        cycle_id=1,
        plan_id="plan-1",
        plan_version=1,
        root_event_key="root-1",
    )
    store.current_plan = lambda _thread_id: plan
    store.permit_for_plan = lambda _plan_id: permit
    store.ensure_execution_attempt = lambda **_: initial
    store.finish_execution_attempt = lambda *args, **kwargs: setattr(
        initial, "status", AttemptStatus.SUCCEEDED
    )
    store.save_workflow_state = lambda updated: setattr(store, "state", updated)
    engine = WorkflowEngine(store=store)
    recovered, busy = engine._recover_executing_state(
        store.state, lock_root=tmp_path / "locks"
    )
    assert not busy
    assert recovered.phase is WorkflowPhase.REVIEW_EXECUTION
    assert initial.status is AttemptStatus.SUCCEEDED
    assert not store.recovered
