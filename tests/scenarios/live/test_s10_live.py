"""S10 LIVE_GITHUB: two real issues in one repository stay isolated.

Both threads share a store and a repository, so a shared branch or worktree
would be visible immediately. Live, the branch names are the ones actually
pushed to a real repository rather than strings a fake invented.
"""

from pathlib import Path

import pytest
from harness.live import (
    LiveCredentialsUnavailable,
    live_repository,
    open_additional_issue,
    open_live_thread,
)
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from sweforge.github_store import WorkflowPhase

PLAN = "1. touch the notes file\n2. run the tests"


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "NOTES.md").write_text("concurrent\n")
    return "wrote NOTES.md"


@scenario(
    "S10",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-THREAD-ISOLATION"],
    description="Two live issues in one repository stay on separate branches.",
)
def s10_live_same_repo_concurrency(root_dir) -> Observation:
    live = open_live_thread(
        "S10",
        root_dir,
        body="@agent fix A",
        planner=ScriptedPlanner(plans=[PLAN, PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT", "ACCEPT"]),
    )
    world = live.world
    second = open_additional_issue(live, "@agent fix B")
    assert second != live.thread_id, "the second issue reused the first thread"

    with world.activate():
        for thread_id in (live.thread_id, second):
            world.drive(
                thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=10
            )

        # Approve both, each on its own issue.
        live.approve()
        number = world.store.connection.execute(
            "SELECT issue_number FROM issue_threads WHERE thread_id=?", (second,)
        ).fetchone()["issue_number"]
        from harness.live import comment, poll_until

        comment(live.client, live.full_name, number, "@agent approve")

        def second_approval():
            row = world.store.connection.execute(
                "SELECT event_key FROM source_events WHERE thread_id=? "
                "AND body LIKE '%@agent approve%' ORDER BY rowid DESC LIMIT 1",
                (second,),
            ).fetchone()
            return row["event_key"] if row else None

        world.engine.approve(
            event_key=poll_until(live.poller, live.full_name, second_approval)
        )

        for thread_id in (live.thread_id, second):
            world.drive(
                thread_id,
                until=WorkflowPhase.AWAITING_PUBLICATION,
                max_ticks=12,
                execute_kwargs={
                    "lock_root": world.root / "locks",
                    "runner": _writing_runner,
                    "checkpointer": object(),
                },
            )

        workspaces = [world.store.thread_workspace(t) for t in (live.thread_id, second)]
        branches = {item.branch_name for item in workspaces}
        assert len(branches) == 2, f"threads shared a branch: {branches}"
        paths = {item.workspace_path for item in workspaces}
        assert len(paths) == 2, f"threads shared a worktree: {paths}"
    return world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S10", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s10_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S10", tmp_path / "s10-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
