"""S28: repair execution recovery reaches its bound and fails closed.

The existing store tests prove the counter arithmetic in isolation.  This
scenario supplies campaign evidence from the public workflow: a hard crash
leaves a REVIEW_REPAIR attempt RUNNING, the next tick recovers it, and the
following tick resumes that same attempt.  After three such recoveries, the
fourth recovery must block permanently without incrementing past the bound.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import (
    MAX_REPAIR_EXECUTION_RECOVERIES,
    AttemptStatus,
    WorkflowPhase,
)

PLAN = "1. edit README\n2. run the tests"


class RepairWorkerDied(BaseException):
    """Escape the execution path exactly as a dead worker process would."""


def _runner(**kwargs):
    if kwargs.get("repair_mode"):
        raise RepairWorkerDied("repair worker died mid-execution")
    (Path(kwargs["worktree"]) / "README.md").write_text("initial execution\n")
    return "initial execution completed"


def _execute_kwargs() -> dict:
    return {"runner": _runner, "checkpointer": object()}


@scenario(
    "S28",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-RETRY-BOUNDED", "INV-NO-PUBLICATION"],
    description="Crashed repair execution stops at its recovery bound.",
)
def s28_repair_execution_recovery_bound(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["NEEDS_FIXES"]),
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        world.engine.approve(event_key=approval.event_key)
        world.drive(
            thread_id,
            until=WorkflowPhase.REPAIR_READY,
            max_ticks=6,
            execute_kwargs=_execute_kwargs(),
        )

        initial_attempt = world.store.latest_attempt(thread_id, 1)
        assert initial_attempt.status == AttemptStatus.SUCCEEDED.value

        # Start the repair and prove the injected crash actually ran.  If this
        # does not raise, no orphan exists and none of the later evidence is
        # meaningful.
        with pytest.raises(RepairWorkerDied):
            world.tick(thread_id, execute_kwargs=_execute_kwargs())
        orphan = world.store.latest_attempt(thread_id, 1)
        assert orphan.status == AttemptStatus.RUNNING.value, (
            f"crash left repair attempt {orphan.status}, not RUNNING"
        )
        assert orphan.attempt_id != initial_attempt.attempt_id
        repair_attempt_id = orphan.attempt_id
        repair_permit_id = orphan.authorization_id

        observed_counts = []
        for expected_count in range(1, MAX_REPAIR_EXECUTION_RECOVERIES + 1):
            recovered = world.tick(thread_id, execute_kwargs=_execute_kwargs())
            assert recovered.phase is WorkflowPhase.REPAIR_READY, (
                f"recovery {expected_count} reached {recovered.phase}"
            )
            attempt = world.store.latest_attempt(thread_id, 1)
            assert attempt.attempt_id == repair_attempt_id, (
                "recovery created a new repair attempt"
            )
            assert attempt.status == AttemptStatus.FAILED.value
            observed_counts.append(attempt.repair_recovery_count)

            with pytest.raises(RepairWorkerDied):
                world.tick(thread_id, execute_kwargs=_execute_kwargs())
            resumed = world.store.latest_attempt(thread_id, 1)
            assert resumed.attempt_id == repair_attempt_id, (
                "resumption created a new repair attempt"
            )
            assert resumed.status == AttemptStatus.RUNNING.value

        assert observed_counts == list(range(1, MAX_REPAIR_EXECUTION_RECOVERIES + 1)), (
            f"recovery counter history was {observed_counts}"
        )

        # The attempt is RUNNING at the configured count.  Its next recovery
        # is the exhausting transition: no runner call occurs and no further
        # repair may be authorized.
        exhausted = world.tick(thread_id, execute_kwargs=_execute_kwargs())
        final_state = world.store.workflow_state(thread_id)
        final_attempt = world.store.latest_attempt(thread_id, 1)
        assert exhausted.phase is WorkflowPhase.REVIEW_BLOCKED
        assert final_state.phase is WorkflowPhase.REVIEW_BLOCKED
        assert final_attempt.attempt_id == repair_attempt_id
        assert final_attempt.status == AttemptStatus.FAILED.value
        assert final_attempt.repair_recovery_count == MAX_REPAIR_EXECUTION_RECOVERIES, (
            f"exhaustion stopped at {final_attempt.repair_recovery_count}, "
            f"expected {MAX_REPAIR_EXECUTION_RECOVERIES}"
        )
        final_permit = world.store.repair_permit(repair_permit_id)
        assert final_permit is not None, "repair permit audit record disappeared"
        assert final_permit.invalidated_at is not None, (
            "exhaustion left the repair permit valid"
        )
        with pytest.raises(ValueError, match="repair permit is unavailable"):
            world.store.begin_or_resume_repair_attempt(
                repair_permit_id, now=world.later()
            )

        observation = world.observation()
        observation.record_bound(
            "EXECUTION_RETRY_X3",
            actual=final_attempt.repair_recovery_count,
            expected=MAX_REPAIR_EXECUTION_RECOVERIES,
        )
    return observation


def test_repair_execution_recovery_bound_is_positive():
    """Positive control: a zero bound would make exhaustion trivial."""
    assert MAX_REPAIR_EXECUTION_RECOVERIES > 0


def test_scenario_passes_without_vacuous_checks(tmp_path):
    result = run("S28", tmp_path / "s28", layer=Layer.L1)
    assert result.ok, "\n" + result.report()
    assert all(check.status == "PASS" for check in result.checks), result.report()


def test_scenario_records_the_persisted_bound_it_reached(tmp_path):
    result = run("S28", tmp_path / "s28-bound", layer=Layer.L1)
    assert result.bounded_paths["EXECUTION_RETRY_X3"] == {
        "observed": True,
        "actual": MAX_REPAIR_EXECUTION_RECOVERIES,
        "expected": MAX_REPAIR_EXECUTION_RECOVERIES,
    }
