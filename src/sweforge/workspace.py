"""Safe, temporary Git worktree management.

Git worktrees provide workspace isolation only. They do not provide security
isolation: commands run by an agent still run on the host.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a repository cannot be prepared as a workspace."""


@dataclass
class ThreadWorkspace:
    """A persistent, repository- and issue-scoped Git worktree."""

    repository: Path
    path: Path
    base_commit: str
    branch_name: str
    created: bool

    @classmethod
    def create(
        cls,
        *,
        repository: str | Path,
        workspace_root: str | Path,
        repo_id: int,
        issue_number: int,
        existing_path: str | None = None,
        expected_branch: str | None = None,
        expected_base: str | None = None,
    ) -> "ThreadWorkspace":
        repo = _repository_root(repository)
        root = Path(workspace_root).expanduser().resolve()
        path = root / str(repo_id) / f"issue-{issue_number}"
        branch = expected_branch or f"sweforge/issue-{issue_number}"
        if existing_path is not None and Path(existing_path).resolve() != path:
            raise WorkspaceError(
                "persisted workspace path does not match expected path"
            )
        if expected_branch is not None and branch != f"sweforge/issue-{issue_number}":
            raise WorkspaceError("persisted workspace branch does not match issue")

        if path.exists():
            if existing_path is None:
                raise WorkspaceError("workspace path exists without persisted metadata")
            if not path.is_dir():
                raise WorkspaceError(f"Workspace path is not a directory: {path}")
            actual_root = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
            actual_branch = _git(path, "branch", "--show-current")
            actual_base = _git(path, "rev-parse", "HEAD")
            if actual_root != path or actual_branch != branch:
                raise WorkspaceError(
                    "existing workspace does not match persisted metadata"
                )
            return cls(repo, path, expected_base or actual_base, branch, False)

        if existing_path is not None:
            raise WorkspaceError("persisted workspace directory is missing")
        path.parent.mkdir(parents=True, exist_ok=True)
        base = _git(repo, "rev-parse", "HEAD")
        try:
            _git(repo, "worktree", "add", "-b", branch, str(path), base)
        except WorkspaceError:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return cls(repo, path, base, branch, True)

    def changed_files(self) -> list[str]:
        return Workspace(self.repository, self.path, self.base_commit).changed_files()

    def diff(self) -> str:
        return Workspace(self.repository, self.path, self.base_commit).diff()

    def head_sha(self) -> str:
        return _git(self.path, "rev-parse", "HEAD")

    def is_clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.path,
            check=True,
            capture_output=True,
        )
        return not result.stdout


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise WorkspaceError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.strip()


def _repository_root(repository: str | Path) -> Path:
    repo = Path(repository).expanduser().resolve()
    if not repo.is_dir():
        raise WorkspaceError(f"Repository path is not a directory: {repo}")
    try:
        return Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    except WorkspaceError as exc:
        raise WorkspaceError(f"Not a Git repository: {repo}") from exc


@dataclass
class Workspace:
    """A temporary detached worktree based on a local Git repository."""

    repository: Path
    path: Path
    base_commit: str
    _removed: bool = False

    @classmethod
    def create(cls, repository: str | Path) -> "Workspace":
        repo = Path(repository).expanduser().resolve()
        if not repo.is_dir():
            raise WorkspaceError(f"Repository path is not a directory: {repo}")
        try:
            root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
            base = _git(repo, "rev-parse", "HEAD")
        except WorkspaceError as exc:
            raise WorkspaceError(f"Not a Git repository: {repo}") from exc

        temp_path = Path(tempfile.mkdtemp(prefix="sweforge-worktree-"))
        shutil.rmtree(temp_path)
        try:
            _git(root, "worktree", "add", "--detach", str(temp_path), base)
        except WorkspaceError:
            shutil.rmtree(temp_path, ignore_errors=True)
            raise
        return cls(repository=root, path=temp_path, base_commit=base)

    def changed_files(self) -> list[str]:
        tracked = [path for _, path in self._tracked_entries()]
        untracked = [path for status, path in self._status_entries() if status == "??"]
        return sorted(set(tracked + untracked))

    def diff(self) -> str:
        tracked = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", self.base_commit, "--"],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        untracked = []
        for status, path in self._status_entries():
            if status != "??":
                continue
            result = subprocess.run(
                ["git", "diff", "--no-index", "--binary", "--", os.devnull, path],
                cwd=self.path,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode not in (0, 1):
                raise WorkspaceError(
                    result.stderr.strip() or "could not diff untracked file"
                )
            untracked.append(result.stdout)
        return tracked + "".join(untracked)

    def is_clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.path,
            check=True,
            capture_output=True,
        )
        return not result.stdout

    def _tracked_entries(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                self.base_commit,
                "--",
            ],
            cwd=self.path,
            check=True,
            capture_output=True,
        )
        fields = result.stdout.split(b"\0")
        entries = []
        index = 0
        while index < len(fields) - 1:
            status = os.fsdecode(fields[index])
            index += 1
            if not status:
                continue
            path = os.fsdecode(fields[index])
            index += 1
            if status[0] in "RC":
                if index >= len(fields):
                    raise WorkspaceError("malformed Git rename diff")
                path = os.fsdecode(fields[index])
                index += 1
            entries.append((status[0], path))
        return entries

    def _status_entries(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.path,
            check=True,
            capture_output=True,
        )
        fields = result.stdout.split(b"\0")
        entries = []
        index = 0
        while index < len(fields) - 1:
            field = fields[index]
            index += 1
            if len(field) < 3:
                continue
            status = os.fsdecode(field[:2])
            path = os.fsdecode(field[3:])
            if status[0] in "RC" or status[1] in "RC":
                if index >= len(fields):
                    raise WorkspaceError("malformed Git rename status")
                index += 1
            entries.append((status, path))
        return entries

    def cleanup(self) -> None:
        """Remove this temporary worktree without modifying the primary checkout."""
        if self._removed:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )
        self._removed = True
