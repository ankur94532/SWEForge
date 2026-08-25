"""S19 LIVE_GITHUB: a no-change execution commits nothing and publishes nothing.

The distinction that matters is between "nothing needed doing" and "the run
failed". Against real GitHub this also proves no empty commit and no pull
request reach the repository.
"""

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, open_live_thread
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from sweforge.github_store import WorkflowPhase

PLAN = "1. confirm the boundary is already correct\n2. change nothing"


def _no_change_runner(**kwargs):
    return "inspected the boundary; no change required"


@scenario(
    "S19",
    layer=Layer.LIVE_GITHUB,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-NO-EMPTY-COMMIT",
        "INV-NO-PUBLICATION",
        "INV-PROVENANCE",
    ],
    description="A live no-change execution succeeds and commits nothing.",
)
def s19_live_no_change(root_dir) -> Observation:
    live = open_live_thread(
        "S19",
        root_dir,
        body="@agent confirm the bulk discount boundary is correct",
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    world = live.world
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
                "runner": _no_change_runner,
                "checkpointer": object(),
            },
        )
        attempt = world.store.latest_attempt(live.thread_id, 1)
        assert attempt.status == "SUCCEEDED", "a no-change run must not fail"
        assert attempt.start_head_sha == attempt.end_head_sha, (
            "a no-change run moved HEAD"
        )
    return world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S19", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s19_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S19", tmp_path / "s19-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
