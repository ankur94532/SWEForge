from __future__ import annotations

import hashlib
import inspect
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

from langgraph.store.memory import InMemoryStore

from sweforge.github_models import (
    OriginSurface,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import (
    PublicationStatus,
    RepoMemoryCandidateRecord,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
    repo_memory_candidate_id_for,
)
from sweforge.issue_resolution import IssueResolutionCase
from sweforge.memory_learning import CuratorOutput
from sweforge.repo_memory import read_repo_memory, repo_memory_namespace
from sweforge.server import ServerConfig, SWEForgeServer
from sweforge.workflow_learning import WorkflowLearningService
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
    prompts = []
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
        self.prompts.append((cycle.cycle_id, task.task_id, task.phase.value, prompt))
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
            elif resume["kind"] == "RESULT_APPROVAL":
                self.runtime.approve_result(
                    task_run_id=task.task_run_id,
                    occurrence_key=resume["occurrence_key"],
                    approval_event_key=resume["event_key"],
                    approved_by=resume["approved_by"],
                    approval_is_authorized=resume["authorized"],
                    approval_occurred_at=resume["approved_at"],
                )
                self.events.append((task.task_id, "RESULT_APPROVED"))
            elif resume["kind"] == "RESULT_FEEDBACK":
                self.runtime.replan_from_result_feedback(
                    task_run_id=task.task_run_id,
                    event_key=resume["event_key"],
                    feedback=resume["feedback"],
                )
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
            validated = self.runtime.finish_validation(
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
            if verdict == ValidationVerdict.ACCEPT:
                self.runtime.publish_validated_result(
                    task_run_id=validated.task_run_id,
                    posted_comment_id=500 + len(self.events),
                    posted_at="2026-01-01T00:10:30Z",
                )
            self.events.append((task.task_id, verdict.value))


class ThreadedPlanningDriver(FakeDriver):
    def drive(self, *, cycle, task, prompt, resume=None):
        del cycle, prompt, resume
        assert task.phase == TaskPhase.PLANNING
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                self.runtime.submit_posted_plan,
                task_run_id=task.task_run_id,
                plan_text=f"Plan for {task.task_id} from a lifecycle tool thread",
                posted_comment_id=101,
                posted_at="2026-01-01T00:10:00Z",
            ).result()


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
    remote = path.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=path, check=True)


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


def _approve(server, source_id, minute):
    event = _event(
        SourceKind.ISSUE_COMMENT,
        source_id,
        "@agent approve",
        f"2026-01-01T00:{minute:02d}:00Z",
    )
    _record(server.config.db, "issue_comments", event)
    server._worker_entry("github:41:issue:9")


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
        "WAITING_FOR_PLAN_APPROVAL",
        "PENDING",
        "PENDING",
        "PENDING",
    ]
    store.close()
    task_ids = ("A", "B", "C", "D")
    for index, task_id in enumerate(task_ids, start=1):
        _approve(server, f"plan-approval-{index}", 10 + index * 2)
        store = SQLiteGitHubStore(server.config.db)
        cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
        assert cycle["active_task_id"] == task_id
        waiting = store.connection.execute(
            """SELECT task_id FROM workflow_task_runs_v1
               WHERE status='WAITING_FOR_RESULT_APPROVAL'"""
        ).fetchall()
        assert [row["task_id"] for row in waiting] == [task_id]
        store.close()
        _approve(server, f"result-approval-{index}", 11 + index * 2)
        if task_id != "D":
            store = SQLiteGitHubStore(server.config.db)
            cycle = store.connection.execute(
                "SELECT * FROM workflow_cycles_v1"
            ).fetchone()
            assert cycle["active_task_id"] == task_ids[index]
            store.close()
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


def test_server_accepts_lifecycle_gateway_from_langgraph_tool_thread(tmp_path):
    server = _server(tmp_path)
    server.driver_factory = lambda runtime, cycle_id, worktree, spec: (
        ThreadedPlanningDriver(runtime, cycle_id, worktree, spec)
    )
    root = _event(
        SourceKind.ISSUE,
        "root",
        "@agent plan from a tool thread",
        "2026-01-01T00:00:00Z",
    )
    _record(server.config.db, "issues", root)

    server._worker_entry("github:41:issue:9")

    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute(
        "SELECT status, phase FROM workflow_task_runs_v1"
    ).fetchone()
    assert (task["status"], task["phase"]) == (
        "WAITING_FOR_PLAN_APPROVAL",
        "WAITING_FOR_PLAN_APPROVAL",
    )
    assert store.dispatcher_failure("github:41:issue:9") is None
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
    _approve(server, "result-approval", 21)

    assert FakeDriver.events.count(("implementation", "EXECUTED")) == 2
    assert ("implementation", "NEEDS_FIXES") in FakeDriver.events
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    assert cycle["workflow_id"] == "default"
    assert cycle["status"] == "PUBLISHED"
    assert len(FakePublisher.calls) == 1
    store.close()


def test_auto_issue_completes_both_barriers_without_human_events(tmp_path):
    FakeDriver.events = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path)
    root = replace(
        _event(
            SourceKind.ISSUE,
            "auto-root",
            "@agent run automatically",
            "2026-01-01T00:00:00Z",
        ),
        issue_labels=("AUTO",),
    )
    _record(server.config.db, "issues", root)
    server._worker_entry("github:41:issue:9")

    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    permit = store.connection.execute(
        "SELECT * FROM workflow_task_permits_v1"
    ).fetchone()
    result_approval = store.connection.execute(
        "SELECT * FROM workflow_task_result_approvals_v1"
    ).fetchone()
    assert task["phase"] == "DONE"
    assert permit["approval_mode"] == "AUTO"
    assert result_approval["mode"] == "AUTO"
    assert result_approval["approval_event_key"] is None
    assert (
        store.connection.execute(
            "SELECT count(*) FROM source_events WHERE body='@agent approve'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.connection.execute("SELECT status FROM workflow_cycles_v1").fetchone()[0]
        == "PUBLISHED"
    )
    assert len(FakePublisher.calls) == 1
    generation = store.publication_generation(FakePublisher.calls[0])
    assert generation.cycle_ids == (1,)
    assert [
        item.task_id for item in store.accepted_lifecycle_material("github:41:issue:9")
    ] == ["implementation"]
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
    assert task["status"] == "WAITING_FOR_PLAN_APPROVAL"
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
    _approve(server, "result-approval", 26)
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
    store = SQLiteGitHubStore(server.config.db)
    assert (
        store.connection.execute("SELECT phase FROM workflow_task_runs_v1").fetchone()[
            0
        ]
        == "WAITING_FOR_RESULT_APPROVAL"
    )
    store.close()
    _approve(server, "result-approval", 21)
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
    assert task["phase"] == "WAITING_FOR_PLAN_APPROVAL"
    assert ("implementation", "CLARIFIED") in FakeDriver.events
    store.close()
    FakeDriver.clarify_once = False


def test_queued_deferred_followup_becomes_generic_revision_cycle(tmp_path):
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
    _approve(server, "result-approval", 21)

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
    assert cycles[1]["root_input_id"].startswith("revision-input-")
    assert cycles[1]["cycle_kind"] == "REVISION"
    assert cycles[1]["active_task_id"] == "revision"
    assert store.deferred_followup_by_id(deferred.deferred_id)["status"] == "CONSUMED"
    assert {
        row["residual_text"]
        for row in store.revision_inputs_for_cycle(cycles[1]["workflow_cycle_id"])
    } == {None, "update the documentation"}
    store.close()


def test_unsolicited_inputs_batch_before_first_publication_and_run_generic_revision(
    tmp_path, monkeypatch
):
    FakeDriver.events = []
    FakeDriver.specs = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path, max_ticks=1)
    thread_id = "github:41:issue:9"
    root = _event(
        SourceKind.ISSUE,
        "root",
        "@agent implement the original request",
        "2026-01-01T00:00:00Z",
    )
    _record(
        server.config.db,
        "issues",
        root,
    )
    server._worker_entry(thread_id)
    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "plan-ok",
            "@agent approve",
            "2026-01-01T00:20:00Z",
        ),
    )
    server._worker_entry(thread_id)

    first = _event(
        SourceKind.ISSUE_COMMENT,
        "steering-executing",
        "@agent preserve old configuration compatibility",
        "2026-01-01T00:21:00Z",
    )
    _record(server.config.db, "issue_comments", first)
    server._worker_entry(thread_id)
    second = _event(
        SourceKind.ISSUE_COMMENT,
        "steering-validating",
        "@agent keep the legacy error shape",
        "2026-01-01T00:22:00Z",
    )
    _record(server.config.db, "issue_comments", second)
    server._worker_entry(thread_id)

    store = SQLiteGitHubStore(server.config.db)
    pending_keys = [
        row["source_event_key"] for row in store.pending_revision_inputs(thread_id)
    ]
    assert pending_keys == [
        first.event_key,
        second.event_key,
    ]
    assert FakePublisher.calls == []
    store.close()
    _record(
        server.config.db,
        "issue_comments",
        _event(
            SourceKind.ISSUE_COMMENT,
            "result-ok",
            "@agent approve",
            "2026-01-01T00:23:00Z",
        ),
    )
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    cycles = store.connection.execute(
        "SELECT * FROM workflow_cycles_v1 ORDER BY cycle_id"
    ).fetchall()
    assert [(row["cycle_kind"], row["status"]) for row in cycles] == [
        ("INITIAL", "AWAITING_PUBLICATION"),
        ("REVISION", "ACTIVE"),
    ]
    revision_tasks = store.connection.execute(
        """SELECT * FROM workflow_task_runs_v1
           WHERE workflow_cycle_id=?""",
        (cycles[1]["workflow_cycle_id"],),
    ).fetchall()
    assert [row["task_id"] for row in revision_tasks] == ["revision"]
    batched = store.revision_inputs_for_cycle(cycles[1]["workflow_cycle_id"])
    assert [row["source_event_key"] for row in batched] == [
        first.event_key,
        second.event_key,
    ]
    assert FakePublisher.calls == []
    store.close()

    server._worker_entry(thread_id)  # revision planning
    _approve(server, "revision-plan-ok", 24)
    server._worker_entry(thread_id)  # revision execution
    server._worker_entry(thread_id)  # revision validation/result
    _approve(server, "revision-result-ok", 25)

    store = SQLiteGitHubStore(server.config.db)
    cycles = store.connection.execute(
        "SELECT * FROM workflow_cycles_v1 ORDER BY cycle_id"
    ).fetchall()
    assert [row["status"] for row in cycles] == ["PUBLISHED", "PUBLISHED"]
    assert [
        row["status"]
        for row in store.revision_inputs_for_cycle(cycles[1]["workflow_cycle_id"])
    ] == ["CONSUMED", "CONSUMED"]
    assert store.thread_workflow_lifecycle(thread_id)["initial_state"] == "PUBLISHED"
    assert len(FakePublisher.calls) == 1
    assert {workflow_id for workflow_id, _ in FakeDriver.specs} == {
        "default",
        f"revision-{server.workflow_spec.digest[:16]}",
    }
    publication = store.publication_for_id(FakePublisher.calls[0])
    generation = store.publication_generation(publication.publication_id)
    assert generation.cycle_ids == (1, 2)

    records = []
    for cycle, fact in zip(cycles, ("Initial fact.", "Revision fact.")):
        source_key = (
            root.event_key
            if cycle["cycle_kind"] == "INITIAL"
            else batched[0]["source_event_key"]
        )
        candidate_id = repo_memory_candidate_id_for(
            repo_id=41,
            thread_id=thread_id,
            cycle_id=cycle["cycle_id"],
            root_input_id=cycle["root_input_id"],
            fact=fact,
            evidence_path="README.md",
            evidence_start_line=1,
            evidence_end_line=1,
        )
        record = RepoMemoryCandidateRecord(
            candidate_id=candidate_id,
            repo_id=41,
            thread_id=thread_id,
            cycle_id=cycle["cycle_id"],
            root_input_id=cycle["root_input_id"],
            source_event_key=source_key,
            category="convention",
            fact=fact,
            durability_reason="README evidence",
            evidence_path="README.md",
            evidence_start_line=1,
            evidence_end_line=1,
            status=RepoMemoryCandidateStatus.PROPOSED.value,
            created_at="2026-01-01T01:00:00Z",
            updated_at="2026-01-01T01:00:00Z",
        )
        store.save_repo_memory_candidate(record)
        records.append(record)
    stale = RepoMemoryCandidateRecord(
        candidate_id=repo_memory_candidate_id_for(
            repo_id=41,
            thread_id=thread_id,
            cycle_id=1,
            root_input_id=cycles[0]["root_input_id"],
            fact="Stale line range.",
            evidence_path="README.md",
            evidence_start_line=99,
            evidence_end_line=99,
        ),
        repo_id=41,
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=cycles[0]["root_input_id"],
        source_event_key=root.event_key,
        category="convention",
        fact="Stale line range.",
        durability_reason="the cited line was removed",
        evidence_path="README.md",
        evidence_start_line=99,
        evidence_end_line=99,
        status="PROPOSED",
        created_at="2026-01-01T01:00:00Z",
        updated_at="2026-01-01T01:00:00Z",
    )
    store.save_repo_memory_candidate(stale)
    store.close()

    # Generation membership and pending proposals are restart-derived, not
    # process-local state.
    store = SQLiteGitHubStore(server.config.db)
    assert store.publication_generation(publication.publication_id).cycle_ids == (1, 2)
    captured = {}

    def curate_memory(**kwargs):
        captured["memory"] = kwargs
        return CuratorOutput(candidates=[], proposal_json="[]")

    def curate(*, model, evidence):
        del model
        captured["evidence"] = evidence
        return IssueResolutionCase(useful=False)

    monkeypatch.setattr(
        "sweforge.workflow_learning.curate_repository_memory", curate_memory
    )
    monkeypatch.setattr("sweforge.workflow_learning.curate_issue_resolution", curate)
    memory = InMemoryStore()
    learner = WorkflowLearningService(
        store=store,
        memory_store=memory,
        memory_model="offline-memory",
        resolution_model="offline-resolution",
        lock_root=tmp_path / "locks",
        clock=lambda: "2026-01-01T01:01:00Z",
    )
    assert learner.process_one(thread_id)
    learned = read_repo_memory(memory, repo_memory_namespace(41)) or ""
    assert "Initial fact." in learned
    assert "Revision fact." in learned
    lifecycle_text = captured["memory"]["lifecycle_text"]
    assert "Initial workflow (cycle 1)" in lifecycle_text
    assert "Accepted revision #1 (cycle 2)" in lifecycle_text
    assert "Executed implementation" in lifecycle_text
    assert "Validated revision: ACCEPT" in lifecycle_text
    assert [
        store.repo_memory_candidate(item.candidate_id).status for item in records
    ] == [
        "ACCEPTED",
        "ACCEPTED",
    ]
    assert store.repo_memory_candidate(stale.candidate_id).status == "REJECTED"
    assert learner.process_one(thread_id)
    evidence = captured["evidence"]
    assert "Initial workflow / implementation" in evidence.plan_text
    assert "Accepted revision #1 / revision" in evidence.plan_text
    assert "Executed implementation" in evidence.execution_response
    assert "Executed revision" in evidence.execution_response
    assert "preserve old configuration compatibility" in evidence.task_text
    assert "keep the legacy error shape" in evidence.task_text
    store.close()


def test_later_revision_prompt_uses_only_durable_accepted_history(tmp_path):
    FakeDriver.events = []
    FakeDriver.specs = []
    FakeDriver.prompts = []
    FakeDriver.verdicts = {
        "implementation": [ValidationVerdict.NEEDS_FIXES, ValidationVerdict.ACCEPT]
    }
    FakePublisher.calls = []
    server = _server(tmp_path)
    thread_id = "github:41:issue:9"
    root = _event(
        SourceKind.ISSUE,
        "root",
        "@agent implement the original behavior",
        "2026-01-01T00:00:00Z",
    )
    _record(
        server.config.db,
        "issues",
        root,
    )
    server._worker_entry(thread_id)
    _approve(server, "initial-plan", 12)
    _approve(server, "initial-result", 14)

    store = SQLiteGitHubStore(server.config.db)
    initial_task = store.connection.execute(
        """SELECT * FROM workflow_task_runs_v1
           WHERE cycle_id=1 AND task_id='implementation'"""
    ).fetchone()
    sentinel = "SUPERSEDED PLAN MUST NOT APPEAR"
    store.connection.execute(
        """INSERT INTO workflow_task_plans_v1(
           plan_id,task_run_id,workflow_cycle_id,task_id,version,plan_text,
           plan_digest,status,posted_at,posted_comment_id,
           approval_occurrence_key,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "superseded-plan-sentinel",
            initial_task["task_run_id"],
            initial_task["workflow_cycle_id"],
            initial_task["task_id"],
            0,
            sentinel,
            hashlib.sha256(sentinel.encode()).hexdigest(),
            "SUPERSEDED",
            "2026-01-01T00:09:00Z",
            99,
            "superseded-plan-occurrence",
            "2026-01-01T00:09:00Z",
        ),
    )
    store.connection.commit()
    store.update_publication(
        FakePublisher.calls[-1],
        status=PublicationStatus.NO_CHANGES,
        now="2026-01-01T00:15:00Z",
        remote_commit_sha="a" * 40,
    )
    store.close()

    first = _event(
        SourceKind.ISSUE_COMMENT,
        "revision-one",
        "@agent preserve legacy configuration behavior",
        "2026-01-01T00:20:00Z",
    )
    _record(server.config.db, "issue_comments", first)
    server._worker_entry(thread_id)
    first_prompt = next(
        prompt
        for cycle_id, task_id, phase, prompt in FakeDriver.prompts
        if cycle_id == 2 and task_id == "revision" and phase == "PLANNING"
    )
    assert "Original issue request (untrusted): implement the original behavior" in (
        first_prompt
    )
    assert "Initial workflow (cycle 1):" in first_prompt
    assert "Accepted plan" in first_prompt
    assert "Plan for implementation version next" in first_prompt
    assert "Final execution" in first_prompt
    assert "Executed implementation" in first_prompt
    assert "Final ACCEPT validation" in first_prompt
    assert "Validated implementation: ACCEPT" in first_prompt
    assert sentinel not in first_prompt
    assert "Validated implementation: NEEDS_FIXES" not in first_prompt
    assert "preserve legacy configuration behavior" in first_prompt

    _approve(server, "revision-one-plan", 21)
    _approve(server, "revision-one-result", 22)
    store = SQLiteGitHubStore(server.config.db)
    second_publication = store.publication_for_id(FakePublisher.calls[-1])
    second_generation = store.publication_generation(second_publication.publication_id)
    assert second_generation.cycle_ids == (2,)
    initial_cycle = store.connection.execute(
        "SELECT * FROM workflow_cycles_v1 WHERE cycle_id=1"
    ).fetchone()
    old_candidate = RepoMemoryCandidateRecord(
        candidate_id=repo_memory_candidate_id_for(
            repo_id=41,
            thread_id=thread_id,
            cycle_id=1,
            root_input_id=initial_cycle["root_input_id"],
            fact="Old published candidate.",
            evidence_path="README.md",
            evidence_start_line=1,
            evidence_end_line=1,
        ),
        repo_id=41,
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=initial_cycle["root_input_id"],
        source_event_key=root.event_key,
        category="convention",
        fact="Old published candidate.",
        durability_reason="old generation",
        evidence_path="README.md",
        evidence_start_line=1,
        evidence_end_line=1,
        status="PROPOSED",
        created_at="2026-01-01T00:23:00Z",
        updated_at="2026-01-01T00:23:00Z",
    )
    store.save_repo_memory_candidate(old_candidate)
    assert (
        store.repo_memory_candidates_for_generation(second_generation, repo_id=41) == []
    )
    store.close()

    store = SQLiteGitHubStore(server.config.db)
    assert store.publication_generation(
        second_publication.publication_id
    ).cycle_ids == (2,)
    store.close()
    second = _event(
        SourceKind.ISSUE_COMMENT,
        "revision-two",
        "@agent also support empty arrays",
        "2026-01-01T00:30:00Z",
    )
    _record(server.config.db, "issue_comments", second)
    server._worker_entry(thread_id)
    second_prompt = next(
        prompt
        for cycle_id, task_id, phase, prompt in FakeDriver.prompts
        if cycle_id == 3 and task_id == "revision" and phase == "PLANNING"
    )
    assert "Initial workflow (cycle 1):" in second_prompt
    assert "Accepted revision #1 (cycle 2):" in second_prompt
    assert "Executed revision" in second_prompt
    assert "Validated revision: ACCEPT" in second_prompt
    assert "also support empty arrays" in second_prompt
    assert "Accepted revision #2 (cycle 3)" not in second_prompt
    assert "Current revision repair/replan history" in second_prompt

    FakeDriver.verdicts["revision"] = [ValidationVerdict.ACCEPT]
    _approve(server, "revision-two-plan", 31)
    _approve(server, "revision-two-result", 32)
    store = SQLiteGitHubStore(server.config.db)
    third_generation = store.publication_generation(FakePublisher.calls[-1])
    assert third_generation.cycle_ids == (3,)
    # The immediately previous publication had no commit; the effective diff
    # baseline remains the latest finalized publication that did push one.
    assert third_generation.previous_commit_sha == "a" * 40
    store.close()


def test_mapped_pr_surfaces_batch_into_same_revision_and_cross_surface_approval_fails(
    tmp_path,
):
    FakeDriver.events = []
    FakeDriver.specs = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path, max_ticks=1)
    thread_id = "github:41:issue:9"
    root = _event(
        SourceKind.ISSUE,
        "root",
        "@agent establish the implementation",
        "2026-01-01T00:00:00Z",
    )
    _record(server.config.db, "issues", root)
    server._worker_entry(thread_id)
    _approve(server, "initial-plan", 20)
    server._worker_entry(thread_id)
    server._worker_entry(thread_id)
    _approve(server, "initial-result", 21)
    store = SQLiteGitHubStore(server.config.db)
    published = store.thread_workflow_lifecycle(thread_id)["initial_state"]
    store.close()
    assert published == "PUBLISHED"
    store = SQLiteGitHubStore(server.config.db)
    store.register_pr_mapping(41, 12, thread_id)
    store.close()

    conversation = replace(
        _event(
            SourceKind.ISSUE_COMMENT,
            "pr-conversation",
            "@agent preserve the old response",
            "2026-01-01T00:31:00Z",
        ),
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=12,
        origin_surface=OriginSurface.PR_CONVERSATION,
    )
    inline = replace(
        _event(
            SourceKind.REVIEW_COMMENT,
            "inline",
            "@agent apply this only to optional checks",
            "2026-01-01T00:32:00Z",
        ),
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=12,
        origin_surface=OriginSurface.PR_INLINE_REVIEW,
        path="src/checks.py",
        line=17,
        start_line=15,
        side="RIGHT",
        diff_hunk="@@ -15,3 +15,5 @@",
        commit_id="new",
        original_commit_id="old",
        review_thread_root_id="700",
    )
    _record(server.config.db, "issue_comments", conversation)
    _record(server.config.db, "review_comments", inline)
    server._worker_entry(thread_id)

    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute(
        """SELECT * FROM workflow_cycles_v1 WHERE cycle_kind='REVISION'
           ORDER BY cycle_id DESC LIMIT 1"""
    ).fetchone()
    assert cycle["active_task_id"] == "revision"
    assert (
        cycle["root_input_id"]
        == store.revision_inputs_for_cycle(cycle["workflow_cycle_id"])[0][
            "revision_input_id"
        ]
    )
    inputs = store.revision_inputs_for_cycle(cycle["workflow_cycle_id"])
    assert [row["origin_surface"] for row in inputs] == [
        "PR_CONVERSATION",
        "PR_INLINE_REVIEW",
    ]
    assert inputs[1]["path"] == "src/checks.py"
    assert inputs[1]["diff_hunk"] == "@@ -15,3 +15,5 @@"
    store.close()

    server._worker_entry(thread_id)  # publish the revision plan on PR conversation
    wrong_surface_approval = replace(
        inline,
        source_id="wrong-surface-approval",
        source_updated_at="2026-01-01T00:40:00Z",
        source_created_at="2026-01-01T00:40:00Z",
        body="@agent approve",
    )
    _record(server.config.db, "review_comments", wrong_surface_approval)
    server._worker_entry(thread_id)
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute(
        "SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?",
        (cycle["workflow_cycle_id"],),
    ).fetchone()
    assert task["phase"] == "WAITING_FOR_PLAN_APPROVAL"
    assert store.input_consumption(wrong_surface_approval.event_key).status == (
        "STALE_APPROVAL"
    )
    store.close()


def test_mid_revision_steering_replans_same_owner_without_consuming_later_input(
    tmp_path,
):
    FakeDriver.events = []
    FakeDriver.specs = []
    FakeDriver.verdicts = {}
    FakePublisher.calls = []
    server = _server(tmp_path, max_ticks=1)
    thread_id = "github:41:issue:9"
    _record(
        server.config.db,
        "issues",
        _event(
            SourceKind.ISSUE,
            "root",
            "@agent establish a base implementation",
            "2026-01-01T00:00:00Z",
        ),
    )
    server._worker_entry(thread_id)
    _approve(server, "initial-plan", 20)
    server._worker_entry(thread_id)
    server._worker_entry(thread_id)
    _approve(server, "initial-result", 21)

    first = _event(
        SourceKind.ISSUE_COMMENT,
        "revision-one",
        "@agent update the compatibility behavior",
        "2026-01-01T00:30:00Z",
    )
    _record(server.config.db, "issue_comments", first)
    server._worker_entry(thread_id)
    _approve(server, "revision-plan-v1", 31)

    later = _event(
        SourceKind.ISSUE_COMMENT,
        "revision-two",
        "@agent also cover empty arrays",
        "2026-01-01T00:32:00Z",
    )
    _record(server.config.db, "issue_comments", later)
    server._worker_entry(thread_id)  # finish the already-authorized execution
    store = SQLiteGitHubStore(server.config.db)
    revision = store.connection.execute(
        """SELECT * FROM workflow_cycles_v1 WHERE cycle_kind='REVISION'
           ORDER BY cycle_id DESC LIMIT 1"""
    ).fetchone()
    task = store.connection.execute(
        "SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?",
        (revision["workflow_cycle_id"],),
    ).fetchone()
    assert task["phase"] == "VALIDATING"
    pending_keys = [
        row["source_event_key"] for row in store.pending_revision_inputs(thread_id)
    ]
    assert pending_keys == [later.event_key]
    store.close()

    server._worker_entry(thread_id)  # validate, then replan at the safe boundary
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute(
        "SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?",
        (revision["workflow_cycle_id"],),
    ).fetchone()
    plans = store.connection.execute(
        "SELECT * FROM workflow_task_plans_v1 WHERE task_run_id=? ORDER BY version",
        (task["task_run_id"],),
    ).fetchall()
    assert task["phase"] == "PLANNING"
    assert [row["status"] for row in plans] == ["SUPERSEDED"]
    assert [
        row["source_event_key"]
        for row in store.revision_inputs_for_cycle(revision["workflow_cycle_id"])
    ] == [first.event_key, later.event_key]
    assert (
        store.revision_inputs_for_cycle(revision["workflow_cycle_id"])[1]["status"]
        == "BATCHED"
    )
    store.close()

    stale = _event(
        SourceKind.ISSUE_COMMENT,
        "stale-v1-approval",
        "@agent approve",
        "2026-01-01T00:33:00Z",
    )
    _record(server.config.db, "issue_comments", stale)
    server._worker_entry(thread_id)  # produce cumulative plan v2
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute(
        "SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?",
        (revision["workflow_cycle_id"],),
    ).fetchone()
    assert task["phase"] == "WAITING_FOR_PLAN_APPROVAL"
    assert store.input_consumption(stale.event_key).status == "STALE_APPROVAL"
    assert (
        len(
            store.connection.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE task_run_id=?",
                (task["task_run_id"],),
            ).fetchall()
        )
        == 2
    )
    store.close()
