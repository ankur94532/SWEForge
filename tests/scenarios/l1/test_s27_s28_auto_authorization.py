"""S27 and S28: the AUTO authorization path, where no human approves.

COVERAGE-GAPS.md names these the two it would not ship without, and for good
reason: `authorize_auto` mints an execution permit with no approval event and
no approver. Every other authorization scenario proves the human path. These
prove the one that bypasses it.

S27 drives a full AUTO lifecycle. S28 drives the race on the boundary: the
label is removed after planning but before the permit is minted, and the
system must fall back to interactive approval rather than authorizing itself.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import PermitSource, WorkflowMode, WorkflowPhase

PLAN = "1. edit README\n2. run the tests"


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "README.md").write_text("auto\n")
    return "edited README"


def _auto_world(root_dir, *, labelled: bool = True) -> World:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    _label(world, labelled)
    return world


def _label(world: World, labelled: bool) -> None:
    """AUTO mode is decided by a label on the real issue."""
    world.github.issue_payloads = {
        (world.repo.repo_id, 7): {"labels": [{"name": "auto"}] if labelled else []}
    }


@scenario(
    "S27",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-ROOT",
        "INV-PLAN-CANONICAL",
        "INV-PERMIT-BOUND",
        "INV-PERMIT-SOURCE",
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-PROVENANCE",
    ],
    description="AUTO mode authorizes without a human and stays bound to its plan.",
)
def s27_auto_mode_lifecycle(root_dir) -> Observation:
    world = _auto_world(root_dir)
    with world.activate():
        world.ingest(world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)

        state = world.store.workflow_state(thread_id)
        assert state.mode == WorkflowMode.AUTO, (
            f"the auto label did not select AUTO mode: {state.mode}"
        )
        plan = world.store.current_plan(thread_id)
        assert plan is not None, "planning produced no current plan"
        assert state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, (
            f"phase was {state.phase}, plan status {plan.status}"
        )
        permit = world.engine.authorize_auto(thread_id=thread_id)

        assert permit.source == PermitSource.AUTO, (
            f"an AUTO permit was minted with source {permit.source}"
        )
        assert permit.source_event_key is None, (
            "an AUTO permit cited an approval event; nothing approved it"
        )
        assert permit.plan_id == plan.plan_id, "the permit left its plan"
        assert permit.plan_version == plan.version

        # A second authorization must not mint a second permit. It refuses
        # rather than returning the existing one: authorize_auto checks the
        # phase before its `existing` lookup, and the first call advances the
        # phase, so that idempotency branch is unreachable. Fail-closed is the
        # safe direction, and the property under test is that no second permit
        # appears -- not which way the repeat call reports it.
        with pytest.raises(ValueError, match="requires a current posted plan"):
            world.engine.authorize_auto(thread_id=thread_id)
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 1, f"AUTO minted {permits} permits"

        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _writing_runner,
                "checkpointer": object(),
            },
        )
        approvals = world.store.connection.execute(
            "SELECT COUNT(*) FROM source_events WHERE body LIKE '%@agent approve%'"
        ).fetchone()[0]
        assert approvals == 0, "AUTO mode consumed a human approval after all"
    return world.observation()


@scenario(
    "S28",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION", "INV-PLAN-CANONICAL"],
    description="Removing the AUTO label before authorization falls back to human.",
)
def s28_auto_label_removed_before_permit(root_dir) -> Observation:
    world = _auto_world(root_dir)
    with world.activate():
        world.ingest(world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        assert world.store.workflow_state(thread_id).mode == WorkflowMode.AUTO

        # The race: the label is removed after planning, before authorization.
        _label(world, False)

        with pytest.raises(ValueError, match="AUTO label was removed"):
            world.engine.authorize_auto(thread_id=thread_id)

        state = world.store.workflow_state(thread_id)
        assert state.mode == WorkflowMode.INTERACTIVE, (
            f"the thread stayed in {state.mode} after the label was removed"
        )
        assert state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, (
            "the thread did not fall back to waiting for a human"
        )
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 0, f"authorization minted {permits} permits after the race"
    return world.observation()


def test_auto_is_a_distinct_permit_source():
    """Positive control: AUTO must be its own source, not aliased to USER."""
    assert PermitSource.AUTO != PermitSource.USER


@pytest.mark.parametrize("scenario_id", ["S27", "S28"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
