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
        self.issue_calls = []
        self.stream_calls = []

    def repository(self, full_name):
        return self.repositories[full_name]

    def issues(self, repo, since, etag):
        self.stream_calls.append(("issues", since, etag))
        return self.responses.get((repo.repo_id, "issues"), PollResponse())

    def issue_comments(self, repo, since, etag):
        self.stream_calls.append(("issue_comments", since, etag))
        return self.responses.get((repo.repo_id, "issue_comments"), PollResponse())

    def review_comments(self, repo, since, etag):
        self.stream_calls.append(("review_comments", since, etag))
        return self.responses.get((repo.repo_id, "review_comments"), PollResponse())

    def issue(self, repo, number):
        self.issue_calls.append((repo.repo_id, number))
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

    assert first.events_persisted == 1
    assert first.threads_created == 1
    assert second.events_persisted == 0
    assert second.threads_created == 0
    assert (
        store.connection.execute(
            "SELECT observed_at FROM repositories WHERE repo_id = 123"
        ).fetchone()[0]
        == "2026-01-01T00:00:00Z"
    )
    assert store.events()[0]["discovered_at"] == "2026-01-01T00:00:00Z"
    assert store.cursor(123, "issues")["last_successful_poll_at"] == (
        "2026-01-01T00:00:00Z"
    )
    assert [row["thread_id"] for row in store.events()] == ["github:123:issue:7"]
    store.close()


def test_issue_activity_does_not_re_root_unchanged_body(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {(123, "issues"): PollResponse((issue_item(updated="2026-01-01T00:00:00Z"),))},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])

    fake.responses[(123, "issues")] = PollResponse(
        (issue_item(updated="2026-01-01T00:00:01Z"),)
    )
    fake.responses[(123, "issue_comments")] = PollResponse(
        (comment_item(comment_id=90, updated="2026-01-01T00:00:02Z"),)
    )
    result = poller(fake, store).poll([repo.full_name])

    issue_events = [row for row in store.events() if row["source_kind"] == "issue"]
    comment_events = [
        row for row in store.events() if row["source_kind"] == "issue_comment"
    ]
    assert result.events_persisted == 1
    assert len(issue_events) == 1
    assert len(comment_events) == 1
    store.close()


@pytest.mark.parametrize(
    ("bodies", "expected_events"),
    [
        (("@agent do X", "@agent do X and Y", "@agent do X"), 3),
        (("@agent do X", "do X", "@agent do X"), 2),
        (("@agent do X", "do X", "do X"), 1),
    ],
)
def test_issue_content_transitions_use_last_observation(
    tmp_path, bodies, expected_events
):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub({repo.full_name: repo})
    store = SQLiteGitHubStore(tmp_path / "state.db")
    for index, body in enumerate(bodies):
        fake.responses[(123, "issues")] = PollResponse(
            (
                issue_item(
                    updated=f"2026-01-01T00:00:0{index}Z",
                    body=body,
                ),
            )
        )
        poller(fake, store).poll([repo.full_name])

    issue_events = [row for row in store.events() if row["source_kind"] == "issue"]
    assert len(issue_events) == expected_events
    store.close()


def test_issue_metadata_updates_without_new_task_event(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    first = issue_item(updated="2026-01-01T00:00:00Z")
    first["title"] = "Original"
    second = issue_item(updated="2026-01-01T00:00:01Z")
    second["title"] = "Renamed"
    fake = FakeGitHub({repo.full_name: repo}, {(123, "issues"): PollResponse((first,))})
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])
    fake.responses[(123, "issues")] = PollResponse((second,))
    assert poller(fake, store).poll([repo.full_name]).events_persisted == 0
    metadata = store.issue_metadata(repo_id=123, issue_number=7)
    assert metadata and metadata.title == "Renamed"
    assert len(store.events()) == 1
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


def test_issue_comment_classification_is_cached_per_poll(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {
            (123, "issue_comments"): PollResponse(
                (comment_item(comment_id=9), comment_item(comment_id=10))
            )
        },
        {(123, 7): {}},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])
    assert fake.issue_calls == [(123, 7)]
    store.close()


def test_comments_require_leading_agent_invocation(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {
            (123, "issue_comments"): PollResponse(
                (
                    comment_item(comment_id=1, body="@agent accepted"),
                    comment_item(comment_id=2, body="  @AGENT accepted"),
                    comment_item(comment_id=3, body="hello @agent rejected"),
                    comment_item(comment_id=4, body="FYI @agent rejected"),
                )
            )
        },
        {(123, 7): {}},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])
    assert {row["source_id"] for row in store.events()} == {"1", "2"}
    store.close()


def test_inline_review_context_is_persisted(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {
            (123, "review_comments"): PollResponse(
                (
                    {
                        "id": 44,
                        "updated_at": "2026-01-01T00:00:00Z",
                        "body": "@agent fix this race",
                        "pull_request_url": "https://api.github.com/repos/example/repo/pulls/12",
                        "path": "src/Foo.java",
                        "line": 15,
                        "start_line": 10,
                        "side": "RIGHT",
                        "start_side": "RIGHT",
                        "diff_hunk": "@@ -10,6 +10,11 @@",
                        "commit_id": "newsha",
                        "original_commit_id": "oldsha",
                        "in_reply_to_id": 40,
                        "pull_request_review_id": 9,
                    },
                )
            )
        },
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])
    event = store.events()[0]
    assert event["origin_surface"] == "PR_INLINE_REVIEW"
    assert event["path"] == "src/Foo.java"
    assert (event["line"], event["start_line"]) == (15, 10)
    assert (event["side"], event["start_side"]) == ("RIGHT", "RIGHT")
    assert event["diff_hunk"] == "@@ -10,6 +10,11 @@"
    assert event["commit_id"] == "newsha"
    assert event["original_commit_id"] == "oldsha"
    assert event["review_thread_root_id"] == "40"
    store.close()


def test_result_counts_new_threads_routing_and_duplicates_precisely(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {
            (123, "issues"): PollResponse((issue_item(),)),
            (123, "issue_comments"): PollResponse(
                (comment_item(comment_id=9), comment_item(comment_id=10))
            ),
        },
        {(123, 7): {}},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    first = poller(fake, store).poll([repo.full_name])
    second = poller(fake, store).poll([repo.full_name])
    assert (first.events_discovered, first.events_persisted) == (3, 3)
    assert (first.threads_created, first.events_routed) == (1, 3)
    assert first.pr_events_unrouted == 0
    assert (second.events_persisted, second.threads_created) == (0, 0)
    assert second.events_routed == 0
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

    assert result.events_persisted == 2
    assert result.pr_events_unrouted == 2
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
    fake.responses[(123, "review_comments")] = PollResponse(
        (
            {
                "id": 12,
                "updated_at": "2026-01-01T00:01:00Z",
                "body": "@agent another review",
                "pull_request_url": "https://api.github.com/repos/example/one/pulls/12",
            },
        )
    )
    routed_result = poller(fake, store).poll([first.full_name])
    assert routed_result.events_routed == 1
    assert routed_result.pr_events_unrouted == 0
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


def test_etag_is_not_reused_after_cursor_query_changes(tmp_path):
    repo = RepositoryRef(123, "example/repo")
    fake = FakeGitHub(
        {repo.full_name: repo},
        {(123, "issues"): PollResponse((issue_item(),), etag="etag-1")},
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    poller(fake, store).poll([repo.full_name])
    poller(fake, store).poll([repo.full_name])
    issue_calls = [call for call in fake.stream_calls if call[0] == "issues"]
    assert issue_calls[0][2] is None
    assert issue_calls[1][2] is None
    store.close()
