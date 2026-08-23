"""Cross-process locking: scope, ordering and the absence of over-serialization.

There is exactly one legal acquisition order:

    IssueThread lock  ->  repository Git lock  ->  release Git lock

The repository Git lock guards only short local Git administrative work that is
genuinely shared between IssueThreads. Everything else -- model calls, tests,
review, GitHub API traffic, per-worktree Git -- stays concurrent.
"""

import concurrent.futures
import subprocess
import threading
from pathlib import Path

import pytest
from test_publication_identity import (
    THREAD_ID,
    Harness,
    TokenProvider,
    approval,
    source_event,
)

from sweforge import execution_locks
from sweforge.execution import (
    LockOrderError,
    ThreadLockUnavailable,
    held_repo_git_locks,
    repo_git_lock,
    thread_lock,
)
from sweforge.github_publisher import GitHubPublisher
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workspace import ThreadWorkspace, WorkspaceError


def worker_publisher(harness):
    """A second worker: its own SQLite connection, the same durable state.

    SQLite connections are thread-bound, so every dispatcher worker owns one.
    """
    store = SQLiteGitHubStore(harness.db)
    publisher = GitHubPublisher(
        store=store,
        client=harness.client,
        token_provider=TokenProvider(),
        lock_root=harness.tmp_path / "locks",
        remote_url_factory=lambda _: f"file://{harness.remote}",
    )
    return store, publisher


def git_repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-qm", "b"],
        cwd=root,
        check=True,
    )
    return root


class SectionWatcher:
    """Records the maximum number of overlapping critical sections."""

    def __init__(self):
        self.guard = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self):
        with self.guard:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self):
        with self.guard:
            self.active -= 1


def watch_worktree_add(monkeypatch, watcher, *, barrier=None):
    """Instrument the shared-Git mutation so overlap is directly observable."""
    import sweforge.workspace as workspace_module

    original = workspace_module._git

    def instrumented(repo, *args):
        if args[:2] == ("worktree", "add"):
            watcher.enter()
            try:
                if barrier is not None:
                    # Every worker is inside the section at once if unguarded.
                    try:
                        barrier.wait(timeout=0.5)
                    except threading.BrokenBarrierError:
                        pass
                return original(repo, *args)
            finally:
                watcher.leave()
        return original(repo, *args)

    monkeypatch.setattr(workspace_module, "_git", instrumented)


def test_shared_worktree_creation_never_overlaps_in_one_repo(tmp_path, monkeypatch):
    """Different IssueThreads, same repo: the shared Git section is serialized."""
    repo = git_repo(tmp_path, "src")
    watcher = SectionWatcher()
    # The barrier would let all workers into the section simultaneously if the
    # lock were missing; with the lock it always times out harmlessly.
    watch_worktree_add(monkeypatch, watcher, barrier=threading.Barrier(4))
    locks = tmp_path / "locks"

    def create(issue_number):
        return ThreadWorkspace.create(
            repository=repo,
            workspace_root=tmp_path / "ws",
            repo_id=1,
            issue_number=issue_number,
            lock_root=locks,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        spaces = list(pool.map(create, [11, 12, 13, 14]))

    assert watcher.peak == 1
    # Independent worktrees, independent branches, all usable.
    assert len({str(space.path) for space in spaces}) == 4
    assert {space.branch_name for space in spaces} == {
        f"sweforge/issue-{number}" for number in (11, 12, 13, 14)
    }
    for space in spaces:
        assert space.path.is_dir()
        assert space.is_clean()


def test_racing_the_same_thread_workspace_leaves_it_usable(tmp_path):
    """One winner; the loser fails closed without destroying the worktree."""
    repo = git_repo(tmp_path, "src")
    root = tmp_path / "ws"
    locks = tmp_path / "locks"
    barrier = threading.Barrier(2)

    def create(_):
        barrier.wait()
        try:
            return ThreadWorkspace.create(
                repository=repo,
                workspace_root=root,
                repo_id=1,
                issue_number=7,
                lock_root=locks,
            )
        except WorkspaceError as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))

    created = [item for item in results if isinstance(item, ThreadWorkspace)]
    assert len(created) == 1
    path = root / "1" / "issue-7"
    assert path.is_dir()
    # The thread stays usable: reopening with persisted metadata succeeds.
    reopened = ThreadWorkspace.create(
        repository=repo,
        workspace_root=root,
        repo_id=1,
        issue_number=7,
        existing_path=str(path),
        expected_branch="sweforge/issue-7",
        lock_root=locks,
    )
    assert reopened.created is False


def test_a_crashed_worktree_add_does_not_wedge_the_thread(tmp_path):
    """A leftover branch with no worktree is reattached, not recreated."""
    repo = git_repo(tmp_path, "src")
    locks = tmp_path / "locks"
    # Simulate a crash after `git worktree add -b` created the branch.
    subprocess.run(
        ["git", "branch", "sweforge/issue-7"], cwd=repo, check=True, capture_output=True
    )
    space = ThreadWorkspace.create(
        repository=repo,
        workspace_root=tmp_path / "ws",
        repo_id=1,
        issue_number=7,
        lock_root=locks,
    )
    assert space.branch_name == "sweforge/issue-7"
    assert space.path.is_dir()
    assert space.is_clean()


def test_a_branch_live_in_another_worktree_is_refused(tmp_path):
    repo = git_repo(tmp_path, "src")
    locks = tmp_path / "locks"
    ThreadWorkspace.create(
        repository=repo,
        workspace_root=tmp_path / "ws",
        repo_id=1,
        issue_number=7,
        lock_root=locks,
    )
    # A different workspace root must not steal a branch already checked out.
    with pytest.raises(WorkspaceError, match="already checked out"):
        ThreadWorkspace.create(
            repository=repo,
            workspace_root=tmp_path / "other",
            repo_id=1,
            issue_number=7,
            lock_root=locks,
        )


def test_repositories_do_not_block_each_other(tmp_path):
    """Repo A holding its Git lock must not delay repo B at all."""
    repo_b = git_repo(tmp_path, "beta")
    locks = tmp_path / "locks"
    done = threading.Event()

    with repo_git_lock(locks, 1):

        def build_b():
            ThreadWorkspace.create(
                repository=repo_b,
                workspace_root=tmp_path / "ws",
                repo_id=2,
                issue_number=7,
                lock_root=locks,
            )
            done.set()

        worker = threading.Thread(target=build_b)
        worker.start()
        # If repo 2 were serialized behind repo 1 this would time out.
        assert done.wait(timeout=10), "repo B blocked on repo A's Git lock"
        worker.join(timeout=10)


def test_repo_git_lock_is_released_before_the_planner_model_runs(tmp_path):
    """The lock covers Git administration only, never model work."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    observed: dict[str, int] = {}

    def planner(**kwargs):
        observed["depth"] = held_repo_git_locks()
        return "Requirements:\n1. change something"

    harness.engine.planner = planner
    harness.engine.plan_event(
        event_key=origin.event_key,
        model="planning-sonnet",
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
        lock_root=harness.tmp_path / "locks",
    )
    assert observed["depth"] == 0
    harness.store.close()


def test_repo_git_lock_is_released_before_execution_and_review(tmp_path):
    """Long-running per-thread work never holds shared repository Git state."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    depths: list[int] = []

    def runner(**kwargs):
        depths.append(held_repo_git_locks())
        Path(kwargs["worktree"], "fix.txt").write_text("fix\n")
        return "done"

    reviewer_depths: list[int] = []

    def reviewer(**_):
        from sweforge.reviewer import ExecutionReviewResult

        reviewer_depths.append(held_repo_git_locks())
        return ExecutionReviewResult(verdict="ACCEPT", summary="ok")

    harness.engine.reviewer = reviewer
    execute_kwargs = harness.execute_kwargs("fix.txt")
    execute_kwargs["runner"] = runner
    for _ in range(2):
        harness.engine.advance(
            thread_id=THREAD_ID,
            model="planning-sonnet",
            review_model="review-sonnet",
            repo_paths={harness.repo.full_name: harness.source},
            workspace_root=harness.tmp_path / "workspaces",
            execute_kwargs=execute_kwargs,
        )
    assert depths == [0]
    assert reviewer_depths and set(reviewer_depths) == {0}
    harness.store.close()


def test_thread_lock_still_admits_only_one_worker(tmp_path):
    locks = tmp_path / "locks"
    with thread_lock(locks, THREAD_ID):
        with pytest.raises(ThreadLockUnavailable):
            with thread_lock(locks, THREAD_ID):
                pass
        # A different IssueThread is unaffected.
        with thread_lock(locks, "github:1:issue:8"):
            pass


def test_lock_inversion_is_structurally_refused(tmp_path):
    """No path may take an IssueThread lock while holding a repo Git lock."""
    locks = tmp_path / "locks"
    with repo_git_lock(locks, 1):
        with pytest.raises(LockOrderError, match="before the repository Git lock"):
            with thread_lock(locks, THREAD_ID):
                pass
    # The legal order works and leaves no residue.
    with thread_lock(locks, THREAD_ID):
        with repo_git_lock(locks, 1):
            assert held_repo_git_locks() == 1
    assert held_repo_git_locks() == 0


def test_publication_never_takes_the_repository_git_lock(tmp_path, monkeypatch):
    """Publication is thread-scoped; GitHub API phases are not serialized."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("fix.txt")

    acquisitions: list[int] = []
    original = execution_locks.repo_git_lock

    def counting(root, repo_id):
        acquisitions.append(repo_id)
        return original(root, repo_id)

    monkeypatch.setattr("sweforge.github_publisher.thread_lock", thread_lock)
    monkeypatch.setattr(execution_locks, "repo_git_lock", counting)
    monkeypatch.setattr("sweforge.workspace.repo_git_lock", counting)

    result = harness.publisher.publish_one()
    assert result.status == "COMPLETED", result.error
    assert acquisitions == []
    harness.store.close()


def test_publication_proceeds_while_shared_git_work_holds_the_repo_lock(tmp_path):
    """Publisher and shared-Git work cannot deadlock: no shared lock, no cycle."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("fix.txt")

    finished: dict[str, str] = {}

    with repo_git_lock(harness.tmp_path / "locks", harness.repo.repo_id):

        def publish():
            store, publisher = worker_publisher(harness)
            try:
                finished["status"] = publisher.publish_one().status
            finally:
                store.close()

        worker = threading.Thread(target=publish)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive(), "publication deadlocked behind the repo Git lock"
    assert finished["status"] == "COMPLETED"
    harness.store.close()


def test_concurrent_publishers_of_one_publication_are_idempotent(tmp_path):
    """Two workers, one publication: one publishes, no duplicate PR or comment."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("fix.txt")

    barrier = threading.Barrier(2)

    def publish(_):
        store, publisher = worker_publisher(harness)
        try:
            barrier.wait()
            return publisher.publish_one().status
        finally:
            store.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(publish, range(2)))

    # Whoever loses the IssueThread lock is told BUSY, or finds no work left.
    assert "COMPLETED" in statuses
    assert set(statuses) <= {"COMPLETED", "BUSY", "NO_WORK"}
    assert len(harness.client.pulls) == 1
    publication_comments = [
        item
        for item in harness.client.created
        if "sweforge:publication:" in item["body"]
    ]
    assert len(publication_comments) == 1
    harness.store.close()
