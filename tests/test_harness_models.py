"""Doubles must be deterministic, and must refuse to improvise."""

import pytest
from harness.models import (
    FaultyRole,
    ReplayRole,
    RoleExhausted,
    ScriptedChatModel,
    ScriptedPlanner,
    ScriptedReviewer,
)
from harness.world import World
from langchain_core.messages import AIMessage

from sweforge.github_store import WorkflowPhase


def test_scripted_planner_returns_plans_in_order_then_holds():
    planner = ScriptedPlanner(plans=["first", "second"])
    assert planner(task="a") == "first"
    assert planner(task="b") == "second"
    assert planner(task="c") == "second"
    assert len(planner.calls) == 3
    assert planner.calls[0]["task"] == "a"


def test_scripted_reviewer_returns_verdicts_in_order_then_holds():
    reviewer = ScriptedReviewer(verdicts=["NEEDS_FIXES", "ACCEPT"])
    assert reviewer(evidence={}).verdict == "NEEDS_FIXES"
    assert reviewer(evidence={}).verdict == "ACCEPT"
    # A repair loop may review more times than a scenario scripted; the double
    # holds so the scenario fails on its invariants, not on the double.
    assert reviewer(evidence={}).verdict == "ACCEPT"


def test_scripted_reviewer_produces_a_real_result_object():
    result = ScriptedReviewer(verdicts=["BLOCKED"], summary="nope")(evidence={})
    assert result.verdict == "BLOCKED"
    assert result.summary == "nope"
    assert result.requirement_checks == []


def test_replay_role_returns_recorded_responses_in_order():
    role = ReplayRole(responses=["one", "two"])
    assert role(x=1) == "one"
    assert role(x=2) == "two"


def test_replay_role_refuses_to_improvise_past_the_cassette():
    """A replay that invents responses is no longer a replay."""
    role = ReplayRole(responses=["only"])
    role()
    with pytest.raises(RoleExhausted, match="1 response"):
        role()


def test_faulty_role_fails_only_at_the_declared_call_index():
    inner = ScriptedPlanner(plans=["ok"])
    role = FaultyRole(inner=inner, fail_on_calls=[2])
    assert role(task="a") == "ok"
    with pytest.raises(RuntimeError, match="injected role failure on call 2"):
        role(task="b")
    assert role(task="c") == "ok"


def test_faulty_role_can_raise_a_specific_error():
    role = FaultyRole(
        inner=ScriptedPlanner(), fail_on_calls=[1], error=ValueError("boom")
    )
    with pytest.raises(ValueError, match="boom"):
        role(task="a")


def test_faulty_role_is_a_pass_through_when_nothing_is_declared():
    role = FaultyRole(inner=ScriptedPlanner(plans=["ok"]))
    assert [role(task="a"), role(task="b")] == ["ok", "ok"]


def test_scripted_chat_model_records_bound_tool_surfaces():
    model = ScriptedChatModel([AIMessage(content="done")])
    model.bind_tools([{"name": "read_repo_file"}])
    assert model.surfaces == [("read_repo_file",)]


def test_scripted_chat_model_refuses_to_improvise():
    model = ScriptedChatModel([AIMessage(content="one")])
    assert model.invoke("hello").content == "one"
    with pytest.raises(RoleExhausted, match="ran out of responses"):
        model.invoke("again")


def test_scripted_planner_drives_a_real_world_to_plan_approval(tmp_path):
    """The double must satisfy the engine's real call signature, not a guess."""
    planner = ScriptedPlanner(plans=["1. edit README\n2. run tests"])
    world = World.build(tmp_path / "w", planner=planner)
    root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    world.ingest(root)
    thread_id = next(iter(world.thread_ids))
    results = world.drive(
        thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=5
    )
    assert results[-1].phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
    assert planner.calls, "the engine never called the injected planner"
