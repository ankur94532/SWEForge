from datetime import UTC, datetime, timedelta

import pytest

from sweforge.github_models import PollResponse, RepositoryRef
from sweforge.github_poller import GitHubPoller
from sweforge.github_store import SQLiteGitHubStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def issue_item(number=7, updated="2026-01-01T00:00:00Z", body="@agent fix this"):
    return {
        "id": number + 100,
        "number": number,
        "updated_at": updated,
        "body": body,
        "html_url": f"https://github.com/example/repo/issues/{number}",
        "user": {"login": "octocat"},
    }


def comment_item(
    comment_id=9,
    number=7,
    updated="2026-01-01T00:00:00Z",
    body="@agent fix this",
):
    return {
        "id": comment_id,
        "updated_at": updated,
        "body": body,
        "issue_url": f"https://api.github.com/repos/example/repo/issues/{number}",
        "html_url": "https://github.com/example/repo/issues/7#issuecomment-9",
        "user": {"login": "octocat"},
    }


class FakeGitHub:
    def __init__(self, repositories, responses=None, issue_payloads=None):
        self.repositories = repositories
        self.responses = responses or {}
        self.issue_payloads = issue_payloads or {}

    def repository(self, full_name):
        return self.repositories[full_name]

    def issues(self, repo, since, etag):
        return self.responses.get((repo.repo_id, "issues"), PollResponse())

    def issue_comments(self, repo, since, etag):
        return self.responses.get((repo.repo_id, "issue_comments"), PollResponse())

    def review_comments(self, repo, since, etag):
        return self.responses.get((repo.repo_id, "review_comments"), PollResponse())

    def issue(self, repo, number):
        return self.issue_payloads.get((repo.repo_id, number), {})


def poller(fake, store):
    return GitHubPoller(
        fake,
        store,
        now=lambda: NOW,
        initial_lookback=timedelta(minutes=5),
    )


def test_issue_event_is_persisted_once_and_thread_is_deterministic(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {(123, "issues"): PollResponse((issue_item(),))},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    first = poller(fake, store).poll([repo.full_name])
    second = poller(fake, store).poll([repo.full_name])

    assert first.persisted == 1
    assert second.persisted == 0
    assert [row["thread_id"] for row in store.events()] == ["github:123:issue:7"]
    store.close()


def test_edited_comment_creates_one_new_event(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {(123, "issue_comments"): PollResponse((comment_item(body="ordinary"),))},
        {(123, 7): {}},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    assert poller(fake, store).poll([repo.full_name]).persisted == 0
    fake.responses[(123, "issue_comments")] = PollResponse(
        (comment_item(updated="2026-01-01T00:01:00Z"),)
    )
    assert poller(fake, store).poll([repo.full_name]).persisted == 1
    assert poller(fake, store).poll([repo.full_name]).persisted == 0
    assert len(store.events()) == 1
    store.close()


def test_pr_comments_are_unrouted_without_mapping(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {
            (123, "issue_comments"): PollResponse((comment_item(number=12),)),
            (123, "review_comments"): PollResponse(
                (
                    {
                        "id": 10,
                        "updated_at": "2026-01-01T00:00:00Z",
                        "body": "@agent review this",
                        "pull_request_url": "https://api.github.com/repos/example/repo/pulls/12",
                        "user": {"login": "octocat"},
                    },
                )
            ),
        },
        {(123, 12): {"pull_request": {"url": "https://example/pr/12"}}},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    result = poller(fake, store).poll([repo.full_name])

    assert result.persisted == 2
    assert not store.threads()
    assert all(row["thread_id"] is None for row in store.events())
    store.close()


def test_pr_mapping_routes_future_events_and_repo_namespaces_are_isolated(tmp_path):
    first = RepositoryRef(123, "example/one")
    second = RepositoryRef(456, "example/two")
    fake = FakeGitHub(
        {first.full_name: first, second.full_name: second},
        {
            (123, "issues"): PollResponse((issue_item(),)),
            (456, "issues"): PollResponse((issue_item(),)),
            (123, "review_comments"): PollResponse(
                (
                    {
                        "id": 11,
                        "updated_at": "2026-01-01T00:00:00Z",
                        "body": "@agent review",
                        "pull_request_url": "https://api.github.com/repos/example/one/pulls/12",
                    },
                )
            ),
        },
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([first.full_name, second.full_name])
    store.register_pr_mapping(123, 12, "github:123:issue:7")
    routed = next(
        event
        for event in store.events()
        if event["repo_id"] == 123 and event["subject_kind"] == "pull_request"
    )
    assert routed["thread_id"] == "github:123:issue:7"
    assert {row["thread_id"] for row in store.threads()} == {
        "github:123:issue:7",
        "github:456:issue:7",
    }
    store.close()


def test_cursor_does_not_advance_when_batch_fails(tmp_path):
    class FailingStore(SQLiteGitHubStore):
        def record_batch(self, *args, **kwargs):
            raise RuntimeError("simulated persistence failure")

    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {(123, "issues"): PollResponse((issue_item(),))},
    )
    store = FailingStore(tmp_path / "state.db")
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        poller(fake, store).poll([repo.full_name])
    assert store.cursor(123, "issues") is None
    store.close()


def test_304_is_successful_noop(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    response = PollResponse(etag="etag-1", not_modified=True)
    fake = FakeGitHub({repo.full_name: repo}, {(123, "issues"): response})
    store = SQLiteGitHubStore(tmp_path / "state.db")
    result = poller(fake, store).poll([repo.full_name])
    assert result.persisted == 0
    assert store.cursor(123, "issues")["last_successful_poll_at"]
    store.close()
