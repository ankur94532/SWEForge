"""S27: review infrastructure exhausts its budget without inventing success.

S26 proves that a *transient* review failure recovers on the same attempt and
the same permit. It deliberately stops after one failure, so it never reaches
the bound. This scenario drives the other side: a review lane that never
recovers must stop at exactly MAX_REVIEW_RECOVERIES, block, and publish
nothing -- rather than retrying forever or recording a success it never had.

It supplies E4's S26_BACKOFF evidence, which must come from a run that
actually reached the bound.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import MAX_REVIEW_RECOVERIES, WorkflowPhase
from sweforge.reviewer import ReviewFinalizationError

PLAN = "1. edit README\n2. run the tests"


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "README.md").write_text("done\n")
    return "edited README"


@scenario(
    "S27",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-REVIEW-NO-REPAIR-ON-INFRA",
        "INV-NO-PUBLICATION",
    ],
    description="Review infrastructure stops at its bound and publishes nothing.",
)
def s27_review_infrastructure_exhaustion(root_dir) -> Observation:
    calls = {"n": 0}

    def always_failing_reviewer(**kwargs):
        calls["n"] += 1
        raise ReviewFinalizationError(
            "execution review finalization failed operationally",
            diagnostic={"guard_codes": []},
        )

    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=always_failing_reviewer,
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        world.engine.approve(event_key=approval.event_key)
        execute_kwargs = {"runner": _writing_runner, "checkpointer": object()}

        # Each failing review raises so the dispatcher's backoff owns the
        # retry, exactly as S26 establishes. Drive until the budget is spent.
        for _ in range(MAX_REVIEW_RECOVERIES):
            with pytest.raises(ReviewFinalizationError):
                world.drive(
                    thread_id,
                    until=WorkflowPhase.AWAITING_PUBLICATION,
                    max_ticks=6,
                    execute_kwargs=execute_kwargs,
                )

        # The exhausting tick does not raise: it blocks instead.
        world.drive(
            thread_id,
            until=WorkflowPhase.REVIEW_BLOCKED,
            max_ticks=6,
            execute_kwargs=execute_kwargs,
        )
        attempt = world.store.latest_attempt(thread_id, 1)
        state = world.store.workflow_state(thread_id)

        assert state.phase == WorkflowPhase.REVIEW_BLOCKED, (
            f"exhaustion left the thread in {state.phase}, not REVIEW_BLOCKED"
        )
        assert attempt.review_recovery_count == MAX_REVIEW_RECOVERIES, (
            f"stopped at {attempt.review_recovery_count} recoveries, "
            f"bound is {MAX_REVIEW_RECOVERIES}"
        )
        assert attempt.status == "SUCCEEDED", (
            "review infrastructure failure must not fail the execution attempt"
        )
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 1, f"exhaustion minted {permits} permits"

        observation = world.observation()
        observation.record_bound(
            "S26_BACKOFF",
            actual=attempt.review_recovery_count,
            expected=MAX_REVIEW_RECOVERIES,
        )
    return observation


def test_review_recovery_bound_is_positive():
    """Positive control: a zero bound would make exhaustion trivial."""
    assert MAX_REVIEW_RECOVERIES > 0


def test_scenario_passes(tmp_path):
    result = run("S27", tmp_path / "s27")
    assert result.ok, "\n" + result.report()


def test_the_scenario_records_the_bound_it_reached(tmp_path):
    """E4 evidence must come from the run, not from the constant."""
    result = run("S27", tmp_path / "s27-bound")
    evidence = result.bounded_paths["S26_BACKOFF"]
    assert evidence == {
        "observed": True,
        "actual": MAX_REVIEW_RECOVERIES,
        "expected": MAX_REVIEW_RECOVERIES,
    }
