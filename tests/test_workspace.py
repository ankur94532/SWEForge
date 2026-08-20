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


def test_workspace_reports_staged_unstaged_and_untracked_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("initial\n")
    git(repo, "add", "tracked.txt")
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
        (workspace.path / "tracked.txt").write_text("staged\n")
        (workspace.path / "staged.txt").write_text("new staged\n")
        git(workspace.path, "add", "tracked.txt", "staged.txt")
        (workspace.path / "unstaged.txt").write_text("new unstaged\n")
        (workspace.path / "unstaged-tracked.txt").write_text("created\n")
        git(workspace.path, "add", "unstaged-tracked.txt")
        (workspace.path / "unstaged-tracked.txt").write_text("changed after staging\n")

        assert set(workspace.changed_files()) == {
            "staged.txt",
            "tracked.txt",
            "unstaged.txt",
            "unstaged-tracked.txt",
        }
        diff = workspace.diff()
        assert "staged" in diff
        assert "new staged" in diff
        assert "new unstaged" in diff
        assert "changed after staging" in diff
        assert (repo / "tracked.txt").read_text() == "initial\n"
    finally:
        workspace.cleanup()


def test_workspace_rejects_non_git_directory(tmp_path):
    with pytest.raises(WorkspaceError, match="Not a Git repository"):
        Workspace.create(tmp_path)
