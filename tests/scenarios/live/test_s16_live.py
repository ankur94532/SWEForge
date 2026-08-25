"""S16 LIVE_GITHUB: a crashed INITIAL is recovered under its own identity.

A BaseException from the runner faithfully simulates a hard worker death:
`except Exception` cannot catch it, so the attempt is left RUNNING exactly as
a SIGKILL would leave it. Live, the recovery must reclaim the same attempt,
permit and workspace against a real repository rather than a synthetic one.
"""

from pathlib import Path

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, open_live_thread
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from sweforge.github_store import AttemptStatus, WorkflowPhase

PLAN = "1. touch the notes file\n2. run the tests"


class WorkerDied(BaseException):
    """Not an Exception: nothing in the execution path may catch it."""


def _crashing_runner(**kwargs):
    raise WorkerDied("worker died mid-execution")


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "NOTES.md").write_text("recovered\n")
    return "wrote NOTES.md"


@scenario(
    "S16",
    layer=Layer.LIVE_GITHUB,
    invariants=[
        "INV-ONE-INITIAL",
        "INV-PERMIT-BOUND",
        "INV-RETRY-BOUNDED",
        "INV-PROVENANCE",
    ],
    description="A live crashed INITIAL is recovered under its own identity.",
)
def s16_live_crash_during_initial(root_dir) -> Observation:
    live = open_live_thread(
        "S16",
        root_dir,
        body="@agent update the notes",
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    world = live.world
    with world.activate():
        world.drive(
            live.thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=10
        )
        permit = live.approve()

        crashed = False
        try:
            world.tick(
                live.thread_id,
                execute_kwargs={
                    "lock_root": world.root / "locks",
                    "runner": _crashing_runner,
                    "checkpointer": object(),
                },
            )
        except WorkerDied:
            crashed = True
        assert crashed, "the runner never ran, so nothing crashed"

        orphan = world.store.latest_attempt(live.thread_id, 1)
        assert orphan.status == AttemptStatus.RUNNING.value, (
            f"the crash did not leave an orphan: {orphan.status}"
        )
        workspace_before = world.store.thread_workspace(live.thread_id)

        world.tick(
            live.thread_id,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _writing_runner,
                "checkpointer": object(),
            },
        )
        recovered = world.store.latest_attempt(live.thread_id, 1)

        assert recovered.attempt_id == orphan.attempt_id, (
            "recovery created a new attempt instead of reclaiming the orphan"
        )
        assert recovered.status != AttemptStatus.RUNNING.value, (
            "the orphan was left RUNNING after recovery"
        )
        assert recovered.authorization_id == permit.permit_id, (
            "recovery rebound the attempt to a different permit"
        )
        initials = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_attempts "
            "WHERE thread_id=? AND kind='INITIAL'",
            (live.thread_id,),
        ).fetchone()[0]
        assert initials == 1, f"recovery produced {initials} INITIAL attempts"

        workspace_after = world.store.thread_workspace(live.thread_id)
        assert workspace_after.workspace_path == workspace_before.workspace_path, (
            "recovery moved the thread workspace"
        )
        assert workspace_after.branch_name == workspace_before.branch_name, (
            "recovery renamed the execution branch"
        )
    return world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S16", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s16_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S16", tmp_path / "s16-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
