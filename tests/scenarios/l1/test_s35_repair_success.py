"""S35: a repair that succeeds and is accepted.

S26 covers review infrastructure failing and S49 covers repair recovery
reaching its bound, but nothing covered repair actually working. The success
path is where a second execution runs under a *different* authorization -- a
repair permit rather than the original plan permit -- and getting that wrong
means either a repair that cannot run or an execution running unauthorized.
"""

import json

from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World
from test_repair_retry_bound import _seed_repair_attempt

from sweforge.github_store import (
    AttemptStatus,
    ExecutionReviewRecord,
    SQLiteGitHubStore,
    WorkflowPhase,
)


@scenario(
    "S35",
    layer=Layer.L1,
    invariants=["INV-PERMIT-BOUND"],
    # INV-ONE-INITIAL and INV-ATTEMPT-TERMINAL are deliberately absent: this
    # scenario seeds through the store's own API rather than the worker, so no
    # attempt events exist and both reported VACUOUS. The single-INITIAL
    # property is asserted directly in the body against the attempts table.
    description="A successful repair runs under its own permit and is accepted.",
)
def s35_repair_succeeds_and_is_accepted(root_dir) -> Observation:
    # Seeded inside a world so the event log is live: the permit and attempt
    # invariants read events, and refuse to evaluate an empty log rather than
    # treating it as a pass.
    world = World.build(root_dir)
    with world.activate():
        return _drive(world)


def _drive(world) -> Observation:
    root_dir = world.root
    store = world.store
    thread_id, repair = _seed_repair_attempt(store)

    # The repair is a distinct attempt under a distinct authorization.
    assert repair.kind == "REVIEW_REPAIR", f"repair attempt kind was {repair.kind}"
    assert repair.repair_round == 1, f"repair_round was {repair.repair_round}"
    initial = store.connection.execute(
        "SELECT authorization_id FROM execution_attempts WHERE kind='INITIAL'"
    ).fetchone()
    assert repair.authorization_id != initial["authorization_id"], (
        "the repair reused the original plan permit instead of its own"
    )

    completed = store.finish_repair_attempt_success(
        repair.authorization_id,
        attempt_id=repair.attempt_id,
        now="now",
        response_text="fixed the boundary",
        end_head_sha="c",
        end_dirty=False,
        workspace_path=str(root_dir / "ws"),
        start_head_sha="b",
    )
    assert completed.status == AttemptStatus.SUCCEEDED.value, (
        f"a successful repair recorded {completed.status}"
    )

    # The repair permit is spent, so it cannot authorize a second execution.
    permit_row = store.connection.execute(
        "SELECT consumed_at, invalidated_at FROM review_repair_permits "
        "WHERE permit_id=?",
        (repair.authorization_id,),
    ).fetchone()
    assert permit_row["consumed_at"] is not None, "the repair permit stayed live"

    # The thread returns to review, where the repair can be accepted.
    state = store.workflow_state(thread_id)
    assert state.phase == WorkflowPhase.REVIEW_EXECUTION, (
        f"a successful repair left the thread in {state.phase}"
    )

    store.save_execution_review(
        ExecutionReviewRecord(
            review_id="r2",
            thread_id=thread_id,
            cycle_id=state.cycle_id,
            plan_id=state.current_plan_id,
            plan_version=1,
            root_event_key=state.root_event_key,
            attempt_id=repair.attempt_id,
            review_iteration=2,
            verdict="ACCEPT",
            summary="repair accepted",
            findings_json=json.dumps([]),
            repair_instructions_json=json.dumps([]),
            created_at="now",
            completed_at="now",
        )
    )
    accepted = store.execution_review_for_attempt(repair.attempt_id)
    assert accepted is not None and accepted.verdict == "ACCEPT"

    # Exactly one INITIAL throughout: repairing never re-runs the original.
    initials = store.connection.execute(
        "SELECT COUNT(*) FROM execution_attempts WHERE thread_id=? AND kind='INITIAL'",
        (thread_id,),
    ).fetchone()[0]
    assert initials == 1, f"the repair produced {initials} INITIAL attempts"
    world.thread_ids.add(thread_id)
    return world.observation()


def test_a_repair_permit_is_not_the_plan_permit():
    """Positive control: the two authorizations must be different tables, so a
    spent repair permit can never be mistaken for standing plan authority."""
    import tempfile
    from pathlib import Path

    store = SQLiteGitHubStore(Path(tempfile.mkdtemp()) / "s.db")
    tables = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"execution_permits", "review_repair_permits"} <= tables


def test_scenario_passes(tmp_path):
    result = run("S35", tmp_path / "s35", layer=Layer.L1)
    assert result.ok, "\n" + result.report()
