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
