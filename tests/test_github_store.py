import sqlite3
from dataclasses import replace

import pytest

from sweforge.github_models import (
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import (
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
)


def event(repo: RepositoryRef, source_id: str = "1", number: int = 7) -> SourceEvent:
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id=source_id,
        source_updated_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=number,
        author_login="octocat",
        body="@agent fix this",
        html_url="https://github.com/example/repo/issues/7",
    )


def test_thread_identity_and_event_idempotency_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    repo = RepositoryRef(12345, "example/repo")
    store = SQLiteGitHubStore(path)
    store.upsert_repository(repo.repo_id, repo.full_name, "2026-01-01T00:00:00Z")
    assert (
        store.record_batch(
            repo.repo_id,
            "issues",
            [event(repo)],
            since="2025-12-31T23:00:00Z",
            etag="one",
            polled_at="2026-01-01T00:00:01Z",
        ).events_persisted
        == 1
    )
    assert (
        store.record_batch(
            repo.repo_id,
            "issues",
            [event(repo)],
            since="2025-12-31T23:00:00Z",
            etag="one",
            polled_at="2026-01-01T00:00:02Z",
        ).events_persisted
        == 0
    )
    store.close()

    reopened = SQLiteGitHubStore(path)
    assert reopened.events()[0]["thread_id"] == "github:12345:issue:7"
    assert reopened.cursor(12345, "issues")["etag"] == "one"
    reopened.close()


def test_pr_mapping_routes_existing_unrouted_events(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(12345, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    pr_event = SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.REVIEW_COMMENT,
        source_id="99",
        source_updated_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=12,
        author_login="octocat",
        body="@agent please fix",
        html_url=None,
    )
    assert (
        store.record_batch(
            repo.repo_id,
            "review_comments",
            [pr_event],
            since="now",
            etag=None,
            polled_at="now",
        ).events_persisted
        == 1
    )
    assert store.events()[0]["thread_id"] is None

    issue_thread = "github:12345:issue:7"
    store.record_batch(
        repo.repo_id,
        "issues",
        [event(repo)],
        since="now",
        etag=None,
        polled_at="now",
    )
    store.register_pr_mapping(repo.repo_id, 12, issue_thread)
    assert store.events()[0]["thread_id"] == issue_thread
    store.close()


def test_duplicate_event_does_not_update_thread_activity(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(12345, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issues",
        [event(repo)],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )
    store.record_batch(
        repo.repo_id,
        "issues",
        [event(repo)],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:02Z",
    )
    assert store.threads()[0]["updated_at"] == "2026-01-01T00:00:01Z"
    edited = event(repo, source_id="1", number=7)
    edited = replace(edited, source_updated_at="2026-01-01T00:01:00Z")
    store.record_batch(
        repo.repo_id,
        "issues",
        [edited],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:01:01Z",
    )
    assert store.threads()[0]["updated_at"] == "2026-01-01T00:01:01Z"
    store.close()


def test_cursor_failure_rolls_back_inserted_event(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(12345, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.connection.execute(
        """CREATE TRIGGER fail_cursor BEFORE INSERT ON poll_cursors
           BEGIN SELECT RAISE(ABORT, 'cursor failure'); END"""
    )
    try:
        store.record_batch(
            repo.repo_id,
            "issues",
            [event(repo)],
            since="now",
            etag=None,
            polled_at="now",
        )
    except Exception as exc:
        assert "cursor failure" in str(exc)
    else:
        raise AssertionError("record_batch unexpectedly succeeded")
    assert not store.events()
    assert store.cursor(repo.repo_id, "issues") is None
    assert not store.threads()
    store.close()


def test_pr_mapping_requires_same_repo_and_rejects_conflicts(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    first = RepositoryRef(1, "example/one")
    second = RepositoryRef(2, "example/two")
    for repo in (first, second):
        store.upsert_repository(repo.repo_id, repo.full_name, "now")
        store.record_batch(
            repo.repo_id,
            "issues",
            [event(repo, number=7)],
            since="now",
            etag=None,
            polled_at="now",
        )
    store.record_batch(
        first.repo_id,
        "issues",
        [event(first, source_id="2", number=8)],
        since="now",
        etag=None,
        polled_at="now",
    )
    first_thread = "github:1:issue:7"
    conflicting_thread = "github:1:issue:8"
    store.register_pr_mapping(1, 12, first_thread)
    store.register_pr_mapping(1, 12, first_thread)
    with pytest.raises(ValueError, match="another thread"):
        store.register_pr_mapping(1, 12, conflicting_thread)
    with pytest.raises(ValueError, match="belong to the repository"):
        store.register_pr_mapping(1, 14, "github:2:issue:7")
    store.close()


def test_existing_state_db_migrates_execution_baselines(tmp_path):
    path = tmp_path / "state.db"
    initial = SQLiteGitHubStore(path)
    initial.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE event_executions")
    connection.execute(
        """CREATE TABLE event_executions (
           event_key TEXT PRIMARY KEY,
           thread_id TEXT NOT NULL,
           status TEXT NOT NULL,
           attempt_count INTEGER NOT NULL,
           started_at TEXT NOT NULL,
           completed_at TEXT,
           response_text TEXT,
           error_message TEXT,
           workspace_path TEXT
        )"""
    )
    connection.commit()
    connection.close()

    migrated = SQLiteGitHubStore(path)
    columns = {
        row[1]
        for row in migrated.connection.execute("PRAGMA table_info(event_executions)")
    }
    assert {"start_head_sha", "end_head_sha", "end_dirty"} <= columns
    migrated.close()


def test_resolved_issue_snapshot_is_not_a_new_task(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(12345, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    first = event(repo)
    edited_snapshot = replace(first, source_updated_at="2026-01-01T00:01:00Z")
    store.record_batch(
        repo.repo_id,
        "issues",
        [first, edited_snapshot],
        since="now",
        etag=None,
        polled_at="now",
    )
    claim = store.claim_next_event(now="now")
    assert claim and claim.event_key == first.event_key
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            claim.thread_id,
            repo.repo_id,
            repo.full_name,
            7,
            "/tmp/repository",
            "/tmp/workspace",
            "sweforge/issue-7",
            "base",
            "now",
            "now",
        )
    )
    store.mark_execution_succeeded(
        first.event_key,
        completed_at="later",
        response_text="ok",
        workspace_path="/tmp/workspace",
        start_head_sha="base",
        end_head_sha="base",
        end_dirty=False,
    )
    store.record_batch(
        repo.repo_id,
        "issues",
        [edited_snapshot],
        since="later",
        etag=None,
        polled_at="latest",
    )
    assert store.execution_for_event(edited_snapshot.event_key) is None
    assert store.claim_next_event(now="latest").event_key == edited_snapshot.event_key
    store.close()


def test_publication_uses_issue_thread_number_for_pr_event(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(12345, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    issue = event(repo)
    store.record_batch(
        repo.repo_id, "issues", [issue], since="now", etag=None, polled_at="now"
    )
    pr_event = replace(
        issue,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id="comment-1",
        source_updated_at="2026-01-01T00:01:00Z",
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=99,
    )
    store.register_pr_mapping(repo.repo_id, 99, "github:12345:issue:7")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [pr_event],
        since="now",
        etag=None,
        polled_at="later",
    )
    store.claim_next_event(now="now")
    store.mark_execution_succeeded(
        issue.event_key,
        completed_at="later",
        response_text="ok",
        workspace_path="/workspace",
        start_head_sha="base",
        end_head_sha="base",
        end_dirty=False,
    )
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            "github:12345:issue:7",
            repo.repo_id,
            repo.full_name,
            7,
            "/repository",
            "/workspace",
            "sweforge/issue-7",
            "base",
            "now",
            "now",
        )
    )
    claim = store.claim_next_event(now="later")
    assert claim and claim.event_key == pr_event.event_key
    store.mark_execution_succeeded(
        pr_event.event_key,
        completed_at="latest",
        response_text="ok",
        workspace_path="/workspace",
        start_head_sha="base",
        end_head_sha="base",
        end_dirty=False,
    )
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            "github:12345:issue:7",
            repo.repo_id,
            repo.full_name,
            7,
            "/repository",
            "/workspace",
            "sweforge/issue-7",
            "base",
            "now",
            "now",
        )
    )
    with pytest.raises(ValueError, match="ACCEPT review"):
        store.ensure_publication(pr_event.event_key, now="latest")
    store.close()
