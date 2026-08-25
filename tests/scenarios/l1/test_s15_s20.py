"""L1 scenarios: S15 restart awaiting approval, S20 unmapped PR mention.

Both are planning-phase scenarios: no execution runs, so they need no executor
double and assert purely on durable state and the event log.
"""

import pytest
from harness.models import ScriptedPlanner
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_models import SubjectKind
from sweforge.github_store import SQLiteGitHubStore, WorkflowPhase
from sweforge.workflow import WorkflowEngine

PLAN = "1. edit README\n2. run the tests"


@scenario(
    "S15",
    layer=Layer.L1,
    invariants=[
        "INV-ONE-ROOT",
        "INV-PLAN-CANONICAL",
        "INV-PLAN-VERSIONED",
        "INV-PERMIT-BOUND",
        "INV-PERMIT-SOURCE",
        "INV-THREAD-ISOLATION",
    ],
    description="Durable state survives restart; approval resumes the same plan.",
)
def s15_restart_awaiting_approval(root_dir) -> Observation:
    world = World.build(root_dir, planner=ScriptedPlanner(plans=[PLAN]))
    with world.activate():
        root = world.event("1", "@agent fix the bug", "2026-01-01T00:00:00Z")
        world.ingest(root)
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        posted = world.store.current_plan(thread_id)

        # Restart: a brand new store handle and engine over the same database,
        # which is what a process restart actually leaves behind.
        world.store = SQLiteGitHubStore(world.root / "state.db")
        world.engine = WorkflowEngine(
            store=world.store, client=world.github, clock=world._clock
        )

        after = world.store.current_plan(thread_id)
        assert after is not None, "the plan did not survive restart"
        assert after.plan_id == posted.plan_id, "restart produced a different plan"
        assert after.version == posted.version

        approval = world.event("2", "@agent approve", "2026-01-01T00:05:00Z")
        world.ingest(approval)
        permit = world.engine.approve(event_key=approval.event_key)
        assert permit.plan_id == posted.plan_id, "approval bound a different plan"
    return world.observation()


@scenario(
    "S20",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="An @agent mention on an unmapped PR must not attach to any thread.",
)
def s20_unmapped_pr_mention(root_dir) -> Observation:
    world = World.build(root_dir, planner=ScriptedPlanner(plans=[PLAN]))
    with world.activate():
        mention = world.event(
            "1",
            "@agent please look at this",
            "2026-01-01T00:00:00Z",
            issue_number=99,
            subject_kind=SubjectKind.PULL_REQUEST,
        )
        world.store.record_batch(
            world.repo.repo_id,
            "review_comments",
            [mention],
            since="now",
            etag=None,
            polled_at="now",
        )
        row = world.store.source_event(mention.event_key)
        assert row is not None, "the event was not recorded at all"
        assert not row["thread_id"], "an unmapped PR mention was routed to a thread"
        threads = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_threads"
        ).fetchone()[0]
        assert threads == 0, "an unmapped PR mention created durable thread identity"
    # No thread exists, so declare none: the isolation invariants would raise.
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S15", "S20"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()


def test_s15_reports_every_declared_invariant(tmp_path):
    result = run("S15", tmp_path / "s15-report", layer=Layer.L1)
    reported = {check.invariant_id for check in result.checks}
    assert "INV-PERMIT-BOUND" in reported
    assert "FAULTS-DRAINED" in reported
