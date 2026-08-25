"""Approval requires repository write access (F4).

Every other approval precondition was enforced -- exact text, phase,
conversation target, posted plan, ordering -- but the author was recorded and
never checked. On a public repository that let any GitHub user authorize
execution.
"""

import pytest
from harness.models import ScriptedPlanner
from harness.world import World

from sweforge.github_store import WorkflowPhase
from sweforge.workflow import (
    APPROVER_PERMISSIONS,
    UnauthorizedApprover,
    _authorized_to_approve,
)

PLAN = "1. a\n2. b"


def _world_at_approval(tmp_path, **fake_kwargs):
    world = World.build(tmp_path, planner=ScriptedPlanner(plans=[PLAN]), **fake_kwargs)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    return world


@pytest.mark.parametrize("permission", sorted(APPROVER_PERMISSIONS))
def test_a_writer_may_approve(permission):
    assert _authorized_to_approve(permission)


@pytest.mark.parametrize("permission", ["read", "triage", "none", ""])
def test_a_non_writer_may_not_approve(permission):
    """triage and read can both comment and neither can change the repo."""
    assert not _authorized_to_approve(permission)


def test_permission_matching_ignores_case_and_padding():
    assert _authorized_to_approve("  Write  ")


def test_an_approval_from_a_read_only_commenter_is_refused(tmp_path):
    world = _world_at_approval(tmp_path / "ro", permissions={"octocat": "read"})
    with world.activate():
        approval = world.event(
            "2", "@agent approve", world.later(), author_login="octocat"
        )
        world.ingest(approval)
        with pytest.raises(UnauthorizedApprover) as excinfo:
            world.engine.approve(event_key=approval.event_key)
    assert "read" in str(excinfo.value)


def test_a_refused_approval_mints_no_permit(tmp_path):
    """The security property: refusal must not authorize execution."""
    world = _world_at_approval(tmp_path / "nopermit", permissions={"octocat": "read"})
    with world.activate():
        approval = world.event(
            "2", "@agent approve", world.later(), author_login="octocat"
        )
        world.ingest(approval)
        with pytest.raises(UnauthorizedApprover):
            world.engine.approve(event_key=approval.event_key)
        thread_id = next(iter(world.thread_ids))
        state = world.engine.store.workflow_state(thread_id)
    assert state.phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL


def test_a_writer_approval_still_succeeds(tmp_path):
    """Positive control: the check must not refuse everyone, or the refusal
    tests above would pass against a wholly broken approval path."""
    world = _world_at_approval(tmp_path / "ok", permissions={"octocat": "write"})
    with world.activate():
        approval = world.event(
            "2", "@agent approve", world.later(), author_login="octocat"
        )
        world.ingest(approval)
        permit = world.engine.approve(event_key=approval.event_key)
    assert permit.permit_id


def test_an_undeterminable_permission_is_refused(tmp_path):
    """Fails closed: a client that cannot answer must not be read as consent."""

    class Unanswerable:
        def repository(self, full_name):
            raise RuntimeError("github unreachable")

    world = _world_at_approval(tmp_path / "closed", permissions={"octocat": "write"})
    with world.activate():
        approval = world.event(
            "2", "@agent approve", world.later(), author_login="octocat"
        )
        world.ingest(approval)
        world.engine.client = Unanswerable()
        with pytest.raises(UnauthorizedApprover) as excinfo:
            world.engine.approve(event_key=approval.event_key)
    assert "could not determine" in str(excinfo.value)
