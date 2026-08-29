from __future__ import annotations

import inspect
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

from sweforge.github_models import (
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import PublicationStatus, SQLiteGitHubStore
from sweforge.server import ServerConfig, SWEForgeServer
from sweforge.workflow_runtime import TaskPhase, ValidationVerdict


class FakeClient:
    def repository(self, full_name):
        return RepositoryRef(41, full_name)

    def collaborator_permission(self, repo, login):
        return "write"

    def close(self):
        pass


def test_server_production_entry_has_no_legacy_workflow_driver():
    worker = inspect.getsource(SWEForgeServer._worker_entry)
    drain = inspect.getsource(SWEForgeServer._drain_workflow)
    assert "DeclarativeWorkflowController" in worker
    assert "WorkflowEngine" not in worker + drain
    assert "controller.advance" in drain


class FakeLearning:
    def __init__(self, **_kwargs):
        pass

    def process_one(self, _thread_id):
        return False


class FakePublisher:
    calls = []

    def __init__(self, *, store, **_kwargs):
        self.store = store

    def publish_one(self, publication_id):
        record = self.store.next_publication(publication_id)
        assert record is not None
        self.calls.append(publication_id)
        self.store.update_publication(
            publication_id,
            status=PublicationStatus.NO_CHANGES,
            now="2026-01-01T00:59:00Z",
        )
        return type("Result", (), {"status": "NO_CHANGES"})()


class FakeDriver:
    events = []
    specs = []
    verdicts = {}
    clarify_once = False
    clarified = False

    def __init__(self, runtime, _workflow_cycle_id, _worktree, spec):
        self.runtime = runtime
        self.specs.append((spec.workflow_id, spec.digest))

    def has_pending_interrupt(self, **_kwargs):
        return True

    def reconcile_interrupts(self, **_kwargs):
        return None

    def drive(self, *, cycle, task, prompt, resume=None):
        if resume is not None:
            if resume["kind"] == "PLAN_APPROVAL":
                self.runtime.approve_plan(
                    task_run_id=task.task_run_id,
                    occurrence_key=resume["occurrence_key"],
                    approval_event_key=resume["event_key"],
                    approved_by=resume["approved_by"],
                    approval_is_authorized=resume["authorized"],
                    approval_occurred_at=resume["approved_at"],
                )
                self.events.append((task.task_id, "APPROVED"))
            elif resume["kind"] == "PLAN_FEEDBACK":
                self.runtime.replan_from_feedback(task.task_run_id)
            elif resume["kind"] == "CLARIFICATION_RESPONSE":
                self.runtime.resume_clarification(
                    task_run_id=task.task_run_id,
                    occurrence_key=resume["occurrence_key"],
                )
                self.events.append((task.task_id, "CLARIFIED"))
            return
        if task.phase == TaskPhase.PLANNING:
            if self.clarify_once and not self.clarified:
                self.runtime.pause_for_clarification(
                    task_run_id=task.task_run_id,
                    occurrence_key=f"clarification:{task.task_run_id}:one",
                )
                type(self).clarified = True
                self.events.append((task.task_id, "CLARIFICATION"))
                return
            self.runtime.submit_posted_plan(
                task_run_id=task.task_run_id,
                plan_text=f"Plan for {task.task_id} version next",
                posted_comment_id=100 + len(self.events),
                posted_at="2026-01-01T00:10:00Z",
            )
            self.events.append((task.task_id, "PLANNED"))
        elif task.phase == TaskPhase.EXECUTING:
            self.runtime.finish_execution(
                task.task_run_id,
                summary=f"Executed {task.task_id}",
                evidence={
                    "reported": {"command": "tests", "exit_code": 0},
                    "tool_observations": [
                        {"command": "tests", "exit_code": 0, "output": "passed"}
                    ],
                },
            )
            self.events.append((task.task_id, "EXECUTED"))
        elif task.phase == TaskPhase.VALIDATING:
            queue = self.verdicts.setdefault(task.task_id, [ValidationVerdict.ACCEPT])
            verdict = queue.pop(0)
            self.runtime.finish_validation(
                task_run_id=task.task_run_id,
                verdict=verdict,
                summary=f"Validated {task.task_id}: {verdict.value}",
                findings=[],
                repair_instructions=(
                    ["repair the same task"]
                    if verdict != ValidationVerdict.ACCEPT
                    else []
                ),
                evidence={
                    "reported": {"tests": "passed"},
                    "validation_runs": [{"diff": "", "executions": []}],
                },
            )
            self.events.append((task.task_id, verdict.value))


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=path,
        check=True,
    )


def _event(kind, source_id, body, created_at):
    return SourceEvent(
        repo_id=41,
        repo_full_name="owner/repo",
        source_kind=kind,
        source_id=source_id,
        source_updated_at=created_at,
        source_created_at=created_at,
        subject_kind=SubjectKind.ISSUE,
        subject_number=9,
        author_login="maintainer",
        body=body,
        html_url=None,
    )


def _record(db, stream, event):
    store = SQLiteGitHubStore(db)
    store.upsert_repository(41, "owner/repo", event.source_updated_at)
    store.record_batch(
        41,
        stream,
        [event],
        since="2025-12-31T00:00:00Z",
        etag=None,
        polled_at=event.source_updated_at,
    )
    store.close()


def _server(tmp_path, spec_path=None, *, max_ticks=30):
    source = tmp_path / "source"
    _git_repo(source)
    config = ServerConfig(
        repositories=("owner/repo",),
        repo_paths={"owner/repo": source},
        db=tmp_path / "state.db",
        checkpoints=tmp_path / "checkpoints.db",
        memory_db=tmp_path / "memory.db",
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        workflow_spec=spec_path,
        model="offline-model",
        unsafe_local_shell=True,
        max_ticks=max_ticks,
    )
    client = FakeClient()
    return SWEForgeServer(
        config,
        client_factory=lambda _config: (client, client),
        driver_factory=lambda runtime, cycle_id, worktree, spec: FakeDriver(
            runtime, cycle_id, worktree, spec
        ),
        publisher_factory=FakePublisher,
        learning_factory=FakeLearning,
        now=lambda: datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    )


def _diamond_spec(path):
    tasks = []
    dependencies = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    for task_id in ("A", "B", "C", "D"):
        tasks.append(
            f"""  - id: {task_id}
    depends_on: {dependencies[task_id]}
    planning: {{skill: plan-{task_id}, tools: [read_file]}}
    execution: {{skill: execute-{task_id}, tools: [edit_file]}}
    validation: {{skill: validate-{task_id}, tools: [run_validation]}}
"""
        )
    path.write_text("version: 1\nworkflow_id: diamond\ntasks:\n" + "".join(tasks))


def test_server_drives_custom_diamond_serially_and_publishes_once(tmp_path):
    FakeDriver.events = []
    FakeDriver.specs = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    spec_path = tmp_path / "workflow.yaml"
    _diamond_spec(spec_path)
    server = _server(tmp_path, spec_path)
    root = _event(
        SourceKind.ISSUE, "root", "@agent implement diamond", "2026-01-01T00:00:00Z"
    )
    _record(server.config.db, "issues", root)
    thread_id = "github:41:issue:9"

    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    tasks = store.connection.execute(
        "SELECT * FROM workflow_task_runs_v1 ORDER BY declaration_index"
    ).fetchall()
    assert cycle["workflow_id"] == "diamond"
    assert cycle["active_task_id"] == "A"
    assert [row["status"] for row in tasks] == [
        "WAITING_FOR_APPROVAL",
        "PENDING",
        "PENDING",
        "PENDING",
    ]
    store.close()

    for index, expected_wait in enumerate(("B", "C", "D"), start=1):
        approval = _event(
            SourceKind.ISSUE_COMMENT,
            f"approval-{index}",
            "@agent approve",
            f"2026-01-01T00:{10 + index + 1:02d}:00Z",
        )
        _record(server.config.db, "issue_comments", approval)
        server._worker_entry(thread_id)
        store = SQLiteGitHubStore(server.config.db)
        cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
        assert cycle["active_task_id"] == expected_wait
        waiting = store.connection.execute(
            """SELECT task_id FROM workflow_task_runs_v1
               WHERE status='WAITING_FOR_APPROVAL'"""
        ).fetchall()
        assert [row["task_id"] for row in waiting] == [expected_wait]
        store.close()

    final_approval = _event(
        SourceKind.ISSUE_COMMENT,
        "approval-4",
        "@agent approve",
        "2026-01-01T00:20:00Z",
    )
    _record(server.config.db, "issue_comments", final_approval)
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    assert cycle["status"] == "PUBLISHED"
    assert cycle["active_task_id"] is None
    assert [
        row["status"]
        for row in store.connection.execute(
            "SELECT status FROM workflow_task_runs_v1 ORDER BY declaration_index"
        )
    ] == ["DONE", "DONE", "DONE", "DONE"]
    assert len(FakePublisher.calls) == 1
    assert (
        store.connection.execute(
            "SELECT count(*) FROM logical_publications"
        ).fetchone()[0]
        == 1
    )
    assert store.pending_memory_learning(thread_id) is not None
    assert store.pending_issue_resolution(thread_id) is not None
    assert [item for item in FakeDriver.events if item[1] == "PLANNED"] == [
        ("A", "PLANNED"),
        ("B", "PLANNED"),
        ("C", "PLANNED"),
        ("D", "PLANNED"),
    ]
    assert {workflow_id for workflow_id, _digest in FakeDriver.specs} == {"diamond"}
    store.close()


def test_default_server_runtime_repair_loop_keeps_same_task(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {
        "implementation": [ValidationVerdict.NEEDS_FIXES, ValidationVerdict.ACCEPT]
    }
    FakePublisher.calls = []
    server = _server(tmp_path)
    root = _event(
        SourceKind.ISSUE, "root", "@agent repair safely", "2026-01-01T00:00:00Z"
    )
    _record(server.config.db, "issues", root)
    thread_id = "github:41:issue:9"
    server._worker_entry(thread_id)
    approval = replace(
        _event(
            SourceKind.ISSUE_COMMENT,
            "approval",
            "@agent approve",
            "2026-01-01T00:20:00Z",
        )
    )
    _record(server.config.db, "issue_comments", approval)
    server._worker_entry(thread_id)

    assert FakeDriver.events.count(("implementation", "EXECUTED")) == 2
    assert ("implementation", "NEEDS_FIXES") in FakeDriver.events
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    assert cycle["workflow_id"] == "default"
    assert cycle["status"] == "PUBLISHED"
    assert len(FakePublisher.calls) == 1
    store.close()


def test_validation_replan_requires_new_same_task_approval(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {
        "implementation": [ValidationVerdict.REPLAN, ValidationVerdict.ACCEPT]
    }
    FakePublisher.calls = []
    server = _server(tmp_path)
    _record(
        server.config.db,
        "issues",
        _event(
            SourceKind.ISSUE,
            "root",
            "@agent revise scope safely",
            "2026-01-01T00:00:00Z",
        ),
    )
    thread_id = "github:41:issue:9"
    server._worker_entry(thread_id)
    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "approval-1",
            "@agent approve",
            "2026-01-01T00:20:00Z",
        ),
    )
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    plans = store.connection.execute(
        "SELECT * FROM workflow_task_plans_v1 ORDER BY version"
    ).fetchall()
    permits = store.connection.execute(
        "SELECT * FROM workflow_task_permits_v1 ORDER BY created_at"
    ).fetchall()
    assert cycle["active_task_id"] == "implementation"
    assert task["status"] == "WAITING_FOR_APPROVAL"
    assert [row["status"] for row in plans] == ["SUPERSEDED", "POSTED"]
    assert permits[0]["invalidated_at"] is not None
    store.close()

    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "approval-2",
            "@agent approve",
            "2026-01-01T00:25:00Z",
        ),
    )
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    assert (
        store.connection.execute("SELECT status FROM workflow_cycles_v1").fetchone()[0]
        == "PUBLISHED"
    )
    assert len(FakePublisher.calls) == 1
    store.close()


def test_server_restarts_recover_executing_and_validating_owner(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path, max_ticks=1)
    _record(
        server.config.db,
        "issues",
        _event(
            SourceKind.ISSUE,
            "root",
            "@agent survive restarts",
            "2026-01-01T00:00:00Z",
        ),
    )
    thread_id = "github:41:issue:9"
    server._worker_entry(thread_id)
    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "approval",
            "@agent approve",
            "2026-01-01T00:20:00Z",
        ),
    )

    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    executing = store.connection.execute(
        "SELECT task_run_id,task_id,phase FROM workflow_task_runs_v1"
    ).fetchone()
    assert executing["phase"] == "EXECUTING"
    store.close()

    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    validating = store.connection.execute(
        "SELECT task_run_id,task_id,phase FROM workflow_task_runs_v1"
    ).fetchone()
    assert validating["phase"] == "VALIDATING"
    assert validating["task_run_id"] == executing["task_run_id"]
    assert validating["task_id"] == executing["task_id"] == "implementation"
    store.close()

    server._worker_entry(thread_id)
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    assert (
        store.connection.execute("SELECT status FROM workflow_cycles_v1").fetchone()[0]
        == "PUBLISHED"
    )
    assert len(FakePublisher.calls) == 1
    store.close()


def test_clarification_keeps_owner_and_rejects_approval_as_answer(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {}
    FakeDriver.clarify_once = True
    FakeDriver.clarified = False
    FakePublisher.calls = []
    server = _server(tmp_path)
    _record(
        server.config.db,
        "issues",
        _event(
            SourceKind.ISSUE,
            "root",
            "@agent ask if needed",
            "2026-01-01T00:00:00Z",
        ),
    )
    thread_id = "github:41:issue:9"
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    assert task["task_id"] == "implementation"
    assert task["phase"] == "WAITING_FOR_INPUT"
    store.close()

    stale = _event(
        SourceKind.ISSUE_COMMENT,
        "stale-approval",
        "@agent approve",
        "2026-01-01T00:40:00Z",
    )
    _record(server.config.db, "issue_comments", stale)
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    assert task["phase"] == "WAITING_FOR_INPUT"
    assert store.input_consumption(stale.event_key).status == "STALE_APPROVAL"
    store.close()

    answer = _event(
        SourceKind.ISSUE_COMMENT,
        "answer",
        "@agent use the existing API",
        "2026-01-01T00:41:00Z",
    )
    _record(server.config.db, "issue_comments", answer)
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    assert cycle["active_task_id"] == "implementation"
    assert task["phase"] == "WAITING_FOR_APPROVAL"
    assert ("implementation", "CLARIFIED") in FakeDriver.events
    store.close()
    FakeDriver.clarify_once = False


def test_queued_deferred_followup_becomes_next_declarative_cycle(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path)
    thread_id = "github:41:issue:9"
    _record(
        server.config.db,
        "issues",
        _event(
            SourceKind.ISSUE,
            "root",
            "@agent finish the first cycle",
            "2026-01-01T00:00:00Z",
        ),
    )
    server._worker_entry(thread_id)
    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "approval",
            "@agent approve",
            "2026-01-01T00:20:00Z",
        ),
    )
    server._worker_entry(thread_id)

    followup = _event(
        SourceKind.ISSUE_COMMENT,
        "followup",
        "@agent also update the documentation",
        "2026-01-01T00:30:00Z",
    )
    _record(server.config.db, "issue_comments", followup)
    store = SQLiteGitHubStore(server.config.db)
    deferred = store.defer_followup(
        source_event_key=followup.event_key,
        thread_id=thread_id,
        originating_cycle_id=1,
        queued_at="2026-01-01T00:31:00Z",
        residual_text="update the documentation",
        disposition_status="DEFERRED",
    )
    store.close()

    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    cycles = store.connection.execute(
        "SELECT * FROM workflow_cycles_v1 ORDER BY cycle_id"
    ).fetchall()
    assert [row["status"] for row in cycles] == ["PUBLISHED", "ACTIVE"]
    assert cycles[1]["root_input_id"] == deferred.deferred_id
    assert cycles[1]["active_task_id"] == "implementation"
    assert store.deferred_followup_by_id(deferred.deferred_id)["status"] == "CONSUMED"
    store.close()
