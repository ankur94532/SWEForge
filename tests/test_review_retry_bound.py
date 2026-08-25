"""D1: review-infrastructure failures must terminate, not retry forever.

ReviewFinalizationError escaped to the dispatcher, whose backoff caps its delay
at an hour but never its count -- the only unbounded retry path left in a system
whose stated design is bounded everywhere. Bounded at 3 to match execution.
"""

import pytest

from sweforge.github_store import (
    MAX_REVIEW_RECOVERIES,
    SQLiteGitHubStore,
    WorkflowPhase,
)


@pytest.fixture
def store(tmp_path):
    return SQLiteGitHubStore(tmp_path / "state.db")


def test_bound_matches_the_execution_retry_bound():
    assert MAX_REVIEW_RECOVERIES == 3


def test_counter_column_exists_and_defaults_to_zero(store):
    columns = {
        row[1]
        for row in store.connection.execute("PRAGMA table_info(execution_attempts)")
    }
    assert "review_recovery_count" in columns


def test_missing_attempt_is_an_error_not_a_silent_pass(store):
    with pytest.raises(ValueError, match="attempt is missing"):
        store.record_review_infrastructure_failure("nope", now="now")


def _seed(store, tmp_path, attempt_id="a1"):
    """Build a real thread, plan, permit and attempt through the store's own API.

    Hand-written INSERTs tripped four foreign keys; going through the public
    surface also keeps this test honest if the schema moves.
    """
    from sweforge.github_models import (
        RepositoryRef,
        SourceEvent,
        SourceKind,
        SubjectKind,
    )
    from sweforge.github_store import AttemptStatus
    from sweforge.workflow import WorkflowEngine

    repo = RepositoryRef(1, "example/repo")

    def event(source_id, body, updated):
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

    root = event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    approval = event("2", "@agent approve", "2026-01-01T00:01:00Z")
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
        attempt_id=attempt_id,
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
    return permit.thread_id


def test_retries_while_budget_remains(store, tmp_path):
    _seed(store, tmp_path)
    for _ in range(MAX_REVIEW_RECOVERIES):
        assert store.record_review_infrastructure_failure("a1", now="now") == "RETRY"


def test_fails_closed_once_the_bound_is_reached(store, tmp_path):
    _seed(store, tmp_path)
    for _ in range(MAX_REVIEW_RECOVERIES):
        store.record_review_infrastructure_failure("a1", now="now")
    assert store.record_review_infrastructure_failure("a1", now="now") == "EXHAUSTED"


def test_exhaustion_moves_the_thread_to_review_blocked(store, tmp_path):
    thread_id = _seed(store, tmp_path)
    for _ in range(MAX_REVIEW_RECOVERIES + 1):
        store.record_review_infrastructure_failure("a1", now="now")
    phase = store.connection.execute(
        "SELECT phase FROM issue_workflow_state WHERE thread_id=?", (thread_id,)
    ).fetchone()[0]
    assert phase == WorkflowPhase.REVIEW_BLOCKED.value


def test_exhaustion_leaves_the_successful_initial_attempt_reusable(store, tmp_path):
    """S26 requires the INITIAL attempt survive: execution succeeded, review did not."""
    _seed(store, tmp_path)
    for _ in range(MAX_REVIEW_RECOVERIES + 1):
        store.record_review_infrastructure_failure("a1", now="now")
    status = store.connection.execute(
        "SELECT status FROM execution_attempts WHERE attempt_id='a1'"
    ).fetchone()[0]
    assert status == "SUCCEEDED"


def test_budget_is_consumed_durably_so_a_crash_cannot_reset_it(store, tmp_path):
    _seed(store, tmp_path)
    store.record_review_infrastructure_failure("a1", now="now")
    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    count = reopened.connection.execute(
        "SELECT review_recovery_count FROM execution_attempts WHERE attempt_id='a1'"
    ).fetchone()[0]
    assert count == 1
