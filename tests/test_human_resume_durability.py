"""Crash-safe application of human lifecycle inputs.

A human action is claimed against one exact occurrence before the transition is
attempted, and only recorded as consumed once the transition provably landed.
A worker that dies in between must cost the user nothing.
"""

from datetime import UTC, datetime

import pytest

from sweforge.github_models import SourceKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.server import ServerConfig, SWEForgeServer
from tests.test_declarative_server_integration import (
    FakeClient,
    FakeDriver,
    FakeLearning,
    FakePublisher,
    _event,
    _git_repo,
    _record,
)

THREAD = "github:41:issue:9"


class ReadOnlyClient(FakeClient):
    def collaborator_permission(self, repo, login):
        return "read"


class CrashBeforeApply(FakeDriver):
    """Dies after the controller claimed the input, before the transition."""

    crash_kinds: tuple[str, ...] = ()
    crashes = 0

    def drive(self, *, cycle, task, prompt, resume=None):
        if resume is not None and resume["kind"] in type(self).crash_kinds:
            type(self).crashes += 1
            raise RuntimeError("worker died before applying the resume")
        return super().drive(cycle=cycle, task=task, prompt=prompt, resume=resume)


class CrashAfterApply(FakeDriver):
    """Applies the transition, then dies before the consumption is recorded."""

    crash_kinds: tuple[str, ...] = ()
    crashes = 0

    def drive(self, *, cycle, task, prompt, resume=None):
        result = super().drive(cycle=cycle, task=task, prompt=prompt, resume=resume)
        if resume is not None and resume["kind"] in type(self).crash_kinds:
            type(self).crashes += 1
            raise RuntimeError("worker died after applying the resume")
        return result


def _build(tmp_path, driver_cls, *, fresh, client_cls=FakeClient):
    """Construct a server; ``fresh=False`` models a restart over the same state."""
    source = tmp_path / "source"
    if fresh:
        _git_repo(source)
    config = ServerConfig(
        repositories=("owner/repo",),
        repo_paths={"owner/repo": source},
        db=tmp_path / "state.db",
        checkpoints=tmp_path / "checkpoints.db",
        memory_db=tmp_path / "memory.db",
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        model="offline-model",
        unsafe_local_shell=True,
        max_ticks=30,
    )
    client = client_cls()
    return SWEForgeServer(
        config,
        client_factory=lambda _config: (client, client),
        driver_factory=lambda runtime, cycle_id, worktree, spec: driver_cls(
            runtime, cycle_id, worktree, spec
        ),
        publisher_factory=FakePublisher,
        learning_factory=FakeLearning,
        now=lambda: datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    )


def _reset(clarify_once=False):
    for cls in (FakeDriver, CrashBeforeApply, CrashAfterApply):
        cls.events = []
        cls.prompts = []
        cls.specs = []
        cls.verdicts = {}
        cls.clarify_once = clarify_once
        cls.clarified = False
    CrashBeforeApply.crash_kinds = ()
    CrashBeforeApply.crashes = 0
    CrashAfterApply.crash_kinds = ()
    CrashAfterApply.crashes = 0
    FakePublisher.calls = []


def _root(server, body="@agent implement"):
    _record(
        server.config.db,
        "issues",
        _event(SourceKind.ISSUE, "root", body, "2026-01-01T00:00:00Z"),
    )


def _comment(server, source_id, body, minute):
    event = _event(
        SourceKind.ISSUE_COMMENT, source_id, body, f"2026-01-01T00:{minute:02d}:00Z"
    )
    _record(server.config.db, "issue_comments", event)
    return event


def _state(server):
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    consumptions = {
        row["event_key"]: dict(row)
        for row in store.connection.execute("SELECT * FROM thread_input_consumptions")
    }
    permits = store.connection.execute(
        "SELECT * FROM workflow_task_permits_v1"
    ).fetchall()
    approvals = store.connection.execute(
        "SELECT * FROM workflow_task_result_approvals_v1"
    ).fetchall()
    store.close()
    return (
        task,
        cycle,
        consumptions,
        [dict(p) for p in permits],
        [dict(a) for a in approvals],
    )


# --- schema / migration ---------------------------------------------------


def test_existing_database_migrates_and_keeps_old_dispositions(tmp_path):
    """A pre-change database opens and its consumptions stay meaningful."""
    path = tmp_path / "legacy.db"
    store = SQLiteGitHubStore(path)
    store.connection.execute("DROP TABLE thread_input_consumptions")
    store.connection.execute(
        """CREATE TABLE thread_input_consumptions (
           event_key TEXT PRIMARY KEY,
           thread_id TEXT NOT NULL,
           cycle_id INTEGER NOT NULL,
           purpose TEXT NOT NULL,
           status TEXT NOT NULL,
           claimed_at TEXT NOT NULL,
           consumed_at TEXT)"""
    )
    store.connection.execute(
        "INSERT INTO thread_input_consumptions VALUES(?,?,?,?,?,?,?)",
        ("legacy-event", "t", 1, "CLARIFICATION_ROUTED", "PLAN_APPROVAL", "t0", "t0"),
    )
    store.connection.commit()
    store.close()

    reopened = SQLiteGitHubStore(path)
    columns = {
        row[1]
        for row in reopened.connection.execute(
            "PRAGMA table_info(thread_input_consumptions)"
        )
    }
    assert "occurrence_key" in columns
    record = reopened.input_consumption("legacy-event")
    assert record.status == "PLAN_APPROVAL"
    assert record.consumed_at == "t0"
    assert record.occurrence_key is None
    # An already-consumed legacy row is never mistaken for a pending claim.
    assert reopened.pending_resume_claim("t") is None
    reopened.close()
    # Re-opening again is idempotent.
    again = SQLiteGitHubStore(path)
    again.close()


def test_claim_is_not_consumption_and_cannot_be_rebound(tmp_path):
    server = _build(tmp_path, FakeDriver, fresh=True)
    _reset()
    _root(server)
    server._worker_entry(THREAD)
    event = _comment(server, "later", "@agent something", 45)
    store = SQLiteGitHubStore(server.config.db)
    store.claim_input_for_resume(
        event.event_key,
        thread_id=THREAD,
        cycle_id=1,
        status="PLAN_APPROVAL",
        occurrence_key="occ-1",
        claimed_at="t1",
    )
    claim = store.pending_resume_claim(THREAD)
    assert claim.status == "PLAN_APPROVAL"
    assert claim.occurrence_key == "occ-1"
    assert claim.consumed_at is None
    with pytest.raises(ValueError, match="already bound"):
        store.claim_input_for_resume(
            event.event_key,
            thread_id=THREAD,
            cycle_id=1,
            status="RESULT_APPROVAL",
            occurrence_key="occ-2",
            claimed_at="t2",
        )
    store.finish_input_resume(event.event_key, applied_at="t3")
    assert store.pending_resume_claim(THREAD) is None
    assert store.input_consumption(event.event_key).consumed_at == "t3"
    store.close()


# --- plan approval --------------------------------------------------------


def test_plan_approval_survives_a_crash_before_the_transition(tmp_path):
    """Case B: claimed, worker died, restart replays without a second comment."""
    _reset()
    CrashBeforeApply.crash_kinds = ("PLAN_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    approval = _comment(server, "approve", "@agent approve", 40)
    with pytest.raises(RuntimeError, match="died before"):
        server._worker_entry(THREAD)

    task, cycle, consumptions, permits, _ = _state(server)
    assert task["phase"] == "WAITING_FOR_PLAN_APPROVAL"
    assert permits == []
    claimed = consumptions[approval.event_key]
    assert claimed["status"] == "PLAN_APPROVAL"
    assert claimed["consumed_at"] is None, "delivery attempt must not consume"

    # Restart: entirely new server, store and driver over the same state.
    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    task, cycle, consumptions, permits, _ = _state(restarted)
    assert ("implementation", "APPROVED") in FakeDriver.events
    assert len(permits) == 1
    assert permits[0]["approval_event_key"] == approval.event_key
    assert consumptions[approval.event_key]["consumed_at"] is not None
    assert cycle["active_task_id"] == "implementation"


def test_plan_approval_applied_then_crash_is_not_applied_twice(tmp_path):
    """Case C: transition landed, ack lost; recovery reconciles the proof."""
    _reset()
    CrashAfterApply.crash_kinds = ("PLAN_APPROVAL",)
    server = _build(tmp_path, CrashAfterApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    approval = _comment(server, "approve", "@agent approve", 40)
    with pytest.raises(RuntimeError, match="died after"):
        server._worker_entry(THREAD)

    _, _, consumptions, permits, _ = _state(server)
    assert len(permits) == 1
    assert consumptions[approval.event_key]["consumed_at"] is None

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    _, _, consumptions, permits, _ = _state(restarted)
    assert len(permits) == 1, "recovery must not authorize a second time"
    assert ("implementation", "APPROVED") not in FakeDriver.events
    assert consumptions[approval.event_key]["consumed_at"] is not None


def test_repeated_recovery_ticks_and_restarts_are_idempotent(tmp_path):
    _reset()
    CrashBeforeApply.crash_kinds = ("PLAN_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    _comment(server, "approve", "@agent approve", 40)
    with pytest.raises(RuntimeError):
        server._worker_entry(THREAD)
    _reset()
    for _ in range(3):
        _build(tmp_path, FakeDriver, fresh=False)._worker_entry(THREAD)
    _, _, _, permits, _ = _state(server)
    assert len(permits) == 1


# --- clarification --------------------------------------------------------


def test_clarification_answer_survives_a_crash_before_resume(tmp_path):
    _reset(clarify_once=True)
    CrashBeforeApply.crash_kinds = ("CLARIFICATION_RESPONSE",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server, "@agent ask if needed")
    server._worker_entry(THREAD)
    task, _, _, _, _ = _state(server)
    assert task["phase"] == "WAITING_FOR_INPUT"

    answer = _comment(server, "answer", "@agent use the existing API", 41)
    with pytest.raises(RuntimeError, match="died before"):
        server._worker_entry(THREAD)
    task, _, consumptions, _, _ = _state(server)
    assert task["phase"] == "WAITING_FOR_INPUT"
    assert consumptions[answer.event_key]["consumed_at"] is None
    assert consumptions[answer.event_key]["status"] == "CLARIFICATION_RESPONSE"

    # Restart with no new GitHub comment.
    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    task, cycle, consumptions, _, _ = _state(restarted)
    assert ("implementation", "CLARIFIED") in FakeDriver.events
    # Resumed to the exact originating phase, with ownership retained.
    assert task["phase"] == "PLANNING"
    assert task["waiting_from_phase"] is None
    assert consumptions[answer.event_key]["consumed_at"] is not None
    assert cycle["active_task_id"] == "implementation"


def test_clarification_applied_then_crash_is_not_resumed_twice(tmp_path):
    _reset(clarify_once=True)
    CrashAfterApply.crash_kinds = ("CLARIFICATION_RESPONSE",)
    server = _build(tmp_path, CrashAfterApply, fresh=True)
    _root(server, "@agent ask if needed")
    server._worker_entry(THREAD)
    answer = _comment(server, "answer", "@agent use the existing API", 41)
    with pytest.raises(RuntimeError, match="died after"):
        server._worker_entry(THREAD)
    task, _, consumptions, _, _ = _state(server)
    assert task["phase"] != "WAITING_FOR_INPUT"
    assert consumptions[answer.event_key]["consumed_at"] is None

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    _, _, consumptions, _, _ = _state(restarted)
    assert ("implementation", "CLARIFIED") not in FakeDriver.events
    assert consumptions[answer.event_key]["consumed_at"] is not None


def test_duplicate_delivery_of_the_same_answer_applies_once(tmp_path):
    _reset(clarify_once=True)
    server = _build(tmp_path, FakeDriver, fresh=True)
    _root(server, "@agent ask if needed")
    server._worker_entry(THREAD)
    answer = _comment(server, "answer", "@agent use the existing API", 41)
    server._worker_entry(THREAD)
    applied = list(FakeDriver.events)
    # Re-ingesting the identical SourceEvent must change nothing.
    _record(server.config.db, "issue_comments", answer)
    server._worker_entry(THREAD)
    assert FakeDriver.events.count(("implementation", "CLARIFIED")) == 1
    assert applied.count(("implementation", "CLARIFIED")) == 1


# --- occurrence identity --------------------------------------------------


def test_a_claim_for_a_superseded_occurrence_fails_closed(tmp_path):
    """An old approval can never authorize a newer plan."""
    _reset()
    server = _build(tmp_path, FakeDriver, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    stale = _comment(server, "stale", "@agent approve", 39)
    store = SQLiteGitHubStore(server.config.db)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    store.claim_input_for_resume(
        stale.event_key,
        thread_id=THREAD,
        cycle_id=1,
        status="PLAN_APPROVAL",
        occurrence_key="plan-approval:some:older:v0:deadbeef",
        claimed_at="2026-01-01T00:35:00Z",
    )
    store.close()

    server._worker_entry(THREAD)
    _, _, consumptions, permits, _ = _state(server)
    assert permits == [], "a superseded claim must never authorize"
    assert consumptions[stale.event_key]["status"] == "STALE"
    assert consumptions[stale.event_key]["consumed_at"] is not None
    assert task["phase"] == "WAITING_FOR_PLAN_APPROVAL"


def test_unauthorized_approver_is_consumed_and_never_claimed(tmp_path):
    _reset()
    server = _build(tmp_path, FakeDriver, fresh=True, client_cls=ReadOnlyClient)
    _root(server)
    server._worker_entry(THREAD)
    approval = _comment(server, "approve", "@agent approve", 40)
    server._worker_entry(THREAD)
    _, _, consumptions, permits, _ = _state(server)
    assert permits == []
    assert consumptions[approval.event_key]["status"] == "STALE"
    # Terminal, never a replayable claim.
    store = SQLiteGitHubStore(server.config.db)
    assert store.pending_resume_claim(THREAD) is None
    store.close()


# --- result approval ------------------------------------------------------


def _to_result_wait(server):
    """Drive a thread to WAITING_FOR_RESULT_APPROVAL."""
    _root(server)
    server._worker_entry(THREAD)
    _comment(server, "plan-approve", "@agent approve", 40)
    server._worker_entry(THREAD)
    task, *_ = _state(server)
    assert task["phase"] == "WAITING_FOR_RESULT_APPROVAL"


def test_result_approval_survives_a_crash_before_the_transition(tmp_path):
    _reset()
    CrashBeforeApply.crash_kinds = ("RESULT_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _to_result_wait(server)
    approval = _comment(server, "result-approve", "@agent approve", 50)
    with pytest.raises(RuntimeError, match="died before"):
        server._worker_entry(THREAD)
    task, _, consumptions, _, approvals = _state(server)
    assert task["phase"] == "WAITING_FOR_RESULT_APPROVAL"
    assert approvals == []
    assert consumptions[approval.event_key]["consumed_at"] is None
    assert consumptions[approval.event_key]["status"] == "RESULT_APPROVAL"

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    task, _, consumptions, _, approvals = _state(restarted)
    assert len(approvals) == 1
    assert approvals[0]["approval_event_key"] == approval.event_key
    assert consumptions[approval.event_key]["consumed_at"] is not None
    assert task["status"] == "DONE"


def test_result_approval_applied_then_crash_is_not_accepted_twice(tmp_path):
    _reset()
    CrashAfterApply.crash_kinds = ("RESULT_APPROVAL",)
    server = _build(tmp_path, CrashAfterApply, fresh=True)
    _to_result_wait(server)
    approval = _comment(server, "result-approve", "@agent approve", 50)
    with pytest.raises(RuntimeError, match="died after"):
        server._worker_entry(THREAD)
    _, _, consumptions, _, approvals = _state(server)
    assert len(approvals) == 1
    assert consumptions[approval.event_key]["consumed_at"] is None

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    _, _, consumptions, _, approvals = _state(restarted)
    assert len(approvals) == 1, "recovery must not accept the result twice"
    assert ("implementation", "RESULT_APPROVED") not in FakeDriver.events
    assert consumptions[approval.event_key]["consumed_at"] is not None


def test_a_plan_claim_can_never_authorize_the_result_occurrence(tmp_path):
    """Kind and occurrence are both part of the binding."""
    _reset()
    server = _build(tmp_path, FakeDriver, fresh=True)
    _to_result_wait(server)
    store = SQLiteGitHubStore(server.config.db)
    plan_event = _comment(server, "mislabelled", "@agent approve", 51)
    task = store.connection.execute("SELECT * FROM workflow_task_runs_v1").fetchone()
    plan = store.connection.execute(
        "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
        (task["current_plan_id"],),
    ).fetchone()
    store.claim_input_for_resume(
        plan_event.event_key,
        thread_id=THREAD,
        cycle_id=1,
        status="PLAN_APPROVAL",
        occurrence_key=plan["approval_occurrence_key"],
        claimed_at="2026-01-01T00:51:00Z",
    )
    store.close()

    server._worker_entry(THREAD)
    _, _, consumptions, _, approvals = _state(server)
    # The plan-kind claim is not offered to the result wait; it is retired as
    # stale because the task no longer has a live plan-approval occurrence.
    assert consumptions[plan_event.event_key]["status"] == "STALE"
    assert all(a["approval_event_key"] != plan_event.event_key for a in approvals)


# --- feedback reviews -----------------------------------------------------


def test_plan_feedback_review_survives_a_crash_and_is_not_replayed_twice(tmp_path):
    """Feedback recovers from its durable review row, not from the event."""
    _reset()
    CrashBeforeApply.crash_kinds = ("PLAN_FEEDBACK",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    feedback = _comment(server, "fb", "@agent preserve the legacy shape", 40)
    with pytest.raises(RuntimeError, match="died before"):
        server._worker_entry(THREAD)

    store = SQLiteGitHubStore(server.config.db)
    reviews = store.connection.execute(
        "SELECT * FROM workflow_feedback_reviews_v1"
    ).fetchall()
    assert len(reviews) == 1
    review_id = reviews[0]["feedback_review_id"]
    assert reviews[0]["source_event_key"] == feedback.event_key
    store.close()

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    store = SQLiteGitHubStore(restarted.config.db)
    reviews = store.connection.execute(
        "SELECT * FROM workflow_feedback_reviews_v1"
    ).fetchall()
    # Exact same review identity; no duplicate review, no duplicate revision.
    assert [row["feedback_review_id"] for row in reviews] == [review_id]
    assert store.pending_revision_inputs(THREAD) == []
    store.close()


# --- invariants preserved through recovery --------------------------------


def test_ownership_is_retained_and_no_peer_starts_during_recovery(tmp_path):
    _reset()
    CrashBeforeApply.crash_kinds = ("PLAN_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    _comment(server, "approve", "@agent approve", 40)
    with pytest.raises(RuntimeError):
        server._worker_entry(THREAD)
    _, cycle, _, _, _ = _state(server)
    assert cycle["active_task_id"] == "implementation"

    _reset()
    restarted = _build(tmp_path, FakeDriver, fresh=False)
    restarted._worker_entry(THREAD)
    store = SQLiteGitHubStore(restarted.config.db)
    rows = store.connection.execute(
        "SELECT task_id,status FROM workflow_task_runs_v1"
    ).fetchall()
    store.close()
    assert len(rows) == 1 and rows[0]["task_id"] == "implementation"


def test_a_claimed_thread_is_runnable_so_recovery_is_actually_dispatched(tmp_path):
    """Claiming removes the event from the unconsumed set; the thread must
    still be selected, or a dead worker would strand it silently."""
    _reset()
    CrashBeforeApply.crash_kinds = ("PLAN_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    _comment(server, "approve", "@agent approve", 40)
    with pytest.raises(RuntimeError):
        server._worker_entry(THREAD)

    store = SQLiteGitHubStore(server.config.db)
    assert store.unconsumed_inputs(THREAD) == []
    assert store.pending_resume_claim(THREAD) is not None
    later = "2026-01-01T09:00:00Z"
    assert store.is_thread_runnable(THREAD, now=later) is True
    assert THREAD in list(store.runnable_thread_ids(now=later))
    store.close()


def test_plain_and_wrong_surface_comments_never_become_claims(tmp_path):
    _reset()
    server = _build(tmp_path, FakeDriver, fresh=True)
    _root(server)
    server._worker_entry(THREAD)
    _comment(server, "chatter", "looks good to me", 40)
    server._worker_entry(THREAD)
    store = SQLiteGitHubStore(server.config.db)
    assert store.pending_resume_claim(THREAD) is None
    _, _, _, permits, _ = _state(server)
    assert permits == []
    store.close()


def test_publication_stays_ineligible_while_a_claim_is_unapplied(tmp_path):
    _reset()
    CrashBeforeApply.crash_kinds = ("RESULT_APPROVAL",)
    server = _build(tmp_path, CrashBeforeApply, fresh=True)
    _to_result_wait(server)
    _comment(server, "result-approve", "@agent approve", 50)
    with pytest.raises(RuntimeError):
        server._worker_entry(THREAD)
    store = SQLiteGitHubStore(server.config.db)
    cycle = store.connection.execute("SELECT * FROM workflow_cycles_v1").fetchone()
    from sweforge.workflow_runtime import WorkflowRuntime

    assert not WorkflowRuntime(store).publication_is_eligible(
        cycle["workflow_cycle_id"]
    )
    assert FakePublisher.calls == []
    store.close()
