import subprocess
from pathlib import Path

import pytest

from sweforge.workspace import Workspace, WorkspaceError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_workspace_isolated_and_reports_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("initial\n")
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=SWEForge",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )

    workspace = Workspace.create(repo)
    try:
        assert workspace.repository == repo.resolve()
        assert workspace.base_commit == git(repo, "rev-parse", "HEAD")
        assert workspace.path != repo
        (workspace.path / "README.md").write_text("changed\n")
        assert workspace.changed_files() == ["README.md"]
        assert "-initial" in workspace.diff()
        assert (repo / "README.md").read_text() == "initial\n"
    finally:
        workspace.cleanup()
    assert not workspace.path.exists()


def test_workspace_rejects_non_git_directory(tmp_path):
    with pytest.raises(WorkspaceError, match="Not a Git repository"):
        Workspace.create(tmp_path)
