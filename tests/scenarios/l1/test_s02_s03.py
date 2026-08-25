"""L1 scenarios S2 and S3: the clarification gauntlet and scope invalidation.

A runner double asks for clarification through `interrupt_result_sink`, which
is the same seam the real agent uses when a LangGraph interrupt fires. That
avoids standing up a graph while still exercising SWEForge's own persistence,
routing and resume path rather than a stub of it.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import ClarificationStatus, WorkflowPhase

PLAN = "1. edit README\n2. run the tests"

# One case per supported answer type. Each must reach WAITING_FOR_INPUT and be
# answerable, and the answer must resume the cycle it interrupted.
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
        (Path(kwargs["worktree"]) / "README.md").write_text("resumed\n")
        return "resumed after the answer"

    return runner


def _approved(world: World, issue_number: int = 7) -> str:
    world.ingest(
        world.event(
            f"root-{issue_number}",
            "@agent fix the bug",
            "2026-01-01T00:00:00Z",
            issue_number=issue_number,
        )
    )
    thread_id = next(t for t in world.thread_ids if t.endswith(f":{issue_number}"))
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    approval = world.event(
        f"approve-{issue_number}",
        "@agent approve",
        world.later(),
        issue_number=issue_number,
    )
    world.ingest(approval)
    world.engine.approve(event_key=approval.event_key)
    return thread_id


@scenario(
    "S2",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-PERMIT-BOUND", "INV-THREAD-ISOLATION"],
    description="Every answer type resumes the same cycle it interrupted.",
)
def s2_clarification_gauntlet(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    covered: list[str] = []
    with world.activate():
        for index, (answer_type, choices, answer) in enumerate(ANSWER_TYPES):
            issue_number = 7 + index
            thread_id = _approved(world, issue_number)
            runner = _asking_runner(answer_type, choices, f"tc-{answer_type.lower()}")
            execute_kwargs = {"runner": runner, "checkpointer": object()}

            result = world.tick(thread_id, execute_kwargs=execute_kwargs)
            assert result.phase is WorkflowPhase.WAITING_FOR_INPUT, (
                f"{answer_type}: expected WAITING_FOR_INPUT, got {result.phase}"
            )
            request = world.store.clarification_for_thread(thread_id)
            assert request is not None and request.answer_type == answer_type
            cycle_before = world.store.workflow_state(thread_id).cycle_id

            # Unrelated input must not be mistaken for the answer.
            noise = world.event(
                f"noise-{issue_number}",
                "@agent by the way, nice work",
                world.later(),
                issue_number=issue_number,
            )
            world.ingest(noise)
            world.tick(thread_id, execute_kwargs=execute_kwargs)
            still_open = world.store.clarification_for_thread(thread_id)
            assert still_open is not None, (
                f"{answer_type}: unrelated input closed the clarification"
            )
            assert still_open.status == ClarificationStatus.OPEN.value, (
                f"{answer_type}: unrelated input answered the question "
                f"(status {still_open.status})"
            )

            # The real answer resumes the same cycle.
            reply = world.event(
                f"answer-{issue_number}",
                f"@agent {answer}",
                world.later(),
                issue_number=issue_number,
            )
            world.ingest(reply)
            world.tick(thread_id, execute_kwargs=execute_kwargs)

            state = world.store.workflow_state(thread_id)
            assert state.cycle_id == cycle_before, (
                f"{answer_type}: the answer started a new cycle "
                f"({cycle_before} -> {state.cycle_id})"
            )
            attempts = world.store.connection.execute(
                "SELECT COUNT(*) FROM execution_attempts "
                "WHERE thread_id=? AND kind='INITIAL'",
                (thread_id,),
            ).fetchone()[0]
            assert attempts == 1, (
                f"{answer_type}: resuming created {attempts} INITIAL attempts"
            )
            covered.append(answer_type)

        assert covered == [item[0] for item in ANSWER_TYPES], (
            f"not every answer type was exercised: {covered}"
        )
    return world.observation()


@scenario(
    "S3",
    layer=Layer.L1,
    invariants=["INV-PERMIT-BOUND", "INV-PLAN-CANONICAL", "INV-THREAD-ISOLATION"],
    description="A clarification cannot silently widen what was authorized.",
)
def s3_scope_change_invalidates_authorization(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        thread_id = _approved(world)
        plan_before = world.store.current_plan(thread_id)
        permit_before = world.store.permit_for_plan(plan_before.plan_id)

        runner = _asking_runner("TEXT", (), "tc-scope")
        execute_kwargs = {"runner": runner, "checkpointer": object()}
        result = world.tick(thread_id, execute_kwargs=execute_kwargs)
        assert result.phase is WorkflowPhase.WAITING_FOR_INPUT

        # The permit that authorized this execution still names the exact plan
        # and version it was minted for; a clarification cannot move it.
        permit_after = world.store.permit_for_plan(plan_before.plan_id)
        assert permit_after.permit_id == permit_before.permit_id
        assert permit_after.plan_id == plan_before.plan_id
        assert permit_after.plan_version == plan_before.version

        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 1, (
            f"waiting for clarification minted {permits} permits; authorization "
            "must not multiply while a question is open"
        )
        plans = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_plans WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        assert plans == 1, f"a clarification created {plans} plans"
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S2", "S3"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
