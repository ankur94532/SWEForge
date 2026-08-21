from sweforge.execution import ExecutionResult
from sweforge.github_execute_cli import main
from sweforge.github_execution_cli import main as manage_main
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import ClaimedEvent, ExecutionStatus, SQLiteGitHubStore


class FakeStore:
    def __init__(self, path):
        self.path = path

    def close(self):
        pass


class FakeCheckpoints:
    saver = object()

    def __init__(self, path):
        self.path = path

    def close(self):
        pass


def args(tmp_path):
    return [
        "--db",
        str(tmp_path / "state.db"),
        "--checkpoints",
        str(tmp_path / "checkpoints.sqlite"),
        "--memory-db",
        str(tmp_path / "memory.sqlite"),
        "--repo-path",
        f"owner/repo={tmp_path}",
        "--model",
        "provider:model",
    ]


def test_clean_no_work_has_distinct_exit_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sweforge.github_execute_cli.SQLiteGitHubStore", FakeStore)
    monkeypatch.setattr(
        "sweforge.github_execute_cli.SQLiteCheckpointer", FakeCheckpoints
    )
    monkeypatch.setattr(
        "sweforge.github_execute_cli.execute_one",
        lambda **kwargs: ExecutionResult(status="NO_WORK"),
    )
    assert main(args(tmp_path)) == 3
    assert "execution status: NO_WORK" in capsys.readouterr().out


def test_failed_execution_is_nonzero_and_safe(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sweforge.github_execute_cli.SQLiteGitHubStore", FakeStore)
    monkeypatch.setattr(
        "sweforge.github_execute_cli.SQLiteCheckpointer", FakeCheckpoints
    )
    event = ClaimedEvent(
        event_key="1:issue:1:now",
        thread_id="github:1:issue:1",
        repo_id=1,
        repo_full_name="owner/repo",
        issue_number=1,
        body="@agent fail",
        workspace_path=None,
    )
    monkeypatch.setattr(
        "sweforge.github_execute_cli.execute_one",
        lambda **kwargs: ExecutionResult(
            status=ExecutionStatus.FAILED.value,
            event=event,
            error="RuntimeError: safe failure",
        ),
    )
    assert main(args(tmp_path)) == 1
    assert "safe failure" in capsys.readouterr().err


def _event_store(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    event = SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id="1",
        source_updated_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body="@agent run",
        html_url=None,
    )
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issues",
        [event],
        since="now",
        etag=None,
        polled_at="now",
    )
    store.claim_next_event(now="now")
    store.mark_execution_failed(
        event.event_key,
        completed_at="now",
        error_message="failure",
        workspace_path=None,
    )
    store.close()
    return event.event_key


def test_execution_management_cli_retry_skip_and_status(tmp_path, capsys):
    event_key = _event_store(tmp_path)
    common = [
        "--db",
        str(tmp_path / "state.db"),
        "--lock-root",
        str(tmp_path / "locks"),
    ]
    assert manage_main([*common, "status"]) == 0
    assert event_key in capsys.readouterr().out
    assert manage_main([*common, "retry", event_key]) == 0
    assert "RETRY_PENDING" in capsys.readouterr().out
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.claim_next_event(now="now")
    store.mark_execution_failed(
        event_key, completed_at="now", error_message="failure", workspace_path=None
    )
    store.close()
    assert manage_main([*common, "skip", event_key]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_execution_management_cli_rejects_bad_event_key(tmp_path, capsys):
    assert (
        manage_main(
            [
                "--db",
                str(tmp_path / "state.db"),
                "retry",
                "missing-event",
            ]
        )
        == 2
    )
    assert "unknown event key" in capsys.readouterr().err
