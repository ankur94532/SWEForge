"""Safe, temporary Git worktree management.

Git worktrees provide workspace isolation only. They do not provide security
isolation: commands run by an agent still run on the host.
"""

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
        # _git strips the porcelain line's optional leading XY whitespace, so
        # remove the two status columns from the normalized line.
        return [line[2:] for line in _git(self.path, "status", "--short").splitlines()]

    def diff(self) -> str:
        return _git(self.path, "diff", "--no-ext-diff")

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
