"""L1 scenarios: S1 canonical lifecycle, S19 no-change execution.

Both drive a real WorkflowEngine over a real git worktree with scripted roles.
S1 is scoped to the lifecycle through ACCEPT: commit/push/PR are the
publisher's job, not advance()'s, so claiming publication invariants here would
assert something this scenario never exercises.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import WorkflowPhase

PLAN = "1. edit README\n2. run the tests"


def _writing_runner(text: str):
    """A runner double that actually mutates the authoritative worktree."""

    def runner(**kwargs):
        worktree = kwargs.get("worktree")
        assert worktree, f"runner received no worktree; got {sorted(kwargs)}"
        (Path(worktree) / "README.md").write_text(text)
        return "edited README"

    return runner


def _noop_runner(**kwargs):
    """Reports success while changing nothing — the S19 shape."""
    assert kwargs.get("worktree"), "runner received no worktree"
    return "nothing needed changing"


def _approved(world: World, thread_id: str):
    approval = world.event("2", "@agent approve", world.later())
    world.ingest(approval)
    return world.engine.approve(event_key=approval.event_key)


@scenario(
    "S1",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-ROOT",
        "INV-PLAN-CANONICAL",
        "INV-PLAN-VERSIONED",
        "INV-PERMIT-BOUND",
        "INV-PERMIT-SOURCE",
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-NO-HOT-RETRY",
        "INV-RETRY-BOUNDED",
        "INV-PROVENANCE",
        "INV-THREAD-ISOLATION",
        "INV-REPO-ISOLATION",
    ],
    description="Issue to accepted review: one plan, one permit, one INITIAL.",
)
def s1_happy_path(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        _approved(world, thread_id)
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _writing_runner("fixed\n"),
                "checkpointer": object(),
            },
        )
        review = world.store.execution_review_for_attempt(
            world.store.latest_attempt(thread_id, 1).attempt_id
        )
        assert review is not None and review.verdict == "ACCEPT"
    return world.observation()


@scenario(
    "S19",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-NO-EMPTY-COMMIT",
        "INV-NO-PUBLICATION",
        "INV-PROVENANCE",
    ],
    description="A no-change execution succeeds and commits nothing.",
)
def s19_no_change_execution(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        world.ingest(
            world.event("1", "@agent check the config", "2026-01-01T00:00:00Z")
        )
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        _approved(world, thread_id)
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _noop_runner,
                "checkpointer": object(),
            },
        )
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "SUCCEEDED", (
            f"a no-change execution must succeed, not fail: {attempt.status}"
        )
        assert not attempt.end_dirty, "a no-change execution left the worktree dirty"
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S1", "S19"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower())
    assert result.ok, "\n" + result.report()
