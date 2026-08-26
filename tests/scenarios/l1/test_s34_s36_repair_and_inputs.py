"""S34 and S36: the repair loop's bound, and two logical inputs in one event.

S34 drives repair rounds until the bound is reached. A repair loop that never
terminates is a system that never stops spending, and the terminal state must
be REVIEW_BLOCKED with nothing published.

S36 covers the README's S -> D1, D2 case: one SourceEvent carrying two logical
inputs becomes two cycles, and an ACCEPT of the first must not authorize the
second. That is an authorization boundary hiding inside an ingestion detail.
"""

import json

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World
from test_repair_retry_bound import _seed_repair_attempt

from sweforge.github_store import (
    ExecutionReviewRecord,
    WorkflowPhase,
)

MAX_REPAIRS = 5


def _needs_fixes_review(store, *, review_id, thread_id, state, attempt_id, iteration):
    store.save_execution_review(
        ExecutionReviewRecord(
            review_id=review_id,
            thread_id=thread_id,
            cycle_id=state.cycle_id,
            plan_id=state.current_plan_id,
            plan_version=1,
            root_event_key=state.root_event_key,
            attempt_id=attempt_id,
            review_iteration=iteration,
            verdict="NEEDS_FIXES",
            summary="still wrong",
            findings_json=json.dumps([]),
            repair_instructions_json=json.dumps([]),
            created_at="now",
            completed_at="now",
        )
    )


@scenario(
    "S34",
    layer=Layer.L1,
    invariants=["INV-PERMIT-BOUND", "INV-NO-PUBLICATION"],
    description="A repair loop stops at its bound and blocks without publishing.",
)
def s34_repair_loop_to_exhaustion(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        store = world.store
        thread_id, repair = _seed_repair_attempt(store)
        world.thread_ids.add(thread_id)

        rounds = 1
        exhausted = False
        last = repair
        # Each round: the repair succeeds, review says NEEDS_FIXES, ask again.
        for index in range(2, MAX_REPAIRS + 3):
            store.finish_repair_attempt_success(
                last.authorization_id,
                attempt_id=last.attempt_id,
                now="now",
                response_text="attempted",
                end_head_sha=f"c{index}",
                end_dirty=False,
                workspace_path=str(root_dir / "ws"),
                start_head_sha="b",
            )
            state = store.workflow_state(thread_id)
            _needs_fixes_review(
                store,
                review_id=f"r{index}",
                thread_id=thread_id,
                state=state,
                attempt_id=last.attempt_id,
                iteration=index,
            )
            try:
                permit = store.create_repair_permit(thread_id=thread_id, now="now")
            except ValueError as exc:
                # The bound is reported by refusing, not by returning None.
                assert "repair rounds" in str(exc), f"unexpected refusal: {exc}"
                exhausted = True
                break
            rounds += 1
            last = store.begin_or_resume_repair_attempt(permit.permit_id, now="now")

        assert exhausted, "the repair loop never reached its bound"
        state = store.workflow_state(thread_id)
        assert state.phase == WorkflowPhase.REVIEW_BLOCKED, (
            f"the repair loop ended in {state.phase}, not REVIEW_BLOCKED"
        )
        assert rounds <= MAX_REPAIRS, (
            f"the loop ran {rounds} rounds against a bound of {MAX_REPAIRS}"
        )
        live = store.connection.execute(
            "SELECT COUNT(*) FROM review_repair_permits "
            "WHERE thread_id=? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (thread_id,),
        ).fetchone()[0]
        assert live == 0, f"exhaustion left {live} live repair permits"
    return world.observation()


@scenario(
    "S36",
    layer=Layer.L1,
    invariants=["INV-DEFERRED-PRESERVED", "INV-PLAN-CANONICAL", "INV-PROVENANCE"],
    description="Two logical inputs in one event become two independently "
    "authorized cycles.",
)
def s36_one_event_two_logical_inputs(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=["1. first task\n2. done", "1. second\n2. done"]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT", "ACCEPT"]),
    )
    with world.activate():
        # One comment carrying two distinct requests.
        world.ingest(
            world.event(
                "1",
                "@agent fix the boundary\n\n@agent also update the notes",
                "2026-01-01T00:00:00Z",
            )
        )
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)

        deferred = list(world.store.deferred_followups(thread_id))
        plan = world.store.current_plan(thread_id)
        assert plan is not None

        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        permit = world.engine.approve(event_key=approval.event_key)

        # The permit authorizes exactly the cycle whose plan was approved.
        assert permit.cycle_id == plan.cycle_id, "the permit left its cycle"
        assert permit.plan_id == plan.plan_id
        # Any deferred second input keeps its own identity for a later cycle.
        for row in deferred:
            assert row["deferred_id"] != permit.source_event_key, (
                "a deferred input was consumed as the approval itself"
            )
        ids = [row["deferred_id"] for row in deferred]
        assert len(ids) == len(set(ids)), f"deferred inputs share an id: {ids}"
    return world.observation()


def test_the_repair_bound_is_positive():
    """Positive control: a zero bound would make S34 trivial."""
    assert MAX_REPAIRS > 0


@pytest.mark.parametrize("scenario_id", ["S34", "S36"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
