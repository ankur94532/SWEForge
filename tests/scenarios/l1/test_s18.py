"""S18: an unsolicited follow-up during execution must be deferred, not injected.

The follow-up arrives while the INITIAL attempt is running. It must not reach
that execution's context, must keep its own durable deferred_id, and must be
available to the next planning cycle with its SourceEvent provenance intact.
"""

from pathlib import Path

from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import WorkflowPhase

PLAN = "1. edit README\n2. run the tests"


@scenario(
    "S18",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-NO-INJECTION",
        "INV-DEFERRED-PRESERVED",
        "INV-PROVENANCE",
        "INV-THREAD-ISOLATION",
    ],
    description="A follow-up sent mid-execution is deferred, never injected.",
)
def s18_unsolicited_followup(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    arrived: dict = {}

    def runner_that_receives_a_followup(**kwargs):
        """Ingest an @agent comment at the moment execution is in flight."""
        worktree = kwargs.get("worktree")
        assert worktree, "runner received no worktree"
        (Path(worktree) / "README.md").write_text("fixed\n")
        followup = world.event("3", "@agent also update the docs", world.later())
        world.ingest(followup)
        arrived["event_key"] = followup.event_key
        return "edited README"

    with world.activate():
        world.ingest(world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        world.engine.approve(event_key=approval.event_key)
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={
                "runner": runner_that_receives_a_followup,
                "checkpointer": object(),
            },
        )

        assert arrived, "the runner never ran, so nothing arrived mid-execution"
        deferred = world.store.deferred_followup(arrived["event_key"])
        assert deferred is not None, (
            "a follow-up sent during execution was not deferred; it may have "
            "been injected into the running cycle"
        )
        assert deferred.deferred_id, "the deferred follow-up has no durable identity"
        assert deferred.source_event_key == arrived["event_key"], (
            "the deferred follow-up lost its SourceEvent provenance"
        )
        # It belongs to the next cycle, not the one that was executing.
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.attempt_number == 1, "the follow-up started a second attempt"
    return world.observation()


def test_s18_passes(tmp_path):
    result = run("S18", tmp_path / "s18")
    assert result.ok, "\n" + result.report()


def test_s18_actually_emitted_a_deferral(tmp_path):
    """Guards against INV-NO-INJECTION passing because nothing was recorded."""
    result = run("S18", tmp_path / "s18-evidence")
    assert result.ok, "\n" + result.report()
    detail = next(
        c.detail for c in result.checks if c.invariant_id == "INV-DEFERRED-PRESERVED"
    )
    assert "1 deferred input" in detail, detail
