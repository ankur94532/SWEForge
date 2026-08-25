"""S2 LIVE_GITHUB: every answer type resumes the cycle it interrupted.

Four answer shapes, each on its own real issue. For each, an unrelated comment
must not be mistaken for the answer, and the real answer must resume the same
cycle rather than starting a new one. Live this also proves the answer survives
the round trip through GitHub and the poller, not just a direct store write.
"""

from pathlib import Path

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, open_live_thread
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from sweforge.github_store import ClarificationStatus, WorkflowPhase

PLAN = "1. touch the notes file\n2. run the tests"

ANSWER_TYPES = [
    ("CHOICE", ("us-east-1", "eu-west-1"), "us-east-1"),
    ("BOOLEAN", (), "yes"),
    ("TEXT", (), "use the staging bucket"),
    ("VALUE", (), "42"),
]


def _asking_runner(answer_type: str, choices: tuple[str, ...], occurrence: str):
    """Request a clarification on the first call, then do the work."""
    state = {"asked": False}

    def runner(**kwargs):
        if not state["asked"]:
            state["asked"] = True
            kwargs["interrupt_result_sink"](
                {
                    "question": f"Which value? ({answer_type})",
                    "reason": "the plan is ambiguous without it",
                    "answer_type": answer_type,
                    "choices": list(choices),
                    "occurrence_key": occurrence,
                }
            )
            return "asked for clarification"
        (Path(kwargs["worktree"]) / "NOTES.md").write_text("answered\n")
        return "resumed after the answer"

    return runner


@scenario(
    "S2",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-PERMIT-BOUND", "INV-THREAD-ISOLATION"],
    description="Every live answer type resumes the same cycle it interrupted.",
)
def s2_live_clarification_gauntlet(root_dir) -> Observation:
    covered: list[str] = []
    live = None
    for index, (answer_type, choices, answer) in enumerate(ANSWER_TYPES):
        current = open_live_thread(
            f"S2-{answer_type}",
            root_dir / f"pass-{index}",
            body=f"@agent update the notes ({answer_type})",
            planner=ScriptedPlanner(plans=[PLAN]),
            reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
        )
        live = live or current
        world = current.world
        with world.activate():
            world.drive(
                current.thread_id,
                until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
                max_ticks=10,
            )
            current.approve()
            runner = _asking_runner(answer_type, choices, f"tc-{answer_type.lower()}")
            execute_kwargs = {
                "lock_root": world.root / "locks",
                "runner": runner,
                "checkpointer": object(),
            }

            result = world.tick(current.thread_id, execute_kwargs=execute_kwargs)
            assert result.phase is WorkflowPhase.WAITING_FOR_INPUT, (
                f"{answer_type}: expected WAITING_FOR_INPUT, got {result.phase}"
            )
            request = world.store.clarification_for_thread(current.thread_id)
            assert request is not None and request.answer_type == answer_type
            cycle_before = world.store.workflow_state(current.thread_id).cycle_id

            # Unrelated input must not be mistaken for the answer.
            current.say("@agent by the way, nice work")
            world.tick(current.thread_id, execute_kwargs=execute_kwargs)
            still_open = world.store.clarification_for_thread(current.thread_id)
            assert still_open is not None, (
                f"{answer_type}: unrelated input closed the clarification"
            )
            assert still_open.status == ClarificationStatus.OPEN.value, (
                f"{answer_type}: unrelated input answered it "
                f"(status {still_open.status})"
            )

            # The real answer resumes the same cycle.
            current.say(f"@agent {answer}")
            world.tick(current.thread_id, execute_kwargs=execute_kwargs)
            state = world.store.workflow_state(current.thread_id)
            assert state.cycle_id == cycle_before, (
                f"{answer_type}: the answer started a new cycle "
                f"({cycle_before} -> {state.cycle_id})"
            )
            initials = world.store.connection.execute(
                "SELECT COUNT(*) FROM execution_attempts "
                "WHERE thread_id=? AND kind='INITIAL'",
                (current.thread_id,),
            ).fetchone()[0]
            assert initials == 1, (
                f"{answer_type}: resuming produced {initials} INITIAL attempts"
            )
            covered.append(answer_type)

    assert covered == [item[0] for item in ANSWER_TYPES], (
        f"not every answer type was exercised: {covered}"
    )
    return live.world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S2", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s2_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S2", tmp_path / "s2-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
