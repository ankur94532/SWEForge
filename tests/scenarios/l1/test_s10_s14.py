"""L1 scenarios S10-S14: concurrency, scheduling and process singleton.

S14 uses two real ServerInstanceLock holders because a singleton is only
meaningful across processes. The rest run in-process against isolated Worlds,
which is what proves thread and repository scoping without a dispatcher.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, layers_for, run, scenario
from harness.world import World

from sweforge.execution import ThreadLockUnavailable, thread_lock
from sweforge.github_store import WorkflowPhase
from sweforge.server import ServerInstanceLock

PLAN = "1. edit README\n2. run the tests"


def _world(root_dir: Path, **kw) -> World:
    return World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
        **kw,
    )


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "README.md").write_text("done\n")
    return "edited README"


def _to_execution(world: World, thread_id: str, issue_number: int = 7) -> None:
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    # The approval must name the same issue as its thread; a default issue
    # number routes every approval to the first thread instead.
    approval = world.event(
        f"approve-{thread_id}",
        "@agent approve",
        world.later(),
        issue_number=issue_number,
    )
    world.ingest(approval)
    world.engine.approve(event_key=approval.event_key)
    world.drive(
        thread_id,
        until=WorkflowPhase.AWAITING_PUBLICATION,
        max_ticks=10,
        execute_kwargs={"runner": _writing_runner, "checkpointer": object()},
    )


@scenario(
    "S10",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-THREAD-ISOLATION"],
    description="Two issues in one repository stay on separate branches.",
)
def s10_same_repo_concurrency(root_dir) -> Observation:
    world = _world(root_dir)
    with world.activate():
        world.ingest(
            world.event("1", "@agent fix A", "2026-01-01T00:00:00Z", issue_number=7)
        )
        world.ingest(
            world.event("2", "@agent fix B", "2026-01-01T00:00:01Z", issue_number=8)
        )
        threads = sorted(world.thread_ids)
        assert len(threads) == 2, f"expected two threads, got {threads}"
        for issue_number, thread_id in zip((7, 8), threads, strict=True):
            _to_execution(world, thread_id, issue_number)
        workspaces = [world.store.thread_workspace(t) for t in threads]
        branches = {item.branch_name for item in workspaces}
        assert len(branches) == 2, f"threads shared a branch: {branches}"
        paths = {item.workspace_path for item in workspaces}
        assert len(paths) == 2, f"threads shared a worktree: {paths}"
    return world.observation()


@scenario(
    "S11",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL"],
    description="Two repositories keep separate state and never serialize.",
)
def s11_cross_repo_concurrency(root_dir) -> Observation:
    first = _world(root_dir / "repo-a", repo_id=1, full_name="example/alpha")
    second = _world(root_dir / "repo-b", repo_id=2, full_name="example/beta")
    with first.activate():
        first.ingest(first.event("1", "@agent fix alpha", "2026-01-01T00:00:00Z"))
        _to_execution(first, next(iter(first.thread_ids)))
    with second.activate():
        second.ingest(second.event("1", "@agent fix beta", "2026-01-01T00:00:00Z"))
        _to_execution(second, next(iter(second.thread_ids)))

    # Each repository's durable state names only itself.
    for world, other_repo_id in ((first, 2), (second, 1)):
        rows = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_threads WHERE repo_id=?", (other_repo_id,)
        ).fetchone()[0]
        assert rows == 0, f"repo {other_repo_id} leaked into the other store"
    # A repository git lock is per repo_id, so the two never contend.
    with thread_lock(root_dir / "locks", f"repo-git:{first.repo.repo_id}"):
        with thread_lock(root_dir / "locks", f"repo-git:{second.repo.repo_id}"):
            pass
    return first.observation()


@scenario(
    "S12",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-THREAD-ISOLATION"],
    description="A backlog drains with every queued thread running exactly once.",
)
def s12_backlog_drain(root_dir) -> Observation:
    world = _world(root_dir)
    with world.activate():
        for number in range(7, 12):
            world.ingest(
                world.event(
                    str(number),
                    f"@agent fix issue {number}",
                    f"2026-01-01T00:00:0{number - 7}Z",
                    issue_number=number,
                )
            )
        threads = sorted(world.thread_ids)
        assert len(threads) == 5, f"expected five queued threads, got {len(threads)}"
        for thread_id in threads:
            _to_execution(world, thread_id, int(thread_id.rsplit(":", 1)[1]))
        # Exactly one INITIAL attempt per queued thread: none lost, none doubled.
        counts = {
            thread_id: world.store.connection.execute(
                "SELECT COUNT(*) FROM execution_attempts "
                "WHERE thread_id=? AND kind='INITIAL'",
                (thread_id,),
            ).fetchone()[0]
            for thread_id in threads
        }
        assert set(counts.values()) == {1}, (
            f"attempts were lost or duplicated: {counts}"
        )
    return world.observation()


@scenario(
    "S13",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL"],
    description="A held thread lock makes the second worker back off, not duplicate.",
)
def s13_thread_lock_contention(root_dir) -> Observation:
    world = _world(root_dir)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        _to_execution(world, thread_id)

        # A second worker holding the thread lock must be refused, not queued.
        lock_root = world.root / "locks"
        with thread_lock(lock_root, thread_id):
            with pytest.raises(ThreadLockUnavailable):
                with thread_lock(lock_root, thread_id):
                    raise AssertionError("two workers held one thread lock")

        attempts = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        assert attempts == 1, f"contention produced {attempts} attempts"
    return world.observation()


@scenario(
    "S14",
    layer=Layer.L1_PROCESS,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="A second server on one state database is refused cleanly.",
)
def s14_singleton_server(root_dir) -> Observation:
    world = _world(root_dir)
    db = world.root / "state.db"
    first = ServerInstanceLock(db)
    first.acquire()
    try:
        second = ServerInstanceLock(db)
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
        # Refusal must be clean: the loser leaves the holder's lock intact.
        third = ServerInstanceLock(db)
        with pytest.raises(RuntimeError, match="already running"):
            third.acquire()
    finally:
        first.close()
    # Once released, a new server may take it.
    fourth = ServerInstanceLock(db)
    fourth.acquire()
    fourth.close()
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S10", "S11", "S12", "S13", "S14"])
def test_scenario_passes(scenario_id, tmp_path):
    # S14 is the singleton-server scenario and is registered for L1_PROCESS,
    # and several of these also have a LIVE_GITHUB body. Resolve each id at its
    # deterministic layer, which is the one this module registers.
    (layer,) = [
        item for item in layers_for(scenario_id) if item is not Layer.LIVE_GITHUB
    ]
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=layer)
    assert result.ok, "\n" + result.report()
