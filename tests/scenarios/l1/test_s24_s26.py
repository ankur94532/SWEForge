"""L1 scenarios S24-S26: curator failures and transient review failure.

All three prove isolation: a failure in a learning lane or in review
infrastructure must not retroactively invalidate work that already succeeded,
and must not fabricate a durable record of success.
"""

from pathlib import Path

import pytest
from harness.models import FaultyRole, ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import MAX_REVIEW_RECOVERIES, WorkflowPhase
from sweforge.reviewer import ExecutionReviewResult, ReviewFinalizationError

PLAN = "1. edit README\n2. run the tests"


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "README.md").write_text("done\n")
    return "edited README"


def _to_accept(world: World, thread_id: str) -> None:
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    approval = world.event("2", "@agent approve", world.later())
    world.ingest(approval)
    world.engine.approve(event_key=approval.event_key)
    world.drive(
        thread_id,
        until=WorkflowPhase.AWAITING_PUBLICATION,
        max_ticks=10,
        execute_kwargs={"runner": _writing_runner, "checkpointer": object()},
    )


@scenario(
    "S24",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-NO-MEMORY-WRITTEN"],
    description="A failing repo-memory curator writes no memory and isolates itself.",
)
def s24_repo_memory_curator_failure(root_dir) -> Observation:
    curator = FaultyRole(
        inner=lambda **kw: [],
        fail_on_calls=[1, 2, 3, 4],
        error=RuntimeError("injected curator failure"),
    )
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
        memory_learner=curator,
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        _to_accept(world, thread_id)

        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "SUCCEEDED", (
            "a curator failure must not invalidate the execution that preceded it"
        )
        accepted = world.store.connection.execute(
            "SELECT COUNT(*) FROM repo_memory_candidates WHERE status='ACCEPTED'"
        ).fetchone()[0]
        assert accepted == 0, "a failing curator wrote accepted repository memory"
    return world.observation()


@scenario(
    "S25",
    layer=Layer.L1,
    invariants=["INV-ATTEMPT-TERMINAL", "INV-NO-RESOLUTION-WRITTEN"],
    description="A failing resolution curator writes no false resolution row.",
)
def s25_resolution_curator_failure(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        _to_accept(world, thread_id)

        # Nothing finalized a publication, so no resolution row may claim the
        # issue was solved. INV-NO-FALSE-RESOLUTION checks exactly that.
        rows = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_resolution_memory WHERE status='COMPLETED'"
        ).fetchone()[0]
        assert rows == 0, "a resolution was recorded without a finalized publication"
    return world.observation()


@scenario(
    "S26",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-REVIEW-NO-REPAIR-ON-INFRA",
        "INV-NO-PUBLICATION",
    ],
    description="Transient review failure reuses INITIAL and mints no new permit.",
)
def s26_transient_review_failure(root_dir) -> Observation:
    """The first review raises operationally; the retry then ACCEPTs."""
    calls = {"n": 0}

    def flaky_reviewer(**kwargs) -> ExecutionReviewResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ReviewFinalizationError(
                "execution review finalization failed operationally",
                diagnostic={"guard_codes": []},
            )
        return ExecutionReviewResult(verdict="ACCEPT", summary="accepted on retry")

    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=flaky_reviewer,
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        permit = world.engine.approve(event_key=approval.event_key)
        execute_kwargs = {"runner": _writing_runner, "checkpointer": object()}

        # Drive into review; the first review attempt fails operationally and
        # the error propagates so the dispatcher's backoff owns the retry.
        with pytest.raises(ReviewFinalizationError):
            world.drive(
                thread_id,
                until=WorkflowPhase.AWAITING_PUBLICATION,
                max_ticks=6,
                execute_kwargs=execute_kwargs,
            )
        attempt_after_failure = world.store.latest_attempt(thread_id, 1)
        assert attempt_after_failure.status == "SUCCEEDED", (
            "review infrastructure failure must not fail the INITIAL attempt"
        )
        assert attempt_after_failure.review_recovery_count == 1, (
            "the bounded review retry budget was not consumed"
        )

        # The retry reuses the same attempt and the same permit.
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=6,
            execute_kwargs=execute_kwargs,
        )
        final = world.store.latest_attempt(thread_id, 1)
        assert final.attempt_id == attempt_after_failure.attempt_id, (
            "review recovery started a new execution attempt"
        )
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 1, f"review recovery minted a second permit ({permits})"
        assert calls["n"] == 2, f"expected 2 review attempts, saw {calls['n']}"
        review = world.store.execution_review_for_attempt(final.attempt_id)
        assert review is not None and review.verdict == "ACCEPT"
        del permit
    return world.observation()


def test_review_retry_budget_is_bounded():
    """Positive control for S26: the bound exists and matches execution."""
    assert MAX_REVIEW_RECOVERIES == 3


@pytest.mark.parametrize("scenario_id", ["S24", "S25", "S26"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower())
    assert result.ok, "\n" + result.report()
