"""Bounded recovery of an INITIAL execution whose worker died.

Repair recovery already existed; an orphaned INITIAL execution previously had
no transition at all and left the IssueThread in EXECUTING forever.  Recovery
keeps the same lifecycle identity, never invents success, and is bounded so a
permanently crashing execution cannot spend model budget indefinitely.
"""

import threading
from pathlib import Path

import pytest
from test_publication_identity import THREAD_ID, Harness, approval, source_event

from sweforge.execution import thread_lock
from sweforge.github_store import (
    MAX_INITIAL_EXECUTION_RECOVERIES,
    AttemptStatus,
    WorkflowPhase,
)


def authorized(tmp_path):
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix it", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    return harness, origin


def recover(harness):
    """Run only the recovery transition, without letting advance re-execute."""
    state = harness.store.workflow_state(THREAD_ID)
    return harness.engine._recover_executing_state(
        state, lock_root=harness.tmp_path / "locks"
    )


def advance(harness, runner=None, filename="fix.txt"):
    execute_kwargs = harness.execute_kwargs(filename)
    if runner is not None:
        execute_kwargs["runner"] = runner
    execute_kwargs["lock_root"] = harness.tmp_path / "locks"
    return harness.engine.advance(
        thread_id=THREAD_ID,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )


class HardCrash(BaseException):
    """Models a process abort: `_execute_claim` only catches Exception."""


def hard_crash(harness, *, leave_partial_work=False, runs=None):
    """Leave exactly the durable state a killed worker leaves behind.

    A BaseException escapes `_execute_claim`'s handler, so nothing is marked
    failed: the attempt stays RUNNING, the execution stays RUNNING and the
    workflow stays EXECUTING -- an orphan, just like SIGKILL.
    """

    def runner(**kwargs):
        if runs is not None:
            runs.append(1)
        if leave_partial_work:
            Path(kwargs["worktree"], "partial.txt").write_text("half-done\n")
        raise HardCrash("worker died")

    with pytest.raises(HardCrash):
        advance(harness, runner=runner)
    state = harness.store.workflow_state(THREAD_ID)
    assert state.phase is WorkflowPhase.EXECUTING
    attempt = harness.store.latest_attempt(THREAD_ID, state.cycle_id)
    assert attempt is not None and attempt.status is AttemptStatus.RUNNING


def test_durable_success_is_reconstructed_without_re_executing(tmp_path):
    """EXECUTING plus a SUCCEEDED execution must reach review, not run again."""
    harness, origin = authorized(tmp_path)
    runs: list[int] = []

    def runner(**kwargs):
        runs.append(1)
        Path(kwargs["worktree"], "fix.txt").write_text("fix\n")
        return "done"

    assert advance(harness, runner=runner).phase is WorkflowPhase.REVIEW_EXECUTION
    assert len(runs) == 1
    # A crash after success persisted but before the phase update.
    harness.store.connection.execute(
        "UPDATE issue_workflow_state SET phase=? WHERE thread_id=?",
        (WorkflowPhase.EXECUTING.value, THREAD_ID),
    )
    harness.store.connection.commit()

    assert advance(harness, runner=runner).phase is not WorkflowPhase.EXECUTING
    assert len(runs) == 1, "recovery re-ran a successful execution"
    attempt = harness.store.latest_attempt(THREAD_ID, 1)
    assert attempt.status is AttemptStatus.SUCCEEDED
    harness.store.close()


def test_a_live_worker_is_never_recovered(tmp_path):
    """An unavailable IssueThread lock means BUSY and no mutation."""
    harness, _ = authorized(tmp_path)
    hard_crash(harness)
    before = harness.store.latest_attempt(THREAD_ID, 1)

    holder_ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with thread_lock(harness.tmp_path / "locks", THREAD_ID):
            holder_ready.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holder_ready.wait(timeout=10)
    try:
        _, busy = recover(harness)
        assert busy is True
        result = advance(harness)
        assert result.phase is WorkflowPhase.EXECUTING
        assert result.message == "busy"
        assert harness.store.workflow_state(THREAD_ID).phase is WorkflowPhase.EXECUTING
        assert harness.store.latest_attempt(THREAD_ID, 1) == before
    finally:
        release.set()
        holder.join(timeout=10)
    harness.store.close()


def test_orphaned_initial_attempt_becomes_a_durable_retry(tmp_path):
    harness, origin = authorized(tmp_path)
    hard_crash(harness)

    recovered, busy = recover(harness)
    assert busy is False
    state = harness.store.workflow_state(THREAD_ID)
    attempt = harness.store.latest_attempt(THREAD_ID, 1)
    # Same lifecycle: same cycle, plan and attempt identity.
    assert state.cycle_id == 1
    assert attempt.kind.value == "INITIAL"
    assert attempt.status is AttemptStatus.INTERRUPTED
    # Nothing pretends the dead attempt succeeded.
    execution = harness.store.execution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert execution["status"] == "RETRY_PENDING"
    assert recovered.phase is WorkflowPhase.EXECUTION_READY
    # The permit is released so the same authorization can be reused.
    permit = harness.store.permit_for_plan(state.current_plan_id)
    assert permit.invalidated_at is None
    harness.store.close()


def test_recovered_execution_resumes_the_same_lifecycle_and_workspace(tmp_path):
    harness, origin = authorized(tmp_path)
    hard_crash(harness, leave_partial_work=True)
    workspace = harness.store.thread_workspace(THREAD_ID)
    partial = Path(workspace.workspace_path) / "partial.txt"
    assert partial.exists(), "the crashed worker left partial work"

    recover(harness)
    runs: list[str] = []

    def runner(**kwargs):
        runs.append(kwargs["worktree"])
        # The partial work from the dead attempt is still here.
        assert Path(kwargs["worktree"], "partial.txt").exists()
        Path(kwargs["worktree"], "fix.txt").write_text("fix\n")
        return "recovered"

    assert advance(harness, runner=runner).phase is WorkflowPhase.REVIEW_EXECUTION
    assert runs == [workspace.workspace_path]
    assert partial.exists(), "recovery must not silently reset the worktree"
    # Same cycle and same logical execution identity throughout.
    assert harness.store.workflow_state(THREAD_ID).cycle_id == 1
    assert (
        harness.store.issue_resolution_for_cycle(
            thread_id=THREAD_ID,
            cycle_id=1,
            root_event_key=origin.event_key,
            root_input_id=None,
        )
        is None
    )
    harness.store.close()


def test_a_dirty_workspace_is_never_treated_as_success(tmp_path):
    """Partial edits are not evidence that the execution completed."""
    harness, origin = authorized(tmp_path)
    hard_crash(harness, leave_partial_work=True)
    recover(harness)
    execution = harness.store.execution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert execution["status"] != "SUCCEEDED"
    assert harness.store.latest_attempt(THREAD_ID, 1).status is not (
        AttemptStatus.SUCCEEDED
    )
    assert harness.store.eligible_publication_id(THREAD_ID) is None
    harness.store.close()


def test_recovery_claims_budget_before_the_next_model_run(tmp_path):
    """retry_count is durable before another execution starts."""
    harness, _ = authorized(tmp_path)
    hard_crash(harness)
    # retry_count counts executions started; the crashed one already counted.
    assert harness.store.latest_attempt(THREAD_ID, 1).retry_count == 1

    observed: list[int] = []

    def runner(**kwargs):
        # Reading through a second connection proves it is committed, not
        # merely pending in this worker's transaction.
        from sweforge.github_store import SQLiteGitHubStore

        peek = SQLiteGitHubStore(harness.db)
        observed.append(peek.latest_attempt(THREAD_ID, 1).retry_count)
        peek.close()
        raise HardCrash("worker died again")

    with pytest.raises(HardCrash):
        advance(harness, runner=runner)
    assert observed == [2], "budget was not durable before the model ran"
    harness.store.close()


def test_a_hard_crash_still_consumes_budget(tmp_path):
    """A BaseException models a process abort: the claim is already durable."""
    from sweforge.github_store import SQLiteGitHubStore

    harness, _ = authorized(tmp_path)
    hard_crash(harness)
    peek = SQLiteGitHubStore(harness.db)
    assert peek.latest_attempt(THREAD_ID, 1).retry_count == 1
    peek.close()

    recover(harness)  # hands the same attempt back
    hard_crash(harness)
    peek = SQLiteGitHubStore(harness.db)
    assert peek.latest_attempt(THREAD_ID, 1).retry_count == 2
    peek.close()
    harness.store.close()


def test_a_permanently_crashing_execution_is_bounded(tmp_path):
    """Restart forever, but spend a bounded number of model invocations."""
    harness, origin = authorized(tmp_path)
    runs: list[int] = []

    def runner(**kwargs):
        runs.append(1)
        raise HardCrash("always dies")

    for _ in range(2 * MAX_INITIAL_EXECUTION_RECOVERIES + 6):
        try:
            advance(harness, runner=runner)
        except HardCrash:
            pass

    assert len(runs) == MAX_INITIAL_EXECUTION_RECOVERIES
    final = harness.store.workflow_state(THREAD_ID)
    assert final.phase is WorkflowPhase.REVIEW_BLOCKED
    attempt = harness.store.latest_attempt(THREAD_ID, 1)
    assert attempt.status is AttemptStatus.FAILED

    # Fail closed: no publication, no plan completion, no solved-issue record.
    assert harness.store.eligible_publication_id(THREAD_ID) is None
    assert harness.store.publications_for_event(origin.event_key) == []
    assert harness.store.current_plan(THREAD_ID).status.value != "EXECUTED"
    assert (
        harness.store.connection.execute(
            "SELECT count(*) FROM issue_resolution_memory"
        ).fetchone()[0]
        == 0
    )

    # And it stays there rather than spinning.
    before = len(runs)
    for _ in range(3):
        try:
            advance(harness, runner=runner)
        except HardCrash:
            pass
    assert len(runs) == before
    assert harness.store.workflow_state(THREAD_ID).phase is WorkflowPhase.REVIEW_BLOCKED
    harness.store.close()


def test_recovery_fails_closed_when_authorization_no_longer_holds(tmp_path):
    harness, _ = authorized(tmp_path)
    hard_crash(harness)
    state = harness.store.workflow_state(THREAD_ID)
    harness.store.invalidate_permits(THREAD_ID, state.cycle_id, now="later")
    harness.store.connection.execute(
        "UPDATE execution_permits SET invalidated_at='later' WHERE thread_id=?",
        (THREAD_ID,),
    )
    harness.store.connection.commit()

    recover(harness)
    assert harness.store.workflow_state(THREAD_ID).phase is WorkflowPhase.REVIEW_BLOCKED
    assert harness.store.latest_attempt(THREAD_ID, 1).status is AttemptStatus.FAILED
    harness.store.close()
