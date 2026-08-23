import pytest

from sweforge.github_models import (
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import SQLiteGitHubStore, WorkflowMode, WorkflowPhase
from sweforge.server import (
    ServerConfig,
    ServerInstanceLock,
    SWEForgeServer,
    _safe_dispatch_error,
)
from sweforge.server_cli import _mappings, build_parser
from sweforge.workflow import WorkflowEngine


def _event(repo: RepositoryRef, source_id: str, body: str) -> SourceEvent:
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id=source_id,
        source_updated_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body=body,
        html_url=None,
    )


def _event_for(
    repo: RepositoryRef,
    source_id: str,
    number: int,
    body: str,
    kind=SourceKind.ISSUE,
) -> SourceEvent:
    event = _event(repo, source_id, body)
    return SourceEvent(
        **{
            **event.__dict__,
            "source_id": source_id,
            "subject_number": number,
            "source_kind": kind,
        }
    )


def _store(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    store.upsert_repository(1, repo.full_name, "2026-01-01T00:00:00Z")
    store.record_batch(
        1,
        "issues",
        [_event(repo, "1", "@agent do the work")],
        since="2025-12-31T23:00:00Z",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )
    return store


def test_runnable_query_is_stable_and_persisted_backoff_excludes_thread(tmp_path):
    store = _store(tmp_path)
    assert store.runnable_thread_ids(now="2026-01-01T00:00:02Z") == ["github:1:issue:7"]
    store.record_dispatcher_failure(
        "github:1:issue:7", now="2026-01-01T00:00:02Z", error="boom"
    )
    assert store.runnable_thread_ids(now="2026-01-01T00:00:03Z") == []
    store.close()

    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    assert reopened.dispatcher_failure("github:1:issue:7")["failure_count"] == 1
    assert reopened.runnable_thread_ids(now="2026-01-01T00:00:06Z") == []
    assert reopened.runnable_thread_ids(now="2026-01-01T00:00:07Z") == [
        "github:1:issue:7"
    ]
    reopened.clear_dispatcher_failure("github:1:issue:7")
    reopened.close()


def test_singleton_lock_rejects_second_owner(tmp_path):
    first = ServerInstanceLock(tmp_path / "state.db")
    second = ServerInstanceLock(tmp_path / "state.db")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.close()
        second.close()


def test_server_config_and_cli_mapping_are_deterministic(tmp_path):
    mappings = _mappings(["z/repo=" + str(tmp_path), "a/repo=" + str(tmp_path)])
    config = ServerConfig(
        repositories=tuple(sorted(mappings)), repo_paths=mappings, model="m"
    )
    server = SWEForgeServer(config, client_factory=lambda _: (None, None))
    assert config.planning == config.execution == config.review == "m"
    assert list(config.repositories) == ["a/repo", "z/repo"]
    assert (
        build_parser().parse_args(["--repo-path", "a/repo=/tmp", "--model", "m"]).once
        is False
    )
    assert server.stop_event.is_set() is False


def test_server_rejects_invalid_bounds(tmp_path):
    with pytest.raises(ValueError):
        SWEForgeServer(
            ServerConfig(
                repositories=("a/repo",),
                repo_paths={"a/repo": tmp_path},
                model="m",
                workers=0,
            ),
            client_factory=lambda _: (None, None),
        )


def test_issue_body_and_comment_actionability_match_poller_policy(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    store.upsert_repository(1, repo.full_name, "2026-01-01T00:00:00Z")
    store.record_batch(
        1,
        "issues",
        [_event_for(repo, "issue", 7, "Please investigate, @agent")],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )
    store.record_batch(
        1,
        "issue_comments",
        [
            _event_for(
                repo,
                "comment",
                8,
                "Please investigate, @agent",
                SourceKind.ISSUE_COMMENT,
            )
        ],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )
    store.record_batch(
        1,
        "issue_comments",
        [
            _event_for(
                repo, "invocation", 9, "@agent investigate", SourceKind.ISSUE_COMMENT
            )
        ],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )
    runnable = store.runnable_thread_ids(now="2026-01-01T00:00:02Z")
    assert "github:1:issue:7" in runnable
    assert "github:1:issue:8" not in runnable
    assert "github:1:issue:9" in runnable
    store.close()


def test_once_refills_all_durable_backlog_not_only_initial_worker_batch(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteGitHubStore(path)
    repo = RepositoryRef(1, "owner/repo")
    store.upsert_repository(1, repo.full_name, "2026-01-01T00:00:00Z")
    for number in range(1, 8):
        store.record_batch(
            1,
            "issues",
            [_event_for(repo, str(number), number, "@agent do work")],
            since="now",
            etag=None,
            polled_at="2026-01-01T00:00:01Z",
        )
    store.close()
    seen: list[str] = []

    def worker(thread_id: str) -> None:
        seen.append(thread_id)
        worker_store = SQLiteGitHubStore(path)
        worker_store.record_dispatcher_failure(
            thread_id, now="9999-01-01T00:00:01Z", error="test completion"
        )
        worker_store.close()

    config = ServerConfig(
        repositories=("owner/repo",),
        repo_paths={"owner/repo": tmp_path},
        db=path,
        model="test-model",
        workers=2,
        once=True,
    )

    class Poller:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, repositories):
            return None

    class Client:
        def close(self):
            pass

    SWEForgeServer(
        config,
        client_factory=lambda _: (Client(), None),
        poller_factory=Poller,
        worker_runner=worker,
    ).run()
    assert len(seen) == 7
    assert len(set(seen)) == 7


def test_auto_plan_wait_is_durable_runnable_without_new_comment(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    root = _event(repo, "root", "@agent do work")
    store.upsert_repository(1, repo.full_name, "2026-01-01T00:00:00Z")
    store.record_batch(
        1,
        "issues",
        [root],
        since="now",
        etag=None,
        polled_at="2026-01-01T00:00:01Z",
    )

    class Client:
        def repository(self, full_name):
            return repo

        def issue(self, repo_ref, number):
            return {"labels": [{"name": "AUTO"}]}

        def comments(self, repo_ref, number):
            return []

        def create_comment(self, repo_ref, number, body):
            return {"id": 1, "body": body}

    engine = WorkflowEngine(store=store, client=Client(), clock=lambda: "now")
    plan = engine.start_cycle(
        event_key=root.event_key, plan_text="plan", mode=WorkflowMode.AUTO
    )
    engine.publish_plan(plan.plan_id)
    state = store.workflow_state("github:1:issue:7")
    assert state.phase is WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
    assert store.is_thread_runnable(state.thread_id, now="now")
    store.close()


def test_dispatcher_error_redacts_known_secret(monkeypatch):
    monkeypatch.setenv("SWEFORGE_GITHUB_TOKEN", "ghs_super_secret")
    error = _safe_dispatch_error(
        RuntimeError("provider rejected ghs_super_secret in response")
    )
    assert "ghs_super_secret" not in error
    assert "[REDACTED]" in error


def test_once_surfaces_poll_failure(tmp_path):
    class Client:
        def close(self):
            pass

    class Poller:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, repositories):
            raise RuntimeError("poll unavailable")

    config = ServerConfig(
        repositories=("owner/repo",),
        repo_paths={"owner/repo": tmp_path},
        db=tmp_path / "state.db",
        model="test-model",
        once=True,
    )
    with pytest.raises(RuntimeError, match="poll unavailable"):
        SWEForgeServer(
            config,
            client_factory=lambda _: (Client(), None),
            poller_factory=Poller,
        ).run()
