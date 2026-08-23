import pytest

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.server import ServerConfig, ServerInstanceLock, SWEForgeServer
from sweforge.server_cli import _mappings, build_parser


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
