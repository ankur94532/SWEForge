"""L1 scenarios S16 and S17: orphan recovery and recovery exhaustion.

A hard crash is simulated with a BaseException from the runner. `except
Exception` in the execution path cannot catch it, so the attempt is left
RUNNING and the phase EXECUTING -- exactly the durable state a process death
leaves behind. Hand-editing the tables would test the recovery code against a
state the system might never actually produce.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import (
    MAX_INITIAL_EXECUTION_RECOVERIES,
    AttemptStatus,
    WorkflowPhase,
)

PLAN = "1. edit README\n2. run the tests"


class WorkerDied(BaseException):
    """Not an Exception: nothing in the execution path may catch it."""


def _crashing_runner(**kwargs):
    raise WorkerDied("worker died mid-execution")


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "README.md").write_text("recovered\n")
    return "edited README"


def _approved(world: World) -> str:
    world.ingest(world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z"))
    thread_id = next(iter(world.thread_ids))
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    approval = world.event("2", "@agent approve", world.later())
    world.ingest(approval)
    world.engine.approve(event_key=approval.event_key)
    return thread_id


def _crash_once(world: World, thread_id: str) -> bool:
    """Tick with a crashing runner. Returns whether the runner actually ran.

    The exhausting tick never reaches the runner -- recovery fails the attempt
    closed first -- so this must not insist on a crash every time.
    """
    try:
        world.tick(
            thread_id,
            execute_kwargs={"runner": _crashing_runner, "checkpointer": object()},
        )
    except WorkerDied:
        return True
    return False


@scenario(
    "S16",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-PERMIT-BOUND",
        "INV-RETRY-BOUNDED",
        "INV-PROVENANCE",
        "INV-THREAD-ISOLATION",
    ],
    description="A crashed INITIAL is recovered under its own identity.",
)
def s16_crash_during_initial(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        thread_id = _approved(world)
        before_permit = world.store.permit_for_plan(
            world.store.current_plan(thread_id).plan_id
        )

        assert _crash_once(world, thread_id), "the runner never ran, so nothing crashed"
        orphan = world.store.latest_attempt(thread_id, 1)
        assert orphan.status == AttemptStatus.RUNNING.value, (
            f"the crash did not leave an orphan: {orphan.status}"
        )
        workspace_before = world.store.thread_workspace(thread_id)

        # Recovery runs on the next tick, under the IssueThread lock.
        world.tick(
            thread_id,
            execute_kwargs={"runner": _writing_runner, "checkpointer": object()},
        )
        recovered = world.store.latest_attempt(thread_id, 1)

        assert recovered.attempt_id == orphan.attempt_id, (
            "recovery created a new attempt instead of reclaiming the orphan"
        )
        assert recovered.status != AttemptStatus.RUNNING.value, (
            "the orphan was left RUNNING after recovery"
        )
        initials = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_attempts "
            "WHERE thread_id=? AND kind='INITIAL'",
            (thread_id,),
        ).fetchone()[0]
        assert initials == 1, f"recovery produced {initials} INITIAL attempts"

        # Same cycle, plan, permit and workspace: nothing was re-authorized.
        assert recovered.cycle_id == orphan.cycle_id
        assert recovered.plan_id == orphan.plan_id
        assert recovered.authorization_id == before_permit.permit_id, (
            "recovery rebound the attempt to a different permit"
        )
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 1, f"recovery minted a second permit ({permits})"
        workspace_after = world.store.thread_workspace(thread_id)
        assert workspace_after.workspace_path == workspace_before.workspace_path
        assert workspace_after.branch_name == workspace_before.branch_name
    return world.observation()


@scenario(
    "S17",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-RETRY-BOUNDED", "INV-NO-PUBLICATION"],
    description="Repeated crashes exhaust the bound and stop, not resurrect.",
)
def s17_recovery_exhaustion(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        thread_id = _approved(world)

        # Crash on every execution. Recovery must stop after the bound rather
        # than resurrecting the attempt forever, so the loop is capped well
        # above the bound and the tick count itself is asserted.
        cap = MAX_INITIAL_EXECUTION_RECOVERIES + 5
        ticks = 0
        for _ in range(cap):
            ticks += 1
            _crash_once(world, thread_id)
            if world.store.workflow_state(thread_id).phase is (
                WorkflowPhase.REVIEW_BLOCKED
            ):
                break
        else:
            raise AssertionError(
                f"still resurrecting after {cap} crashes; the bound never held"
            )

        final_state = world.store.workflow_state(thread_id)
        final_attempt = world.store.latest_attempt(thread_id, final_state.cycle_id)

        assert final_state.phase is WorkflowPhase.REVIEW_BLOCKED
        assert final_attempt.status == AttemptStatus.FAILED.value, (
            f"exhaustion left the attempt {final_attempt.status}"
        )
        assert final_attempt.retry_count == MAX_INITIAL_EXECUTION_RECOVERIES, (
            f"bound hit at {final_attempt.retry_count}, expected exactly "
            f"{MAX_INITIAL_EXECUTION_RECOVERIES}"
        )
        # One tick per recovery, plus the tick that exhausts.
        assert ticks == MAX_INITIAL_EXECUTION_RECOVERIES + 1, (
            f"took {ticks} ticks to exhaust a bound of "
            f"{MAX_INITIAL_EXECUTION_RECOVERIES}"
        )
        initials = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_attempts "
            "WHERE thread_id=? AND kind='INITIAL'",
            (thread_id,),
        ).fetchone()[0]
        assert initials == 1, f"exhaustion produced {initials} INITIAL attempts"
    return world.observation()


def test_recovery_bound_exists():
    """Positive control: an unbounded recovery would make S17 meaningless."""
    assert MAX_INITIAL_EXECUTION_RECOVERIES == 3


@pytest.mark.parametrize("scenario_id", ["S16", "S17"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower())
    assert result.ok, "\n" + result.report()
