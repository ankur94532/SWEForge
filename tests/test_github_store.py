from sweforge.github_models import (
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import SQLiteGitHubStore


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
        )
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
        )
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
        )
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
