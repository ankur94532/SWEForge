from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from sweforge.github_models import (
    InteractionMode,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_driver import DeepAgentWorkflowDriver
from sweforge.workflow_runtime import (
    TaskPhase,
    ValidationVerdict,
    WorkflowCycleKind,
    WorkflowRuntime,
)
from sweforge.workflow_spec import parse_workflow_spec
from sweforge.workspace import ThreadWorkspace, WorkspaceError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def event(
    repo: RepositoryRef,
    source_id: str,
    *,
    labels: tuple[str, ...] = (),
    body: str = "@agent implement it",
) -> SourceEvent:
    timestamp = (
        f"2026-01-01T00:{int(source_id[-1]) if source_id[-1].isdigit() else 0:02d}:00Z"
    )
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id=source_id,
        source_updated_at=timestamp,
        source_created_at=timestamp,
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="owner",
        body=body,
        html_url=None,
        issue_labels=labels,
    )


def record(store: SQLiteGitHubStore, source: SourceEvent) -> str:
    store.upsert_repository(
        source.repo_id, source.repo_full_name, source.source_updated_at
    )
    store.record_batch(
        source.repo_id,
        "issues",
        [source],
        since="now",
        etag=None,
        polled_at=source.source_updated_at,
    )
    return str(store.source_event(source.event_key)["thread_id"])


def spec(two_tasks: bool = False):
    def task(task_id: str, dependencies: list[str]):
        return {
            "id": task_id,
            "depends_on": dependencies,
            "planning": {"skill": f"plan-{task_id}", "tools": ["read_file"]},
            "execution": {"skill": f"execute-{task_id}", "tools": ["edit_file"]},
            "validation": {
                "skill": f"validate-{task_id}",
                "tools": ["run_validation"],
            },
        }

    tasks = [task("A", [])]
    if two_tasks:
        tasks.append(task("B", ["A"]))
    return parse_workflow_spec(
        {"version": 1, "workflow_id": "upgrade-test", "tasks": tasks}
    )


def runtime_for(tmp_path: Path, *, mode: InteractionMode, two_tasks: bool = False):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(77, "example/repo")
    root = event(
        repo, "root1", labels=(("AUTO",) if mode == InteractionMode.AUTO else ())
    )
    thread_id = record(store, root)
    runtime = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    cycle = runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=root.event_key,
        spec=spec(two_tasks),
    )
    return store, runtime, cycle


def execute_and_accept_validation(runtime: WorkflowRuntime, task_run_id: str):
    runtime.finish_execution(
        task_run_id,
        summary="implemented cumulatively",
        evidence={"tool_observations": [], "reported": {"tests": "passed"}},
    )
    runtime.finish_validation(
        task_run_id=task_run_id,
        verdict=ValidationVerdict.ACCEPT,
        summary="all checks passed",
        findings=[],
        repair_instructions=[],
        evidence={"validation_runs": [{"tests": "passed"}], "reported": {}},
    )


def test_interaction_mode_is_captured_once_and_survives_restart_and_cycles(tmp_path):
    db = tmp_path / "state.db"
    store = SQLiteGitHubStore(db)
    repo = RepositoryRef(1, "example/auto")
    root = event(repo, "root1", labels=("AUTO",))
    thread_id = record(store, root)
    assert store.interaction_mode(thread_id) == InteractionMode.AUTO

    later = event(repo, "root2", labels=(), body="@agent follow up")
    record(store, later)
    runtime = WorkflowRuntime(store)
    runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=root.event_key,
        spec=spec(),
    )
    store.connection.execute(
        "UPDATE workflow_cycles_v1 SET status='PUBLISHED' WHERE thread_id=?",
        (thread_id,),
    )
    store.connection.execute(
        """UPDATE thread_workflow_lifecycle_v1
           SET initial_state='PUBLISHED' WHERE thread_id=?""",
        (thread_id,),
    )
    store.connection.commit()
    runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=2,
        root_input_id=later.event_key,
        spec=spec(),
        cycle_kind=WorkflowCycleKind.REVISION,
        revision_sequence=1,
    )
    store.close()

    reopened = SQLiteGitHubStore(db)
    assert reopened.interaction_mode(thread_id) == InteractionMode.AUTO
    assert (
        reopened.connection.execute(
            "SELECT count(*) FROM workflow_cycles_v1 WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        == 2
    )
    reopened.close()


def test_manual_mode_ignores_a_later_auto_label(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(2, "example/manual")
    thread_id = record(store, event(repo, "root1"))
    record(store, event(repo, "root2", labels=("AUTO",), body="@agent more"))
    assert store.interaction_mode(thread_id) == InteractionMode.MANUAL
    store.close()


def test_existing_database_threads_migrate_to_manual(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE repositories(
            repo_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL);
        CREATE TABLE issue_threads(
            thread_id TEXT PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
            repo_full_name TEXT NOT NULL, issue_number INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(repo_id, issue_number));
        INSERT INTO repositories VALUES(1,'example/legacy','now');
        INSERT INTO issue_threads
          VALUES('github:1:issue:7',1,'example/legacy',7,'now','now');
        """
    )
    connection.commit()
    connection.close()
    store = SQLiteGitHubStore(db)
    assert store.interaction_mode("github:1:issue:7") == InteractionMode.MANUAL
    store.close()


def remote_repository(tmp_path: Path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "clone", "-q", remote, source], check=True)
    git(source, "switch", "-q", "-c", "main")
    (source / "README.md").write_text("one\n")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "one",
    )
    git(source, "push", "-qu", "origin", "main")
    subprocess.run(["git", "clone", "-q", "-b", "main", remote, writer], check=True)
    return remote, source, writer


def advance_remote(writer: Path, text: str) -> str:
    (writer / "README.md").write_text(text + "\n")
    git(writer, "add", "README.md")
    git(
        writer,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        text,
    )
    git(writer, "push", "-q", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


def test_new_workspaces_pin_fetched_origin_main_without_moving_source(tmp_path):
    _, source, writer = remote_repository(tmp_path)
    source_head = git(source, "rev-parse", "HEAD")
    second = advance_remote(writer, "two")
    first_workspace = ThreadWorkspace.create(
        repository=source,
        workspace_root=tmp_path / "workspaces",
        repo_id=1,
        issue_number=7,
        lock_root=tmp_path / "locks",
        fetch_remote_main=True,
    )
    assert first_workspace.base_commit == second
    assert git(source, "rev-parse", "HEAD") == source_head
    assert (first_workspace.path / "README.md").read_text() == "two\n"

    third = advance_remote(writer, "three")
    reopened = ThreadWorkspace.create(
        repository=source,
        workspace_root=tmp_path / "workspaces",
        repo_id=1,
        issue_number=7,
        existing_path=str(first_workspace.path),
        expected_branch=first_workspace.branch_name,
        expected_base=first_workspace.base_commit,
        lock_root=tmp_path / "locks",
        fetch_remote_main=False,
    )
    second_workspace = ThreadWorkspace.create(
        repository=source,
        workspace_root=tmp_path / "workspaces",
        repo_id=1,
        issue_number=8,
        lock_root=tmp_path / "locks",
        fetch_remote_main=True,
    )
    assert reopened.base_commit == second
    assert second_workspace.base_commit == third
    assert git(source, "rev-parse", "HEAD") == source_head


def test_remote_fetch_failure_creates_no_workspace_or_issue_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    with pytest.raises(WorkspaceError, match="fetch origin main failed"):
        ThreadWorkspace.create(
            repository=repo,
            workspace_root=tmp_path / "workspaces",
            repo_id=1,
            issue_number=7,
            lock_root=tmp_path / "locks",
            fetch_remote_main=True,
        )
    assert not (tmp_path / "workspaces" / "1" / "issue-7").exists()
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/sweforge/issue-7"],
            cwd=repo,
        ).returncode
        != 0
    )


def test_manual_accept_waits_for_exact_result_and_feedback_replans_same_task(tmp_path):
    store, runtime, cycle = runtime_for(
        tmp_path, mode=InteractionMode.MANUAL, two_tasks=True
    )
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    plan = runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="preserve foo behavior and tests",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    with pytest.raises(PermissionError, match="AUTO plan authorization"):
        runtime.auto_authorize_plan(task.task_run_id)
    permit = runtime.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key="plan-event",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )
    execute_and_accept_validation(runtime, task.task_run_id)
    result = runtime.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=2,
        posted_at="2026-01-01T00:13:00Z",
    )
    assert runtime.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL
    assert runtime.select_active_task(cycle.workflow_cycle_id).task_id == "A"
    assert (
        next(
            item
            for item in runtime.task_runs(cycle.workflow_cycle_id)
            if item.task_id == "B"
        ).phase
        == TaskPhase.PENDING
    )
    assert not runtime.publication_is_eligible(cycle.workflow_cycle_id)
    with pytest.raises(PermissionError, match="AUTO result approval"):
        runtime.auto_accept_result(task.task_run_id)
    with pytest.raises(PermissionError, match="permission"):
        runtime.approve_result(
            task_run_id=task.task_run_id,
            occurrence_key=result.result_occurrence_key,
            approval_event_key="unauthorized",
            approved_by="reader",
            approval_is_authorized=False,
            approval_occurred_at="2026-01-01T00:14:00Z",
        )

    replanning = runtime.replan_from_result_feedback(
        task_run_id=task.task_run_id,
        event_key="feedback-event",
        feedback="also handle null values",
    )
    assert replanning.phase == TaskPhase.PLANNING
    assert runtime.cycle(cycle.workflow_cycle_id).active_task_id == "A"
    assert runtime.permit(permit.permit_id).invalidated_at is not None
    assert "preserve foo behavior" in str(replanning.repair_feedback)
    assert "also handle null values" in str(replanning.repair_feedback)
    revised = runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="preserve foo behavior and tests; add null handling and coverage",
        posted_comment_id=3,
        posted_at="2026-01-01T00:15:00Z",
    )
    assert revised.version == 2
    with pytest.raises(ValueError, match="expected workflow phase|stale|does not own"):
        runtime.approve_result(
            task_run_id=task.task_run_id,
            occurrence_key=result.result_occurrence_key,
            approval_event_key="stale-result-event",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:16:00Z",
        )
    runtime.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=revised.approval_occurrence_key,
        approval_event_key="plan-v2-event",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:16:00Z",
    )
    execute_and_accept_validation(runtime, task.task_run_id)
    revised_result = runtime.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=4,
        posted_at="2026-01-01T00:17:00Z",
    )
    with pytest.raises(ValueError, match="stale"):
        runtime.approve_result(
            task_run_id=task.task_run_id,
            occurrence_key=result.result_occurrence_key,
            approval_event_key="stale-result-event",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:18:00Z",
        )
    runtime.approve_result(
        task_run_id=task.task_run_id,
        occurrence_key=revised_result.result_occurrence_key,
        approval_event_key="result-v2-event",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:18:00Z",
    )
    assert runtime.task(task.task_run_id).phase == TaskPhase.DONE
    assert runtime.select_active_task(cycle.workflow_cycle_id).task_id == "B"
    store.close()


def test_auto_records_both_authorities_and_selects_next_task_without_human_input(
    tmp_path,
):
    store, runtime, cycle = runtime_for(
        tmp_path, mode=InteractionMode.AUTO, two_tasks=True
    )
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="automatic exact plan",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    current_plan = runtime.plan(runtime.task(task.task_run_id).current_plan_id or "")
    with pytest.raises(PermissionError, match="MANUAL mode"):
        runtime.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=current_plan.approval_occurrence_key,
            approval_event_key="fabricated-human-event",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:12:00Z",
        )
    permit = runtime.auto_authorize_plan(task.task_run_id)
    assert permit.approval_mode == "AUTO"
    assert permit.approval_event_key.startswith("auto-plan-authority:plan-approval:")
    execute_and_accept_validation(runtime, task.task_run_id)
    runtime.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=2,
        posted_at="2026-01-01T00:13:00Z",
    )
    first_result = runtime.current_result(task.task_run_id)
    assert first_result is not None
    with pytest.raises(PermissionError, match="invalid for AUTO mode"):
        runtime.approve_result(
            task_run_id=task.task_run_id,
            occurrence_key=first_result.result_occurrence_key,
            approval_event_key="fabricated-human-event",
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:14:00Z",
        )
    approval = runtime.auto_accept_result(task.task_run_id)
    assert approval.mode == "AUTO"
    assert approval.approval_event_key is None
    assert runtime.task(task.task_run_id).phase == TaskPhase.DONE
    second = runtime.select_active_task(cycle.workflow_cycle_id)
    assert second.task_id == "B"
    runtime.submit_posted_plan(
        task_run_id=second.task_run_id,
        plan_text="automatic exact plan B",
        posted_comment_id=3,
        posted_at="2026-01-01T00:14:00Z",
    )
    runtime.auto_authorize_plan(second.task_run_id)
    execute_and_accept_validation(runtime, second.task_run_id)
    runtime.publish_validated_result(
        task_run_id=second.task_run_id,
        posted_comment_id=4,
        posted_at="2026-01-01T00:15:00Z",
    )
    runtime.auto_accept_result(second.task_run_id)
    assert runtime.select_active_task(cycle.workflow_cycle_id) is None
    assert runtime.publication_is_eligible(cycle.workflow_cycle_id)
    assert (
        store.connection.execute(
            "SELECT count(*) FROM source_events WHERE body='@agent approve'"
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_auto_replan_keeps_owner_and_auto_authorizes_the_new_plan(tmp_path):
    store, runtime, cycle = runtime_for(tmp_path, mode=InteractionMode.AUTO)
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="automatic plan v1",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    first_permit = runtime.auto_authorize_plan(task.task_run_id)
    runtime.finish_execution(
        task.task_run_id,
        summary="scope needs revision",
        evidence={"tool_observations": [], "reported": {}},
    )
    replanning = runtime.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.REPLAN,
        summary="revise the cumulative plan",
        findings=[{"scope": "changed"}],
        repair_instructions=["preserve prior work and add the new scope"],
        evidence={"validation_runs": [{"tests": "passed"}], "reported": {}},
    )
    assert replanning.phase == TaskPhase.PLANNING
    assert runtime.cycle(cycle.workflow_cycle_id).active_task_id == task.task_id
    assert runtime.permit(first_permit.permit_id).invalidated_at is not None

    revised = runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="automatic cumulative plan v2",
        posted_comment_id=2,
        posted_at="2026-01-01T00:12:00Z",
    )
    second_permit = runtime.auto_authorize_plan(task.task_run_id)
    assert revised.version == 2
    assert second_permit.approval_mode == "AUTO"
    assert runtime.task(task.task_run_id).phase == TaskPhase.EXECUTING
    store.close()


def test_blocked_validation_fails_closed_without_releasing_owner(tmp_path):
    store, runtime, cycle = runtime_for(tmp_path, mode=InteractionMode.MANUAL)
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    plan = runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="manual plan",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    runtime.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key="plan-event",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )
    runtime.finish_execution(
        task.task_run_id,
        summary="attempted implementation",
        evidence={"tool_observations": [], "reported": {}},
    )
    failed = runtime.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.BLOCKED,
        summary="required dependency is unavailable",
        findings=[{"blocker": "dependency"}],
        repair_instructions=[],
        evidence={"validation_runs": [{"tests": "blocked"}], "reported": {}},
    )
    assert failed.phase == TaskPhase.FAILED
    assert runtime.cycle(cycle.workflow_cycle_id).status.value == "FAILED"
    assert runtime.cycle(cycle.workflow_cycle_id).active_task_id == task.task_id
    assert not runtime.publication_is_eligible(cycle.workflow_cycle_id)
    store.close()


class CommentClient:
    def __init__(self):
        self.items: list[dict] = []

    def repository(self, full_name):
        return RepositoryRef(77, full_name)

    def comments(self, repo, number):
        return list(self.items)

    def create_comment(self, repo, number, body):
        item = {
            "id": len(self.items) + 1,
            "body": body,
            "created_at": "2026-01-01T00:13:00Z",
        }
        self.items.append(item)
        return item


def test_validated_result_comment_reconciles_by_exact_result_identity(tmp_path):
    store, runtime, cycle = runtime_for(tmp_path, mode=InteractionMode.MANUAL)
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    plan = runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="plan",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    runtime.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key="plan-event",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )
    execute_and_accept_validation(runtime, task.task_run_id)
    client = CommentClient()
    driver = DeepAgentWorkflowDriver(
        runtime=runtime,
        workflow_cycle_id=cycle.workflow_cycle_id,
        spec=spec(),
        store=store,
        client=client,
        worktree=tmp_path,
        planning_model="unused",
        execution_model="unused",
        validation_model="unused",
        checkpointer=None,
        memory_store=None,
        capability_registry=None,
        sandbox_backend_provider=None,
        secure_execution=False,
        unsafe_local_shell=True,
    )
    first = driver._publish_result(
        cycle, task_run_id=task.task_run_id, task_id=task.task_id
    )
    second = driver._publish_result(
        cycle, task_run_id=task.task_run_id, task_id=task.task_id
    )
    assert first == second
    assert len(client.items) == 1
    assert "Verdict: ACCEPT" in client.items[0]["body"]
    assert "@agent approve" in client.items[0]["body"]
    store.close()
