"""Cross-process locking primitives and their one legal ordering.

Kept in its own module because `workspace` needs the repository Git lock and
`execution` imports `workspace`.
"""

import fcntl
import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ThreadLockUnavailable(RuntimeError):
    """Raised when another process currently owns a thread lock."""


class LockOrderError(RuntimeError):
    """Raised when locks would be taken in an order that can deadlock."""


# ----------------------------------------------------------------------
# Lock ordering
#
# There is exactly one legal order:
#
#     IssueThread lock  ->  repository Git lock  ->  (release Git lock)
#
# The repository Git lock guards only short local Git administrative work and
# must never be held across a model call, a test run or a network wait.  Taking
# an IssueThread lock while holding a repository Git lock would invert the
# order, so `thread_lock` refuses it outright rather than deadlocking.
# ----------------------------------------------------------------------
_LOCK_ORDER = threading.local()


def _repo_git_lock_depth() -> int:
    return getattr(_LOCK_ORDER, "repo_git_depth", 0)


def held_repo_git_locks() -> int:
    """Depth of repository Git locks held by the current thread."""
    return _repo_git_lock_depth()


@contextmanager
def repo_git_lock(root: str | Path, repo_id: int) -> Iterator[None]:
    """Serialize mutations of ONE repository's shared Git administrative state.

    Scope is deliberately narrow.  Per-worktree work (status, diff, commit on a
    thread's own branch) is not shared state and stays concurrent; only
    operations that mutate the source repository's worktree registry or its
    branch namespace need this.  The key is the authoritative `repo_id`, never
    anything a model supplied, so one repository never blocks another.

    Blocking, like the repository memory lock: the guarded section contains
    only local Git commands, so waiting briefly is preferable to failing work
    that is about to succeed.  A crashed holder releases the OS lock.
    """
    lock_root = Path(root).expanduser().resolve()
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"repo-git-{int(repo_id)}.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _LOCK_ORDER.repo_git_depth = _repo_git_lock_depth() + 1
        try:
            yield
        finally:
            _LOCK_ORDER.repo_git_depth = _repo_git_lock_depth() - 1
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def thread_lock(root: str | Path, thread_id: str) -> Iterator[None]:
    """Acquire a non-blocking cross-process lock for exactly one thread."""
    if _repo_git_lock_depth():
        raise LockOrderError(
            "IssueThread lock must be acquired before the repository Git lock"
        )
    lock_root = Path(root).expanduser().resolve()
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{hashlib.sha256(thread_id.encode()).hexdigest()}.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ThreadLockUnavailable(
                "IssueThread is already being executed"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
