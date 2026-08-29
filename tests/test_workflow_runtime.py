import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_middleware import WorkflowAuthority
from sweforge.workflow_runtime import (
    TaskPhase,
    ValidationVerdict,
    WorkflowCycleStatus,
    WorkflowRuntime,
)
from sweforge.workflow_spec import parse_workflow_spec
from sweforge.workflow_tools import build_lifecycle_tools


def _event(repo):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id="1",
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="owner",
        body="@agent implement the workflow",
        html_url=None,
    )


def _spec(order=("A", "B", "C", "D")):
    deps = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    return parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "test-flow",
            "tasks": [
                {
                    "id": name,
                    "depends_on": deps[name],
                    "planning": {
                        "skill": f"{name}-planning",
                        "tools": ["read_file", "glob", "grep"],
                    },
                    "execution": {
                        "skill": f"{name}-execution",
                        "tools": ["read_file", "write_file", "edit_file", "execute"],
                    },
                    "validation": {
                        "skill": f"{name}-validation",
                        "tools": ["read_file", "run_validation"],
                    },
                }
                for name in order
            ],
        }
    )


@pytest.fixture
def runtime(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    event = _event(repo)
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [event],
        since="now",
        etag=None,
        polled_at="now",
    )
    thread_id = store.source_event(event.event_key)["thread_id"]
    engine = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    cycle = engine.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=event.event_key,
        spec=_spec(),
        spec_ref="/etc/sweforge/workflow.yaml",
    )
    yield store, engine, cycle
    store.close()


def _approve_execute_validate(engine, task, verdict=ValidationVerdict.ACCEPT):
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text=f"Plan for {task.task_id}",
        posted_comment_id=100 + task.declaration_index,
        posted_at="2026-01-01T00:11:00Z",
    )
    permit = engine.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key=f"approval-{task.task_id}",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )
    assert permit.plan_digest == plan.plan_digest
    validating = engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"tests": "passed"},
            "tool_observations": [
                {"command": "tests", "exit_code": 0, "output": "passed"}
            ],
        },
    )
    assert validating.phase == TaskPhase.VALIDATING
    validated = engine.finish_validation(
        task_run_id=task.task_run_id,
        verdict=verdict,
        summary="validated",
        findings=[],
        repair_instructions=["repair"] if verdict != ValidationVerdict.ACCEPT else [],
        evidence={
            "reported": {"tests": "passed"},
            "validation_runs": [{"diff": "", "executions": []}],
        },
    )
    if verdict != ValidationVerdict.ACCEPT:
        return validated
    result = engine.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=200 + task.declaration_index,
        posted_at="2026-01-01T00:13:00Z",
    )
    engine.approve_result(
        task_run_id=task.task_run_id,
        occurrence_key=result.result_occurrence_key,
        approval_event_key=f"result-approval-{task.task_id}",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:14:00Z",
    )
    return engine.task(task.task_run_id)


def test_diamond_runs_strictly_serial_in_declaration_order(runtime):
    _, engine, cycle = runtime
    selected = []
    for expected in ("A", "B", "C", "D"):
        task = engine.select_active_task(cycle.workflow_cycle_id)
        assert task.task_id == expected
        selected.append(task.task_id)
        assert engine.select_active_task(cycle.workflow_cycle_id).task_id == expected
        _approve_execute_validate(engine, task)
    assert selected == ["A", "B", "C", "D"]
    assert engine.select_active_task(cycle.workflow_cycle_id) is None
    finished = engine.cycle(cycle.workflow_cycle_id)
    assert finished.status == WorkflowCycleStatus.AWAITING_PUBLICATION
    assert finished.active_task_id is None
    assert engine.publication_is_eligible(cycle.workflow_cycle_id)


def test_shared_workflow_connection_serializes_parallel_tool_reads(runtime):
    """Parallel agent tools must not corrupt reads on the shared SQLite handle."""
    _, engine, cycle = runtime
    selected = engine.select_active_task(cycle.workflow_cycle_id)
    assert selected.task_id == "A"
    workers = 16
    start = Barrier(workers)

    def read_active_cycle() -> None:
        start.wait()
        for _ in range(500):
            current = engine.cycle(cycle.workflow_cycle_id)
            active = engine.active_task(cycle.workflow_cycle_id)
            assert current.status == WorkflowCycleStatus.ACTIVE
            assert current.active_task_id == "A"
            assert active is not None and active.task_id == "A"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(read_active_cycle) for _ in range(workers)]
        for future in futures:
            future.result()


def test_waiting_for_approval_retains_owner_and_does_not_start_ready_peer(runtime):
    _, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="A plan",
        posted_comment_id=99,
        posted_at="2026-01-01T00:11:00Z",
    )
    assert plan.status == "POSTED"
    assert engine.select_active_task(cycle.workflow_cycle_id).task_id == "A"
    statuses = {
        item.task_id: item.status for item in engine.task_runs(cycle.workflow_cycle_id)
    }
    assert statuses["A"] == TaskPhase.WAITING_FOR_PLAN_APPROVAL
    assert statuses["B"] == statuses["C"] == statuses["D"] == TaskPhase.PENDING

    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, _spec())
    with pytest.raises(PermissionError, match="not runnable"):
        authority.snapshot()
    assert authority.snapshot_for_tool("submit_plan").phase == TaskPhase.PLANNING
    with pytest.raises(PermissionError, match="not runnable"):
        authority.snapshot_for_tool("read_file")


def test_b_selected_before_c_and_d_waits_for_both(runtime):
    _, engine, cycle = runtime
    a = engine.select_active_task(cycle.workflow_cycle_id)
    _approve_execute_validate(engine, a)
    b = engine.select_active_task(cycle.workflow_cycle_id)
    assert b.task_id == "B"
    _approve_execute_validate(engine, b)
    c = engine.select_active_task(cycle.workflow_cycle_id)
    assert c.task_id == "C"
    assert (
        next(
            t for t in engine.task_runs(cycle.workflow_cycle_id) if t.task_id == "D"
        ).status
        == TaskPhase.PENDING
    )


def test_validation_repairs_and_replans_keep_same_owner(runtime):
    _, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    repaired = _approve_execute_validate(engine, task, ValidationVerdict.NEEDS_FIXES)
    assert repaired.phase == TaskPhase.EXECUTING
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == "A"
    engine.finish_execution(task.task_run_id)
    replanning = engine.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.REPLAN,
        summary="scope must change",
        findings=[{"scope": "changed"}],
        repair_instructions=["revise the plan"],
        evidence={"diff": "out of approved scope"},
    )
    assert replanning.phase == TaskPhase.PLANNING
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == "A"
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)


def test_clarification_occurrence_retains_owner_and_cannot_cross_resume(runtime):
    _, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    paused = engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
    )
    assert paused.phase == TaskPhase.WAITING_FOR_INPUT
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == "A"
    assert engine.select_active_task(cycle.workflow_cycle_id).task_id == "A"
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, _spec())
    with pytest.raises(PermissionError, match="not runnable"):
        authority.snapshot()
    assert (
        authority.snapshot_for_tool("request_clarification").phase == TaskPhase.PLANNING
    )
    with pytest.raises(ValueError, match="stale"):
        engine.resume_clarification(
            task_run_id=task.task_run_id,
            occurrence_key="plan-approval:A:wrong-kind",
        )
    assert (
        engine.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
        ).phase
        == TaskPhase.PLANNING
    )


def test_stale_approval_and_unposted_plan_fail_closed(runtime):
    _, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    with pytest.raises(ValueError, match="comment"):
        engine.submit_posted_plan(
            task_run_id=task.task_run_id,
            plan_text="not visible",
            posted_comment_id=0,
            posted_at="2026-01-01T00:11:00Z",
        )
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="visible",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    with pytest.raises(ValueError, match="stale"):
        engine.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=replace(
                plan, approval_occurrence_key="old"
            ).approval_occurrence_key,
            approval_event_key="approval",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:12:00Z",
        )
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)


def test_feedback_revises_same_task_and_invalidates_old_plan(runtime):
    _, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    old = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="old plan",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    assert engine.replan_from_feedback(task.task_run_id).phase == TaskPhase.PLANNING
    revised = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="revised plan",
        posted_comment_id=2,
        posted_at="2026-01-01T00:13:00Z",
    )
    assert revised.version == 2
    assert engine.plan(old.plan_id).status == "SUPERSEDED"
    with pytest.raises(ValueError, match="stale"):
        engine.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=old.approval_occurrence_key,
            approval_event_key="stale-approval",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:14:00Z",
        )
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == "A"


def test_restart_recovers_exact_active_task_and_phase(runtime):
    store, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="durable",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    restarted = WorkflowRuntime(store)
    recovered = restarted.select_active_task(cycle.workflow_cycle_id)
    assert recovered.task_run_id == task.task_run_id
    assert recovered.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL


def test_restart_rehydrates_exact_persisted_workflow_spec(runtime):
    store, engine, cycle = runtime
    recovered = engine.spec_for_cycle(cycle.workflow_cycle_id)
    assert recovered.workflow_id == cycle.workflow_id
    assert recovered.digest == cycle.workflow_digest
    store.connection.execute(
        """UPDATE workflow_cycles_v1 SET workflow_spec_json='{}'
           WHERE workflow_cycle_id=?""",
        (cycle.workflow_cycle_id,),
    )
    store.connection.commit()
    with pytest.raises(ValueError, match="persisted workflow specification"):
        engine.spec_for_cycle(cycle.workflow_cycle_id)


def test_reordered_workflow_needs_only_configuration_change(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(321, "example/reordered")
    event = replace(_event(repo), subject_number=8)
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id, "events", [event], since="now", etag=None, polled_at="now"
    )
    thread_id = store.source_event(event.event_key)["thread_id"]
    linear = _spec(("A", "B", "C", "D"))
    raw = linear.canonical_document()
    raw["workflow_id"] = "reordered"
    raw["tasks"] = []
    for name, deps in (("B", []), ("C", ["B"]), ("A", ["C"])):
        raw["tasks"].append(
            {
                "id": name,
                "depends_on": deps,
                "planning": {"skill": f"{name}-planning", "tools": ["read_file"]},
                "execution": {"skill": f"{name}-execution", "tools": ["edit_file"]},
                "validation": {"skill": f"{name}-validation", "tools": ["read_file"]},
            }
        )
    runtime = WorkflowRuntime(store)
    cycle = runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=event.event_key,
        spec=parse_workflow_spec(raw),
    )
    assert runtime.select_active_task(cycle.workflow_cycle_id).task_id == "B"
    store.close()


def test_reopen_migrates_database_without_generic_tables(tmp_path):
    path = tmp_path / "legacy.db"
    store = SQLiteGitHubStore(path)
    store.upsert_repository(999, "example/legacy", "before-upgrade")
    for table in (
        "workflow_task_validations_v1",
        "workflow_task_executions_v1",
        "workflow_task_permits_v1",
        "workflow_task_plans_v1",
        "workflow_task_runs_v1",
        "workflow_cycles_v1",
    ):
        store.connection.execute(f"DROP TABLE {table}")
    store.connection.commit()
    store.close()

    migrated = SQLiteGitHubStore(path)
    names = {
        row[0]
        for row in migrated.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "workflow_cycles_v1" in names
    assert "workflow_task_runs_v1" in names
    assert migrated.repository_id_for_full_name("example/legacy") == 999
    migrated.close()


def test_lifecycle_gateways_capture_application_owned_evidence(runtime):
    store, engine, cycle = runtime
    task = engine.select_active_task(cycle.workflow_cycle_id)
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="capture evidence",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    engine.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key="approval-A",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )
    validation_runs = []
    tools = {
        item.name: item
        for item in build_lifecycle_tools(
            runtime=engine,
            workflow_cycle_id=cycle.workflow_cycle_id,
            publish_plan=lambda **_kwargs: (1, "now"),
            publish_result=lambda **_kwargs: (2, "2026-01-01T00:13:00Z"),
            execution_evidence=lambda: [
                {"command": "pytest", "exit_code": 0, "output": "passed"}
            ],
            validation_evidence=lambda: list(validation_runs),
        )
    }
    tools["finish_execution"].invoke(
        {"summary": "implemented", "evidence": {"reported": "done"}}
    )
    execution = store.connection.execute(
        "SELECT evidence_json FROM workflow_task_executions_v1"
    ).fetchone()
    execution_evidence = json.loads(execution["evidence_json"])
    assert execution_evidence["tool_observations"][0]["command"] == "pytest"

    with pytest.raises(ValueError, match="run_validation evidence is required"):
        tools["finish_validation"].invoke(
            {
                "verdict": "ACCEPT",
                "summary": "looks good",
                "findings": [],
                "repair_instructions": [],
                "evidence": {"reported": "passed"},
            }
        )
    validation_runs.append({"diff": "", "executions": []})
    with pytest.raises(KeyError, match="pregel_scratchpad"):
        tools["finish_validation"].invoke(
            {
                "verdict": "ACCEPT",
                "summary": "looks good",
                "findings": [],
                "repair_instructions": [],
                "evidence": {"reported": "passed"},
            }
        )
    validation = store.connection.execute(
        "SELECT evidence_json FROM workflow_task_validations_v1"
    ).fetchone()
    validation_evidence = json.loads(validation["evidence_json"])
    assert validation_evidence["validation_runs"] == [{"diff": "", "executions": []}]
