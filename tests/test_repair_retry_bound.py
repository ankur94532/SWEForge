"""The repair-execution retry bound must terminate, not retry forever.

MAX_REPAIR_EXECUTION_RECOVERIES governs how often an orphaned repair attempt
is revived. Nothing referenced it: the only test touching
recover_orphaned_repair_attempt stubbed the method out, so the exhaustion
branch in the real store had never run. It is the third of three retry bounds
(initial execution, review, repair) and the only one that was unproven.
"""

import json

import pytest

from sweforge.github_models import (
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import (
    MAX_REPAIR_EXECUTION_RECOVERIES,
    AttemptStatus,
    ExecutionReviewRecord,
    SQLiteGitHubStore,
    WorkflowPhase,
)
from sweforge.workflow import WorkflowEngine


@pytest.fixture
def store(tmp_path):
    return SQLiteGitHubStore(tmp_path / "state.db")


def _event(repo, source_id, body, updated):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=source_id,
        source_updated_at=updated,
        source_created_at=updated,
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body=body,
        html_url=None,
    )


def _seed_repair_attempt(store):
    """Drive a real thread to a bound repair attempt through the public API."""
    repo = RepositoryRef(1, "example/repo")
    root = _event(repo, "1", "@agent fix it", "2026-01-01T00:00:00Z")
    approval = _event(repo, "2", "@agent approve", "2026-01-01T00:01:00Z")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [root, approval],
        since="now",
        etag=None,
        polled_at="now",
    )
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    plan = engine.start_cycle(
        event_key=root.event_key, plan_text="edit README", posted_comment_id=1
    )
    permit = engine.approve(event_key=approval.event_key)
    attempt = store.ensure_execution_attempt(
        attempt_id="a1",
        thread_id=permit.thread_id,
        cycle_id=permit.cycle_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        root_event_key=plan.root_event_key,
        authorization_id=permit.permit_id,
        created_at="now",
    )
    store.finish_execution_attempt(
        attempt.attempt_id,
        status=AttemptStatus.SUCCEEDED,
        completed_at="now",
        response_text="done",
        start_head_sha="a",
        end_head_sha="b",
        end_dirty=False,
    )
    # A repair permit is only mintable against a NEEDS_FIXES review.
    store.save_execution_review(
        ExecutionReviewRecord(
            review_id="r1",
            thread_id=permit.thread_id,
            cycle_id=permit.cycle_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            root_event_key=plan.root_event_key,
            attempt_id=attempt.attempt_id,
            review_iteration=1,
            verdict="NEEDS_FIXES",
            summary="needs work",
            findings_json=json.dumps([]),
            repair_instructions_json=json.dumps([]),
            created_at="now",
            completed_at="now",
        )
    )
    # A repair permit is only mintable while the thread is in REVIEW_EXECUTION.
    # The workflow reaches that phase when review begins; there is no public
    # store method for the transition, and this test exercises the store's
    # recovery bound in isolation from the workflow that drives it.
    store.connection.execute(
        "UPDATE issue_workflow_state SET phase=? WHERE thread_id=?",
        (WorkflowPhase.REVIEW_EXECUTION.value, permit.thread_id),
    )
    store.connection.commit()
    repair_permit = store.create_repair_permit(thread_id=permit.thread_id, now="now")
    repair = store.begin_or_resume_repair_attempt(repair_permit.permit_id, now="now")
    return permit.thread_id, repair


def _count(store, attempt_id):
    return store.connection.execute(
        "SELECT status, repair_recovery_count FROM execution_attempts "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()


def _drive_recoveries(store, repair, times):
    """Orphan and revive the repair attempt `times` times.

    Recovery retires the orphaned attempt and moves the thread to
    REPAIR_READY; the count only accumulates when the same attempt is then
    resumed, which is what the dispatcher does.
    """
    for _ in range(times):
        store.recover_orphaned_repair_attempt(repair.attempt_id, now="now")
        store.begin_or_resume_repair_attempt(repair.authorization_id, now="now")


def test_the_repair_bound_matches_the_other_two_retry_bounds():
    assert MAX_REPAIR_EXECUTION_RECOVERIES == 3


def test_the_counter_column_exists(store):
    columns = {
        row[1]
        for row in store.connection.execute("PRAGMA table_info(execution_attempts)")
    }
    assert "repair_recovery_count" in columns


def test_recovery_revives_the_same_attempt_while_budget_remains(store):
    _thread_id, repair = _seed_repair_attempt(store)
    _drive_recoveries(store, repair, MAX_REPAIR_EXECUTION_RECOVERIES)
    row = _count(store, repair.attempt_id)
    assert row["repair_recovery_count"] == MAX_REPAIR_EXECUTION_RECOVERIES
    assert row["status"] == AttemptStatus.RUNNING.value, "failed before the bound"


def test_the_attempt_fails_once_the_bound_is_exceeded(store):
    """The property: repair retries terminate rather than looping forever."""
    _thread_id, repair = _seed_repair_attempt(store)
    _drive_recoveries(store, repair, MAX_REPAIR_EXECUTION_RECOVERIES)
    store.recover_orphaned_repair_attempt(repair.attempt_id, now="now")
    row = _count(store, repair.attempt_id)
    assert row["status"] == AttemptStatus.FAILED.value


def test_the_counter_does_not_advance_past_the_bound(store):
    """Exhaustion stops counting rather than running away."""
    _thread_id, repair = _seed_repair_attempt(store)
    _drive_recoveries(store, repair, MAX_REPAIR_EXECUTION_RECOVERIES)
    store.recover_orphaned_repair_attempt(repair.attempt_id, now="now")
    assert (
        _count(store, repair.attempt_id)["repair_recovery_count"]
        == MAX_REPAIR_EXECUTION_RECOVERIES
    )


def test_exhaustion_invalidates_the_repair_permit(store):
    """An exhausted repair must not leave live authorization behind."""
    _thread_id, repair = _seed_repair_attempt(store)
    _drive_recoveries(store, repair, MAX_REPAIR_EXECUTION_RECOVERIES)
    store.recover_orphaned_repair_attempt(repair.attempt_id, now="now")
    row = store.connection.execute(
        "SELECT invalidated_at FROM review_repair_permits WHERE permit_id=?",
        (repair.authorization_id,),
    ).fetchone()
    assert row["invalidated_at"] is not None


def test_no_repair_can_resume_after_exhaustion(store):
    """The security-relevant end state: no path back to execution."""
    _thread_id, repair = _seed_repair_attempt(store)
    _drive_recoveries(store, repair, MAX_REPAIR_EXECUTION_RECOVERIES)
    store.recover_orphaned_repair_attempt(repair.attempt_id, now="now")
    with pytest.raises(ValueError, match="repair permit is unavailable"):
        store.begin_or_resume_repair_attempt(repair.authorization_id, now="now")
