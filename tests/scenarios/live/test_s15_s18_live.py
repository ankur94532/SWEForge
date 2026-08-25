"""S15 and S18 LIVE_GITHUB: restart across approval, and a mid-run follow-up.

S15 proves the posted plan stays canonical across a restart: approval must
resume the same plan version and must not produce a second plan comment on the
real issue. S18 proves an unsolicited follow-up arriving mid-execution is
deferred rather than injected into the running agent.
"""

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, open_live_thread
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from sweforge.github_store import WorkflowPhase

PLAN = "1. adjust the pricing note\n2. run the tests"


def _writing_runner(**kwargs):
    from pathlib import Path

    (Path(kwargs["worktree"]) / "NOTES.md").write_text("live run\n")
    return "wrote NOTES.md"


@scenario(
    "S15",
    layer=Layer.LIVE_GITHUB,
    invariants=[
        "INV-PLAN-CANONICAL",
        "INV-PLAN-VERSIONED",
        "INV-PERMIT-BOUND",
        "INV-PROVENANCE",
    ],
    # INV-ONE-INITIAL is deliberately absent: S15 stops at approval and never
    # executes, so it would range over zero cycles and assert nothing.
    description="A restart while awaiting approval keeps one canonical plan.",
)
def s15_live_restart_awaiting_approval(root_dir) -> Observation:
    live = open_live_thread(
        "S15",
        root_dir,
        body="@agent update the pricing note",
        planner=ScriptedPlanner(plans=[PLAN, "1. a different plan\n2. no"]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    world = live.world
    with world.activate():
        world.drive(
            live.thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=10
        )
        posted = world.store.current_plan(live.thread_id)
        assert posted is not None

        # The restart: another tick while awaiting approval must not re-plan.
        world.drive(
            live.thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=3
        )
        again = world.store.current_plan(live.thread_id)
        assert again.plan_id == posted.plan_id, "a restart replaced the posted plan"
        assert again.version == posted.version, "a restart bumped the plan version"

        plan_comments = world.github_facts.comments_matching("sweforge:plan:")
        assert len(plan_comments) <= 1, (
            f"the restart posted {len(plan_comments)} plan comments to the issue"
        )

        permit = live.approve()
        assert permit.plan_id == posted.plan_id, "approval bound a different plan"
        assert permit.plan_version == posted.version
    return world.observation()


@scenario(
    "S18",
    layer=Layer.LIVE_GITHUB,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-NO-INJECTION",
        "INV-DEFERRED-PRESERVED",
        "INV-PROVENANCE",
    ],
    description="A live follow-up sent mid-run is deferred, never injected.",
)
def s18_live_unsolicited_followup(root_dir) -> Observation:
    live = open_live_thread(
        "S18",
        root_dir,
        body="@agent update the pricing note",
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    world = live.world

    def runner_that_receives_a_followup(**kwargs):
        """Post the follow-up at the moment execution is in flight.

        Posting it after approval but before execution starts is a different
        scenario: the workflow legitimately re-plans when scope arrives before
        any work has begun, which supersedes the plan rather than deferring.
        """
        from pathlib import Path

        (Path(kwargs["worktree"]) / "NOTES.md").write_text("live run\n")
        live.say("@agent also rename the module")
        return "wrote NOTES.md"

    with world.activate():
        world.drive(
            live.thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=10
        )
        live.approve()
        world.drive(
            live.thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=12,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": runner_that_receives_a_followup,
                "checkpointer": object(),
            },
        )
        deferred = list(world.store.deferred_followups(live.thread_id))
        assert deferred, "the follow-up was neither deferred nor preserved"
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S15", "S18"])
def test_registered_for_the_live_layer(scenario_id):
    from harness.scenario import SCENARIOS

    assert SCENARIOS[(scenario_id, Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
@pytest.mark.parametrize("scenario_id", ["S15", "S18"])
def test_live(scenario_id, tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run(
        scenario_id, tmp_path / f"{scenario_id.lower()}-live", layer=Layer.LIVE_GITHUB
    )
    assert result.ok, "\n" + result.report()
