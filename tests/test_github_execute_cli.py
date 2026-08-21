from sweforge.execution import ExecutionResult
from sweforge.github_execute_cli import main
from sweforge.github_store import ClaimedEvent, ExecutionStatus


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
