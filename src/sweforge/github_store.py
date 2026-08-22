"""Durable SQLite persistence for GitHub ingestion."""

# SQL statements are kept readable as complete statements.
# ruff: noqa: E501

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .github_models import (
    SourceEvent,
    SubjectKind,
    is_exact_agent_approval,
    starts_with_agent_invocation,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS repositories (
    repo_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_threads (
    thread_id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, issue_number)
);
CREATE TABLE IF NOT EXISTS source_events (
    event_key TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    source_created_at TEXT,
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL,
    author_login TEXT,
    body TEXT NOT NULL,
    html_url TEXT,
    thread_id TEXT REFERENCES issue_threads(thread_id),
    discovered_at TEXT NOT NULL,
    origin_surface TEXT NOT NULL DEFAULT 'ISSUE',
    path TEXT,
    line INTEGER,
    start_line INTEGER,
    side TEXT,
    start_side TEXT,
    diff_hunk TEXT,
    commit_id TEXT,
    original_commit_id TEXT,
    in_reply_to_id TEXT,
    pull_request_review_id TEXT,
    review_thread_root_id TEXT
);
CREATE TABLE IF NOT EXISTS poll_cursors (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    stream TEXT NOT NULL,
    since TEXT NOT NULL,
    etag TEXT,
    last_successful_poll_at TEXT,
    PRIMARY KEY(repo_id, stream)
);
CREATE TABLE IF NOT EXISTS pr_thread_mappings (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    pr_number INTEGER NOT NULL,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    PRIMARY KEY(repo_id, pr_number)
);
CREATE TABLE IF NOT EXISTS thread_workspaces (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    source_repository_path TEXT NOT NULL,
    workspace_path TEXT NOT NULL UNIQUE,
    branch_name TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_executions (
    event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    response_text TEXT,
    error_message TEXT,
    workspace_path TEXT,
    start_head_sha TEXT,
    end_head_sha TEXT,
    end_dirty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_publications (
    event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    local_commit_sha TEXT,
    remote_commit_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    comment_id INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_workflow_state (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    phase TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    current_plan_id TEXT,
    mode TEXT NOT NULL,
    response_surface TEXT NOT NULL DEFAULT 'ISSUE',
    response_subject_number INTEGER,
    response_comment_id TEXT,
    response_url TEXT,
    review_thread_root_id TEXT,
    planning_feedback_event_key TEXT REFERENCES source_events(event_key),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_plans (
    plan_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    cycle_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    plan_text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    posted_at TEXT,
    posted_comment_id INTEGER,
    approved_at TEXT,
    approved_by TEXT,
    approval_event_key TEXT,
    UNIQUE(thread_id, cycle_id, version)
);
CREATE TABLE IF NOT EXISTS execution_permits (
    permit_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    source TEXT NOT NULL,
    source_event_key TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT
);
CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    attempt_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    repair_round INTEGER NOT NULL DEFAULT 0,
    parent_review_id TEXT,
    authorization_id TEXT NOT NULL,
    status TEXT NOT NULL,
    response_text TEXT,
    start_head_sha TEXT,
    end_head_sha TEXT,
    start_dirty INTEGER NOT NULL DEFAULT 0,
    end_dirty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(thread_id, cycle_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS execution_reviews (
    review_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_attempts(attempt_id),
    review_iteration INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    repair_instructions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_repair_permits (
    permit_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT REFERENCES source_events(event_key),
    parent_review_id TEXT NOT NULL REFERENCES execution_reviews(review_id),
    repair_round INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT,
    UNIQUE(thread_id, cycle_id, repair_round)
);
CREATE TABLE IF NOT EXISTS thread_input_consumptions (
    event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


@dataclass
class RecordBatchResult:
    events_persisted: int = 0
    threads_created: int = 0
    events_routed: int = 0
    pr_events_unrouted: int = 0


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    RETRY_PENDING = "RETRY_PENDING"
    SKIPPED = "SKIPPED"


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    PR_CREATED = "PR_CREATED"
    COMMENTED = "COMMENTED"
    COMPLETED = "COMPLETED"
    NO_CHANGES = "NO_CHANGES"
    FAILED = "FAILED"


class WorkflowPhase(StrEnum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    WAITING_FOR_PLAN_APPROVAL = "WAITING_FOR_PLAN_APPROVAL"
    EXECUTION_READY = "EXECUTION_READY"
    EXECUTING = "EXECUTING"
    AWAITING_PUBLICATION = "AWAITING_PUBLICATION"
    REVIEW_EXECUTION = "REVIEW_EXECUTION"
    REPAIR_READY = "REPAIR_READY"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"


class WorkflowMode(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    AUTO = "AUTO"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    SUPERSEDED = "SUPERSEDED"
    APPROVED = "APPROVED"
    AUTO_APPROVED = "AUTO_APPROVED"
    EXECUTED = "EXECUTED"


class PermitSource(StrEnum):
    USER = "USER"
    AUTO = "AUTO"


class InputPurpose(StrEnum):
    CYCLE_ROOT = "CYCLE_ROOT"
    PLAN_FEEDBACK = "PLAN_FEEDBACK"
    LIVE_PLANNING_INPUT = "LIVE_PLANNING_INPUT"
    LIVE_EXECUTION_INPUT = "LIVE_EXECUTION_INPUT"
    PLAN_APPROVAL = "PLAN_APPROVAL"
    EARLY_PLAN_APPROVAL = "EARLY_PLAN_APPROVAL"
    STALE_PLAN_APPROVAL = "STALE_PLAN_APPROVAL"
    LIVE_REVIEW_INPUT = "LIVE_REVIEW_INPUT"


class AttemptKind(StrEnum):
    INITIAL = "INITIAL"
    REVIEW_REPAIR = "REVIEW_REPAIR"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class PendingWorkflowInputError(ValueError):
    def __init__(self, event_key: str) -> None:
        self.event_key = event_key
        super().__init__(f"workflow input requires replanning: {event_key}")


class PendingRepairInputError(PendingWorkflowInputError):
    """A repair was prevented by new user input that must not be consumed."""


RESUMABLE_PUBLICATION_STATUSES = (
    PublicationStatus.PENDING,
    PublicationStatus.COMMITTED,
    PublicationStatus.PUSHED,
    PublicationStatus.PR_CREATED,
    PublicationStatus.COMMENTED,
)


@dataclass(frozen=True)
class ClaimedEvent:
    event_key: str
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    body: str
    workspace_path: str | None
    retrying: bool = False
    origin_surface: str = "ISSUE"
    path: str | None = None
    line: int | None = None
    start_line: int | None = None
    side: str | None = None
    start_side: str | None = None
    diff_hunk: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    in_reply_to_id: str | None = None
    pull_request_review_id: str | None = None
    review_thread_root_id: str | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    event_key: str
    thread_id: str
    status: ExecutionStatus
    attempt_count: int
    started_at: str
    completed_at: str | None
    error_message: str | None


@dataclass(frozen=True)
class ExecutionAttemptRecord:
    attempt_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    attempt_number: int
    kind: AttemptKind
    repair_round: int
    parent_review_id: str | None
    authorization_id: str
    status: AttemptStatus
    response_text: str | None
    start_head_sha: str | None
    end_head_sha: str | None
    start_dirty: bool
    end_dirty: bool
    created_at: str
    completed_at: str | None
    retry_count: int


@dataclass(frozen=True)
class ExecutionReviewRecord:
    review_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    attempt_id: str
    review_iteration: int
    verdict: str
    summary: str
    findings_json: str
    repair_instructions_json: str
    created_at: str
    completed_at: str


@dataclass(frozen=True)
class ThreadWorkspaceRecord:
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    source_repository_path: str
    workspace_path: str
    branch_name: str
    base_commit: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PublicationRecord:
    event_key: str
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    status: PublicationStatus
    branch_name: str
    local_commit_sha: str | None
    remote_commit_sha: str | None
    pr_number: int | None
    pr_url: str | None
    comment_id: int | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkflowStateRecord:
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    phase: WorkflowPhase
    cycle_id: int
    root_event_key: str
    current_plan_id: str | None
    mode: WorkflowMode
    created_at: str
    updated_at: str
    response_surface: str = "ISSUE"
    response_subject_number: int | None = None
    response_comment_id: str | None = None
    response_url: str | None = None
    review_thread_root_id: str | None = None
    planning_feedback_event_key: str | None = None


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    cycle_id: int
    version: int
    root_event_key: str
    plan_text: str
    status: PlanStatus
    created_at: str
    posted_at: str | None
    posted_comment_id: int | None
    approved_at: str | None
    approved_by: str | None
    approval_event_key: str | None


@dataclass(frozen=True)
class ExecutionPermit:
    permit_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    source: PermitSource
    source_event_key: str | None
    created_at: str
    consumed_at: str | None
    invalidated_at: str | None


@dataclass(frozen=True)
class WorkflowInputRecord:
    event_key: str
    thread_id: str
    cycle_id: int
    purpose: InputPurpose
    status: str
    claimed_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class ReviewRepairPermit:
    permit_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str | None
    parent_review_id: str
    repair_round: int
    created_at: str
    consumed_at: str | None
    invalidated_at: str | None


class SQLiteGitHubStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self._migrate_execution_baselines()
        self.connection.commit()

    def _migrate_execution_baselines(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(event_executions)")
        }
        migrations = {
            "start_head_sha": (
                "ALTER TABLE event_executions ADD COLUMN start_head_sha TEXT"
            ),
            "end_head_sha": (
                "ALTER TABLE event_executions ADD COLUMN end_head_sha TEXT"
            ),
            "end_dirty": (
                "ALTER TABLE event_executions ADD COLUMN "
                "end_dirty INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.connection.execute(statement)
        repair_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(review_repair_permits)"
            )
        }
        if "root_event_key" not in repair_columns:
            self.connection.execute(
                "ALTER TABLE review_repair_permits ADD COLUMN root_event_key TEXT"
            )
        self.connection.execute(
            """UPDATE review_repair_permits SET root_event_key = (
               SELECT r.root_event_key FROM execution_reviews r
               WHERE r.review_id = review_repair_permits.parent_review_id)
               WHERE root_event_key IS NULL"""
        )
        permit_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_permits)")
        }
        if "root_event_key" not in permit_columns:
            self.connection.execute(
                "ALTER TABLE execution_permits ADD COLUMN root_event_key TEXT"
            )
            self.connection.execute(
                """UPDATE execution_permits SET root_event_key=(
                   SELECT root_event_key FROM issue_plans
                   WHERE issue_plans.plan_id=execution_permits.plan_id)"""
            )
        # Successful executions created before the review gate must never be
        # published implicitly after an upgrade.
        self.connection.execute(
            """UPDATE issue_workflow_state SET phase = ?
               WHERE phase = ? AND NOT EXISTS (
                 SELECT 1 FROM execution_reviews r
                 WHERE r.thread_id = issue_workflow_state.thread_id
                   AND r.verdict = 'ACCEPT')""",
            (
                WorkflowPhase.REVIEW_EXECUTION.value,
                WorkflowPhase.AWAITING_PUBLICATION.value,
            ),
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO execution_attempts(
               attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
               attempt_number,kind,repair_round,authorization_id,status,response_text,
               start_head_sha,end_head_sha,end_dirty,created_at,completed_at)
               SELECT 'attempt-' || p.permit_id, s.thread_id, s.cycle_id, p.plan_id,
                      p.plan_version, p.root_event_key, 1, 'INITIAL', 0, p.permit_id,
                      'SUCCEEDED', e.response_text, e.start_head_sha, e.end_head_sha,
                      e.end_dirty, e.started_at, e.completed_at
               FROM issue_workflow_state s
               JOIN execution_permits p ON p.thread_id=s.thread_id AND p.cycle_id=s.cycle_id
                 AND p.plan_id=s.current_plan_id
               JOIN event_executions e ON e.event_key=p.root_event_key
               WHERE e.status='SUCCEEDED'"""
        )
        self.connection.execute(
            """UPDATE issue_workflow_state SET phase=?
               WHERE phase='EXECUTING' AND EXISTS (
                 SELECT 1 FROM event_executions e
                 WHERE e.event_key=issue_workflow_state.root_event_key
                   AND e.status='SUCCEEDED')""",
            (WorkflowPhase.REVIEW_EXECUTION.value,),
        )
        source_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(source_events)")
        }
        source_migrations = {
            "source_created_at": (
                "ALTER TABLE source_events ADD COLUMN source_created_at TEXT"
            ),
            "origin_surface": (
                "ALTER TABLE source_events ADD COLUMN origin_surface TEXT "
                "NOT NULL DEFAULT 'ISSUE'"
            ),
            "path": "ALTER TABLE source_events ADD COLUMN path TEXT",
            "line": "ALTER TABLE source_events ADD COLUMN line INTEGER",
            "start_line": "ALTER TABLE source_events ADD COLUMN start_line INTEGER",
            "side": "ALTER TABLE source_events ADD COLUMN side TEXT",
            "start_side": "ALTER TABLE source_events ADD COLUMN start_side TEXT",
            "diff_hunk": "ALTER TABLE source_events ADD COLUMN diff_hunk TEXT",
            "commit_id": "ALTER TABLE source_events ADD COLUMN commit_id TEXT",
            "original_commit_id": (
                "ALTER TABLE source_events ADD COLUMN original_commit_id TEXT"
            ),
            "in_reply_to_id": (
                "ALTER TABLE source_events ADD COLUMN in_reply_to_id TEXT"
            ),
            "pull_request_review_id": (
                "ALTER TABLE source_events ADD COLUMN pull_request_review_id TEXT"
            ),
            "review_thread_root_id": (
                "ALTER TABLE source_events ADD COLUMN review_thread_root_id TEXT"
            ),
        }
        for column, statement in source_migrations.items():
            if column not in source_columns:
                self.connection.execute(statement)
        workflow_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(issue_workflow_state)"
            )
        }
        workflow_migrations = {
            "response_surface": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_surface TEXT "
                "NOT NULL DEFAULT 'ISSUE'"
            ),
            "response_subject_number": (
                "ALTER TABLE issue_workflow_state ADD COLUMN "
                "response_subject_number INTEGER"
            ),
            "response_comment_id": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_comment_id TEXT"
            ),
            "response_url": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_url TEXT"
            ),
            "review_thread_root_id": (
                "ALTER TABLE issue_workflow_state ADD COLUMN review_thread_root_id TEXT"
            ),
            "planning_feedback_event_key": (
                "ALTER TABLE issue_workflow_state ADD COLUMN "
                "planning_feedback_event_key TEXT"
            ),
        }
        for column, statement in workflow_migrations.items():
            if column not in workflow_columns:
                self.connection.execute(statement)
        permit_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_permits)")
        }
        if "root_event_key" not in permit_columns:
            self.connection.execute(
                "ALTER TABLE execution_permits ADD COLUMN root_event_key TEXT"
            )
        self.connection.execute(
            """UPDATE execution_permits SET root_event_key = (
               SELECT root_event_key FROM issue_plans
               WHERE issue_plans.plan_id = execution_permits.plan_id
            ) WHERE root_event_key IS NULL"""
        )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def upsert_repository(self, repo_id: int, full_name: str, observed_at: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO repositories(repo_id, full_name, observed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(repo_id) DO UPDATE SET full_name=excluded.full_name,
                   observed_at=excluded.observed_at""",
                (repo_id, full_name, observed_at),
            )

    def repository_id_for_full_name(self, full_name: str) -> int | None:
        """Resolve an observed repository name to its stable GitHub ID."""
        row = self.connection.execute(
            "SELECT repo_id FROM repositories WHERE full_name = ?", (full_name,)
        ).fetchone()
        return int(row["repo_id"]) if row is not None else None

    def cursor(self, repo_id: int, stream: str):
        return self.connection.execute(
            "SELECT since, etag, last_successful_poll_at FROM poll_cursors "
            "WHERE repo_id = ? AND stream = ?",
            (repo_id, stream),
        ).fetchone()

    def record_batch(
        self,
        repo_id: int,
        stream: str,
        events: list[SourceEvent],
        *,
        since: str,
        etag: str | None,
        polled_at: str,
    ) -> RecordBatchResult:
        result = RecordBatchResult()
        with self.transaction() as db:
            self._repair_redundant_issue_events(db, now=polled_at)
            for event in events:
                if event.repo_id != repo_id:
                    raise ValueError("event repository does not match the batch")
                if db.execute(
                    "SELECT 1 FROM source_events WHERE event_key = ?",
                    (event.event_key,),
                ).fetchone():
                    continue
                if self._has_resolved_issue_snapshot(db, event):
                    continue
                thread_id = self._resolve_thread(db, event, polled_at, result)
                db.execute(
                    """INSERT OR IGNORE INTO source_events(
                       event_key, repo_id, repo_full_name, source_kind, source_id,
                       source_updated_at, source_created_at, subject_kind,
                       subject_number, author_login,
                       body, html_url, thread_id, discovered_at, origin_surface,
                       path, line, start_line, side, start_side, diff_hunk,
                       commit_id, original_commit_id, in_reply_to_id,
                       pull_request_review_id, review_thread_root_id)
                       VALUES (?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?)""",
                    (
                        event.event_key,
                        event.repo_id,
                        event.repo_full_name,
                        event.source_kind.value,
                        event.source_id,
                        event.source_updated_at,
                        event.source_created_at,
                        event.subject_kind.value,
                        event.subject_number,
                        event.author_login,
                        event.body,
                        event.html_url,
                        thread_id,
                        polled_at,
                        event.origin_surface.value,
                        event.path,
                        event.line,
                        event.start_line,
                        event.side,
                        event.start_side,
                        event.diff_hunk,
                        event.commit_id,
                        event.original_commit_id,
                        event.in_reply_to_id,
                        event.pull_request_review_id,
                        event.review_thread_root_id,
                    ),
                )
                result.events_persisted += 1
            db.execute(
                """INSERT INTO poll_cursors(repo_id, stream, since, etag,
                   last_successful_poll_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repo_id, stream) DO UPDATE SET since=excluded.since,
                   etag=excluded.etag,
                   last_successful_poll_at=excluded.last_successful_poll_at""",
                (repo_id, stream, since, etag, polled_at),
            )
        return result

    @staticmethod
    def _has_resolved_issue_snapshot(
        db: sqlite3.Connection, event: SourceEvent
    ) -> bool:
        if event.source_kind.value != "issue":
            return False
        return (
            db.execute(
                """SELECT 1
                   FROM source_events earlier
                   JOIN event_executions execution
                     ON execution.event_key = earlier.event_key
                   LEFT JOIN event_publications publication
                     ON publication.event_key = earlier.event_key
                   WHERE earlier.repo_id = ?
                     AND earlier.source_kind = 'issue'
                     AND earlier.source_id = ?
                     AND earlier.body = ?
                     AND (
                         execution.status = ? OR (
                             execution.status = ? AND
                             publication.status IN (?, ?)
                         )
                     )
                   LIMIT 1""",
                (
                    event.repo_id,
                    event.source_id,
                    event.body,
                    ExecutionStatus.SKIPPED.value,
                    ExecutionStatus.SUCCEEDED.value,
                    PublicationStatus.COMPLETED.value,
                    PublicationStatus.NO_CHANGES.value,
                ),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _repair_redundant_issue_events(db: sqlite3.Connection, *, now: str) -> None:
        db.execute(
            """INSERT INTO event_executions(
                   event_key, thread_id, status, attempt_count, started_at,
                   completed_at, response_text, error_message, workspace_path)
               SELECT later.event_key, later.thread_id, ?, 0, later.discovered_at,
                      ?, NULL, ?, NULL
               FROM source_events later
               JOIN source_events earlier
                 ON earlier.repo_id = later.repo_id
                AND earlier.source_kind = 'issue'
                AND later.source_kind = 'issue'
                AND earlier.source_id = later.source_id
                AND earlier.body = later.body
                AND (
                    earlier.source_updated_at < later.source_updated_at OR
                    (earlier.source_updated_at = later.source_updated_at AND
                     earlier.event_key < later.event_key)
                )
               JOIN event_executions prior_execution
                 ON prior_execution.event_key = earlier.event_key
               LEFT JOIN event_publications prior_publication
                 ON prior_publication.event_key = earlier.event_key
               LEFT JOIN event_executions later_execution
                 ON later_execution.event_key = later.event_key
               WHERE later_execution.event_key IS NULL
                 AND (
                     prior_execution.status = ? OR (
                         prior_execution.status = ? AND
                         prior_publication.status IN (?, ?)
                     )
                 )""",
            (
                ExecutionStatus.SKIPPED.value,
                now,
                "duplicate unchanged issue snapshot after resolved event",
                ExecutionStatus.SKIPPED.value,
                ExecutionStatus.SUCCEEDED.value,
                PublicationStatus.COMPLETED.value,
                PublicationStatus.NO_CHANGES.value,
            ),
        )

    def claim_next_event(self, *, now: str) -> ClaimedEvent | None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT se.event_key, se.thread_id, se.repo_id,
                          se.repo_full_name, thread.issue_number, se.body,
                          se.origin_surface, se.path, se.line, se.start_line,
                          se.side, se.start_side, se.diff_hunk, se.commit_id,
                          se.original_commit_id, se.in_reply_to_id,
                          se.pull_request_review_id, se.review_thread_root_id,
                          ee.status AS execution_status
                   FROM source_events AS se
                   JOIN issue_threads AS thread
                     ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions AS ee
                     ON ee.event_key = se.event_key
                   WHERE se.thread_id IS NOT NULL
                     AND (ee.event_key IS NULL OR ee.status = ?)
                     AND NOT EXISTS (
                         SELECT 1
                         FROM source_events AS earlier
                         LEFT JOIN event_executions AS prior
                           ON prior.event_key = earlier.event_key
                         LEFT JOIN event_publications AS prior_publication
                           ON prior_publication.event_key = prior.event_key
                         WHERE earlier.thread_id = se.thread_id
                           AND (
                               earlier.source_updated_at < se.source_updated_at
                               OR (
                                   earlier.source_updated_at = se.source_updated_at
                                   AND earlier.discovered_at < se.discovered_at
                               )
                               OR (
                                   earlier.source_updated_at = se.source_updated_at
                                   AND earlier.discovered_at = se.discovered_at
                                   AND earlier.event_key < se.event_key
                               )
                           )
                           AND (
                               prior.event_key IS NULL OR NOT (
                                   prior.status = ? OR (
                                       prior.status = ? AND
                                       prior_publication.status IN (?, ?)
                                   )
                               )
                           )
                     )
                   ORDER BY se.source_updated_at, se.discovered_at, se.event_key
                   LIMIT 1""",
                (
                    ExecutionStatus.RETRY_PENDING.value,
                    ExecutionStatus.SKIPPED.value,
                    ExecutionStatus.SUCCEEDED.value,
                    PublicationStatus.COMPLETED.value,
                    PublicationStatus.NO_CHANGES.value,
                ),
            ).fetchone()
            if row is None:
                return None
            retrying = row["execution_status"] == ExecutionStatus.RETRY_PENDING.value
            if retrying:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, row["event_key"]),
                )
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (
                        row["event_key"],
                        row["thread_id"],
                        ExecutionStatus.RUNNING.value,
                        now,
                    ),
                )
            return ClaimedEvent(
                event_key=row["event_key"],
                thread_id=row["thread_id"],
                repo_id=row["repo_id"],
                repo_full_name=row["repo_full_name"],
                issue_number=row["issue_number"],
                body=row["body"],
                workspace_path=None,
                retrying=retrying,
                origin_surface=row["origin_surface"],
                path=row["path"],
                line=row["line"],
                start_line=row["start_line"],
                side=row["side"],
                start_side=row["start_side"],
                diff_hunk=row["diff_hunk"],
                commit_id=row["commit_id"],
                original_commit_id=row["original_commit_id"],
                in_reply_to_id=row["in_reply_to_id"],
                pull_request_review_id=row["pull_request_review_id"],
                review_thread_root_id=row["review_thread_root_id"],
            )

    def release_execution_claim(self, event_key: str, *, retrying: bool) -> None:
        with self.transaction() as db:
            if retrying:
                db.execute(
                    "UPDATE event_executions SET status = ? WHERE event_key = ? "
                    "AND status = ?",
                    (
                        ExecutionStatus.RETRY_PENDING.value,
                        event_key,
                        ExecutionStatus.RUNNING.value,
                    ),
                )
            else:
                db.execute(
                    "DELETE FROM event_executions WHERE event_key = ? AND status = ?",
                    (event_key, ExecutionStatus.RUNNING.value),
                )

    def running_executions_before(self, started_before: str) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """SELECT event_key, thread_id, status, attempt_count, started_at,
                      completed_at, error_message
               FROM event_executions WHERE status = ? AND started_at < ?
               ORDER BY started_at, event_key""",
            (ExecutionStatus.RUNNING.value, started_before),
        ).fetchall()
        return [self._execution_record(row) for row in rows]

    def mark_execution_interrupted(
        self, event_key: str, *, completed_at: str, error_message: str
    ) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE event_executions SET status = ?, completed_at = ?,
                   error_message = ? WHERE event_key = ? AND status = ?""",
                (
                    ExecutionStatus.INTERRUPTED.value,
                    completed_at,
                    error_message,
                    event_key,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("event execution is no longer running")

    def retry_execution(self, event_key: str) -> ExecutionStatus:
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM event_executions WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown event key")
            status = ExecutionStatus(row["status"])
            if status == ExecutionStatus.RETRY_PENDING:
                return status
            if status not in (ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED):
                raise ValueError(f"cannot retry execution in {status.value} status")
            db.execute(
                "UPDATE event_executions SET status = ?, completed_at = NULL "
                "WHERE event_key = ?",
                (ExecutionStatus.RETRY_PENDING.value, event_key),
            )
            db.execute(
                "UPDATE execution_permits SET consumed_at = NULL "
                "WHERE root_event_key = ? AND consumed_at IS NOT NULL "
                "AND invalidated_at IS NULL",
                (event_key,),
            )
            db.execute(
                """UPDATE issue_workflow_state SET phase = ?, updated_at = ?
                   WHERE root_event_key = ? AND phase = ?""",
                (
                    WorkflowPhase.EXECUTION_READY.value,
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    event_key,
                    WorkflowPhase.EXECUTING.value,
                ),
            )
            return ExecutionStatus.RETRY_PENDING

    def skip_execution(self, event_key: str, *, completed_at: str, reason: str) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM event_executions WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown event key")
            status = ExecutionStatus(row["status"])
            if status == ExecutionStatus.SKIPPED:
                return
            if status not in (ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED):
                raise ValueError(f"cannot skip execution in {status.value} status")
            db.execute(
                "UPDATE event_executions SET status = ?, completed_at = ?, "
                "error_message = ? WHERE event_key = ?",
                (ExecutionStatus.SKIPPED.value, completed_at, reason, event_key),
            )

    def execution_records(self) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """SELECT event_key, thread_id, status, attempt_count, started_at,
                      completed_at, error_message FROM event_executions
               ORDER BY started_at, event_key"""
        ).fetchall()
        return [self._execution_record(row) for row in rows]

    @staticmethod
    def _execution_record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            event_key=row["event_key"],
            thread_id=row["thread_id"],
            status=ExecutionStatus(row["status"]),
            attempt_count=row["attempt_count"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error_message=row["error_message"],
        )

    def mark_execution_succeeded(
        self,
        event_key: str,
        *,
        completed_at: str,
        response_text: str,
        workspace_path: str,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> None:
        with self.transaction() as db:
            self._update_execution(
                db,
                event_key,
                ExecutionStatus.SUCCEEDED,
                completed_at=completed_at,
                response_text=response_text,
                error_message=None,
                workspace_path=workspace_path,
                start_head_sha=start_head_sha,
                end_head_sha=end_head_sha,
                end_dirty=end_dirty,
            )

    def mark_execution_failed(
        self,
        event_key: str,
        *,
        completed_at: str,
        error_message: str,
        workspace_path: str | None,
    ) -> None:
        with self.transaction() as db:
            self._update_execution(
                db,
                event_key,
                ExecutionStatus.FAILED,
                completed_at=completed_at,
                response_text=None,
                error_message=error_message,
                workspace_path=workspace_path,
            )

    @staticmethod
    def _update_execution(
        db: sqlite3.Connection,
        event_key: str,
        status: ExecutionStatus,
        *,
        completed_at: str,
        response_text: str | None,
        error_message: str | None,
        workspace_path: str | None,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> None:
        cursor = db.execute(
            """UPDATE event_executions SET status = ?, completed_at = ?,
               response_text = ?, error_message = ?, workspace_path = ?,
               start_head_sha = ?, end_head_sha = ?, end_dirty = ?
               WHERE event_key = ? AND status = ?""",
            (
                status.value,
                completed_at,
                response_text,
                error_message,
                workspace_path,
                start_head_sha,
                end_head_sha,
                int(end_dirty),
                event_key,
                ExecutionStatus.RUNNING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("event execution is not currently running")

    def execution_for_event(self, event_key: str):
        return self.connection.execute(
            "SELECT * FROM event_executions WHERE event_key = ?", (event_key,)
        ).fetchone()

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> ExecutionAttemptRecord:
        return ExecutionAttemptRecord(
            attempt_id=row["attempt_id"],
            thread_id=row["thread_id"],
            cycle_id=row["cycle_id"],
            plan_id=row["plan_id"],
            plan_version=row["plan_version"],
            root_event_key=row["root_event_key"],
            attempt_number=row["attempt_number"],
            kind=AttemptKind(row["kind"]),
            repair_round=row["repair_round"],
            parent_review_id=row["parent_review_id"],
            authorization_id=row["authorization_id"],
            status=AttemptStatus(row["status"]),
            response_text=row["response_text"],
            start_head_sha=row["start_head_sha"],
            end_head_sha=row["end_head_sha"],
            start_dirty=bool(row["start_dirty"]),
            end_dirty=bool(row["end_dirty"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            retry_count=row["retry_count"],
        )

    def execution_attempt(self, attempt_id: str) -> ExecutionAttemptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._attempt_record(row) if row else None

    def latest_attempt(
        self, thread_id: str, cycle_id: int
    ) -> ExecutionAttemptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_attempts WHERE thread_id = ? AND cycle_id = "
            "? ORDER BY attempt_number DESC LIMIT 1",
            (thread_id, cycle_id),
        ).fetchone()
        return self._attempt_record(row) if row else None

    def ensure_execution_attempt(
        self,
        *,
        attempt_id: str,
        thread_id: str,
        cycle_id: int,
        plan_id: str,
        plan_version: int,
        root_event_key: str,
        authorization_id: str,
        created_at: str,
        kind: AttemptKind = AttemptKind.INITIAL,
        parent_review_id: str | None = None,
        repair_round: int = 0,
        attempt_number: int | None = None,
    ) -> ExecutionAttemptRecord:
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing:
                if existing["status"] != AttemptStatus.SUCCEEDED.value:
                    db.execute(
                        "UPDATE execution_attempts SET status=?,retry_count=retry_count+1,completed_at=NULL WHERE attempt_id=?",
                        (AttemptStatus.RUNNING.value, attempt_id),
                    )
                return self._attempt_record(existing)
            number = (
                attempt_number
                or (
                    db.execute(
                        "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM execution_attempts "
                        "WHERE thread_id = ? AND cycle_id = ?",
                        (thread_id, cycle_id),
                    ).fetchone()[0]
                )
            )
            db.execute(
                "INSERT INTO execution_attempts(attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,attempt_number,kind,repair_round,parent_review_id,authorization_id,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    thread_id,
                    cycle_id,
                    plan_id,
                    plan_version,
                    root_event_key,
                    number,
                    kind.value,
                    repair_round,
                    parent_review_id,
                    authorization_id,
                    AttemptStatus.RUNNING.value,
                    created_at,
                ),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def finish_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: AttemptStatus,
        completed_at: str,
        response_text: str | None = None,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> ExecutionAttemptRecord:
        with self.transaction() as db:
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=?,response_text=?,start_head_sha=COALESCE(?,start_head_sha),end_head_sha=?,end_dirty=? WHERE attempt_id=?",
                (
                    status.value,
                    completed_at,
                    response_text,
                    start_head_sha,
                    end_head_sha,
                    int(end_dirty),
                    attempt_id,
                ),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def execution_review_for_attempt(
        self, attempt_id: str
    ) -> ExecutionReviewRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_reviews WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not row:
            return None
        return ExecutionReviewRecord(**dict(row))

    def execution_review(self, review_id: str) -> ExecutionReviewRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return ExecutionReviewRecord(**dict(row)) if row else None

    def save_execution_review(
        self, review: ExecutionReviewRecord
    ) -> ExecutionReviewRecord:
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO execution_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.thread_id,
                    review.cycle_id,
                    review.plan_id,
                    review.plan_version,
                    review.root_event_key,
                    review.attempt_id,
                    review.review_iteration,
                    review.verdict,
                    review.summary,
                    review.findings_json,
                    review.repair_instructions_json,
                    review.created_at,
                    review.completed_at,
                ),
            )
        return self.execution_review_for_attempt(review.attempt_id)  # type: ignore[return-value]

    def accept_execution_review(self, review_id: str, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            state = (
                db.execute(
                    "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                    (row["thread_id"],),
                ).fetchone()
                if row
                else None
            )
            attempt = (
                db.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()
                if row
                else None
            )
            plan = (
                db.execute(
                    "SELECT * FROM issue_plans WHERE plan_id = ?", (row["plan_id"],)
                ).fetchone()
                if row
                else None
            )
            latest = (
                db.execute(
                    "SELECT attempt_id FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (state["thread_id"], state["cycle_id"]),
                ).fetchone()
                if state
                else None
            )
            if (
                not row
                or not state
                or not attempt
                or row["verdict"] != "ACCEPT"
                or state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or row["thread_id"] != state["thread_id"]
                or row["cycle_id"] != state["cycle_id"]
                or row["root_event_key"] != state["root_event_key"]
                or not plan
                or state["current_plan_id"] != plan["plan_id"]
                or plan["version"] != row["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or not latest
                or latest["attempt_id"] != attempt["attempt_id"]
                or attempt["thread_id"] != state["thread_id"]
                or attempt["cycle_id"] != state["cycle_id"]
                or attempt["root_event_key"] != state["root_event_key"]
                or attempt["plan_id"] != plan["plan_id"]
                or attempt["plan_version"] != plan["version"]
            ):
                raise ValueError("execution review is stale or not acceptable")
            if (
                state["current_plan_id"] != row["plan_id"]
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
            ):
                raise ValueError(
                    "execution review does not match current successful attempt"
                )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.AWAITING_PUBLICATION.value, now, row["thread_id"]),
            )

    def block_execution_review(self, review_id: str, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?",
                (row["thread_id"],) if row else (None,),
            ).fetchone()
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?",
                (row["attempt_id"],) if row else (None,),
            ).fetchone()
            if (
                not row
                or not state
                or not attempt
                or row["verdict"] != "BLOCKED"
                or state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
                or state["root_event_key"] != row["root_event_key"]
                or state["current_plan_id"] != row["plan_id"]
            ):
                raise ValueError("execution review is stale or not blockable")
            db.execute(
                "UPDATE review_repair_permits SET invalidated_at=? WHERE thread_id=? AND consumed_at IS NULL",
                (now, row["thread_id"]),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.REVIEW_BLOCKED.value, now, row["thread_id"]),
            )

    def create_repair_permit(
        self, *, thread_id: str, now: str, max_repairs: int = 5
    ) -> ReviewRepairPermit:
        exhausted = False
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?", (thread_id,)
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=(SELECT current_plan_id FROM issue_workflow_state WHERE thread_id=?)",
                (thread_id,),
            ).fetchone()
            attempt = (
                db.execute(
                    "SELECT * FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (thread_id, state["cycle_id"] if state else -1),
                ).fetchone()
                if state
                else None
            )
            review = db.execute(
                "SELECT * FROM execution_reviews WHERE attempt_id=?",
                (attempt["attempt_id"],) if attempt else (None,),
            ).fetchone()
            if not state or not plan or not attempt or not review:
                raise ValueError("repair requires a current reviewed execution")
            latest = db.execute(
                "SELECT attempt_id FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                (thread_id, state["cycle_id"]),
            ).fetchone()
            if (
                state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
                or review["verdict"] != "NEEDS_FIXES"
                or review["thread_id"] != thread_id
                or review["cycle_id"] != state["cycle_id"]
                or review["root_event_key"] != state["root_event_key"]
                or review["plan_id"] != plan["plan_id"]
                or review["plan_version"] != plan["version"]
                or not latest
                or latest["attempt_id"] != attempt["attempt_id"]
                or plan["status"]
                not in (
                    PlanStatus.APPROVED.value,
                    PlanStatus.AUTO_APPROVED.value,
                )
            ):
                raise ValueError("repair review is stale")
            round_number = attempt["repair_round"] + 1
            if round_number > max_repairs:
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REVIEW_BLOCKED.value, now, thread_id),
                )
                db.execute(
                    "UPDATE review_repair_permits SET invalidated_at=? WHERE thread_id=? AND consumed_at IS NULL",
                    (now, thread_id),
                )
                exhausted = True
            if not exhausted:
                permit_id = f"repair-{review['review_id']}-{round_number}"
                db.execute(
                    """INSERT OR IGNORE INTO review_repair_permits(
                   permit_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
                   parent_review_id,repair_round,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        permit_id,
                        thread_id,
                        state["cycle_id"],
                        plan["plan_id"],
                        plan["version"],
                        state["root_event_key"],
                        review["review_id"],
                        round_number,
                        now,
                    ),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REPAIR_READY.value, now, thread_id),
                )
        if exhausted:
            raise ValueError("maximum review repair rounds reached")
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row))  # type: ignore[arg-type]

    def consume_repair_permit(self, permit_id: str, *, now: str) -> ReviewRepairPermit:
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    "UPDATE review_repair_permits SET consumed_at=? WHERE permit_id=? "
                    "AND consumed_at IS NULL AND invalidated_at IS NULL",
                    (now, permit_id),
                ).rowcount
                != 1
            ):
                raise ValueError("repair permit is unavailable")
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row))

    def repair_permit(self, permit_id: str) -> ReviewRepairPermit | None:
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row)) if row else None

    def repair_permit_for_thread(self, thread_id: str) -> ReviewRepairPermit | None:
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE thread_id=? AND consumed_at IS NULL "
            "AND invalidated_at IS NULL ORDER BY repair_round DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return ReviewRepairPermit(**dict(row)) if row else None

    def invalidate_repair_permit(self, permit_id: str, *, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE review_repair_permits SET invalidated_at=? WHERE permit_id=? AND consumed_at IS NULL",
                (now, permit_id),
            )

    def begin_or_resume_repair_attempt(
        self, permit_id: str, *, now: str
    ) -> ExecutionAttemptRecord:
        permit = self.repair_permit(permit_id)
        if permit is None:
            raise ValueError("repair permit is unavailable")
        return self.bind_repair_execution(
            permit_id, expected_thread_id=permit.thread_id, now=now
        )

    def bind_repair_execution(
        self, permit_id: str, *, expected_thread_id: str, now: str
    ) -> ExecutionAttemptRecord:
        pending_event: str | None = None
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
            if not permit or permit["root_event_key"] is None:
                raise ValueError("repair permit has no reconstructable root event")
            if permit["thread_id"] != expected_thread_id:
                raise ValueError("repair permit belongs to another thread")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?",
                (expected_thread_id,),
            ).fetchone()
            if not permit or permit["invalidated_at"] or permit["consumed_at"]:
                raise ValueError("repair permit is unavailable")
            if not state or state["phase"] != WorkflowPhase.REPAIR_READY.value:
                raise ValueError("workflow is not repair-ready")
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=?", (permit["plan_id"],)
            ).fetchone()
            parent = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id=?",
                (permit["parent_review_id"],),
            ).fetchone()
            latest = db.execute(
                "SELECT * FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                (permit["thread_id"], permit["cycle_id"]),
            ).fetchone()
            parent_attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?",
                (parent["attempt_id"],) if parent else (None,),
            ).fetchone()
            if (
                state["cycle_id"] != permit["cycle_id"]
                or state["root_event_key"] != permit["root_event_key"]
                or state["current_plan_id"] != permit["plan_id"]
                or not plan
                or plan["version"] != permit["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or not parent
                or parent["verdict"] != "NEEDS_FIXES"
                or parent["thread_id"] != expected_thread_id
                or parent["cycle_id"] != permit["cycle_id"]
                or parent["root_event_key"] != permit["root_event_key"]
                or parent["plan_id"] != permit["plan_id"]
                or parent["plan_version"] != permit["plan_version"]
                or not latest
                or not parent_attempt
                or latest["attempt_id"] != parent_attempt["attempt_id"]
                or latest["status"] != AttemptStatus.SUCCEEDED.value
                or (latest["repair_round"] or 0) + 1 != permit["repair_round"]
            ):
                raise ValueError("repair permit binding is stale")
            pending = db.execute(
                """SELECT event_key, body FROM source_events
                   WHERE thread_id=? AND event_key != ? AND event_key NOT IN
                   (SELECT event_key FROM thread_input_consumptions)
                   ORDER BY source_updated_at, discovered_at, event_key""",
                (expected_thread_id, permit["root_event_key"]),
            ).fetchall()
            for candidate in pending:
                if is_exact_agent_approval(candidate["body"]):
                    db.execute(
                        """INSERT OR IGNORE INTO thread_input_consumptions(
                           event_key,thread_id,cycle_id,purpose,status,claimed_at,consumed_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            candidate["event_key"],
                            expected_thread_id,
                            permit["cycle_id"],
                            InputPurpose.STALE_PLAN_APPROVAL.value,
                            "CONSUMED",
                            now,
                            now,
                        ),
                    )
                    self._resolve_workflow_control(
                        db,
                        event_key=candidate["event_key"],
                        thread_id=expected_thread_id,
                        claimed_at=now,
                        purpose=InputPurpose.STALE_PLAN_APPROVAL,
                    )
                elif starts_with_agent_invocation(candidate["body"]):
                    db.execute(
                        "UPDATE review_repair_permits SET invalidated_at=? WHERE permit_id=?",
                        (now, permit_id),
                    )
                    db.execute(
                        "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                        (WorkflowPhase.REVIEW_BLOCKED.value, now, expected_thread_id),
                    )
                    pending_event = candidate["event_key"]
                    break
            if pending_event:
                pass
            else:
                attempt_id = f"attempt-{permit_id}"
                existing = db.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE execution_attempts SET status=?,retry_count=retry_count+1,completed_at=NULL WHERE attempt_id=?",
                        (AttemptStatus.RUNNING.value, attempt_id),
                    )
                else:
                    db.execute(
                        """INSERT INTO execution_attempts(
                           attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
                           attempt_number,kind,repair_round,parent_review_id,authorization_id,
                           status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            attempt_id,
                            expected_thread_id,
                            permit["cycle_id"],
                            permit["plan_id"],
                            permit["plan_version"],
                            permit["root_event_key"],
                            latest["attempt_number"] + 1,
                            AttemptKind.REVIEW_REPAIR.value,
                            permit["repair_round"],
                            permit["parent_review_id"],
                            permit_id,
                            AttemptStatus.RUNNING.value,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.EXECUTING.value, now, expected_thread_id),
                )
                bound_attempt_id = attempt_id
        if pending_event:
            raise PendingRepairInputError(pending_event)
        return self.execution_attempt(bound_attempt_id)  # type: ignore[return-value]

    def finish_repair_attempt_success(
        self,
        permit_id: str,
        *,
        attempt_id: str,
        now: str,
        response_text: str,
        end_head_sha: str,
        end_dirty: bool,
        workspace_path: str,
        start_head_sha: str,
    ) -> ExecutionAttemptRecord:
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if not permit or not attempt or attempt["authorization_id"] != permit_id:
                raise ValueError("repair attempt binding is invalid")
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=?,response_text=?,end_head_sha=?,end_dirty=? WHERE attempt_id=?",
                (
                    AttemptStatus.SUCCEEDED.value,
                    now,
                    response_text,
                    end_head_sha,
                    int(end_dirty),
                    attempt_id,
                ),
            )
            db.execute(
                "UPDATE event_executions SET response_text=?,workspace_path=?,end_head_sha=?,end_dirty=? WHERE event_key=?",
                (
                    response_text,
                    workspace_path,
                    end_head_sha,
                    int(end_dirty),
                    permit["root_event_key"],
                ),
            )
            db.execute(
                "UPDATE review_repair_permits SET consumed_at=? WHERE permit_id=? AND consumed_at IS NULL",
                (now, permit_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.REVIEW_EXECUTION.value, now, permit["thread_id"]),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def mark_repair_attempt_failed(self, attempt_id: str, *, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=? WHERE attempt_id=?",
                (AttemptStatus.FAILED.value, now, attempt_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=(SELECT thread_id FROM execution_attempts WHERE attempt_id=?)",
                (WorkflowPhase.REPAIR_READY.value, now, attempt_id),
            )

    def publication_is_eligible(self, event_key: str) -> bool:
        row = self.connection.execute(
            "SELECT ee.status execution_status, s.thread_id, s.cycle_id, s.root_event_key, s.current_plan_id, s.phase, "
            "p.version plan_version, p.status plan_status, a.attempt_id, a.status attempt_status, "
            "a.plan_id attempt_plan, a.plan_version attempt_plan_version, a.root_event_key attempt_root, "
            "a.thread_id attempt_thread, a.cycle_id attempt_cycle, r.verdict, r.thread_id review_thread, "
            "r.cycle_id review_cycle, r.root_event_key review_root, r.plan_id review_plan, r.plan_version review_plan_version, r.attempt_id review_attempt "
            "FROM source_events e JOIN event_executions ee ON ee.event_key=e.event_key "
            "LEFT JOIN issue_workflow_state s ON s.thread_id=e.thread_id "
            "LEFT JOIN issue_plans p ON p.plan_id=s.current_plan_id "
            "LEFT JOIN execution_attempts a ON a.thread_id=s.thread_id AND a.cycle_id=s.cycle_id "
            "AND a.attempt_number=(SELECT MAX(a2.attempt_number) FROM execution_attempts a2 WHERE a2.thread_id=s.thread_id AND a2.cycle_id=s.cycle_id) "
            "LEFT JOIN execution_reviews r ON r.attempt_id=a.attempt_id WHERE e.event_key=?",
            (event_key,),
        ).fetchone()
        if row is None:
            return False
        return bool(
            row
            and row["execution_status"] == ExecutionStatus.SUCCEEDED.value
            and row["root_event_key"] == event_key
            and row["phase"] == WorkflowPhase.AWAITING_PUBLICATION.value
            and row["attempt_status"] == AttemptStatus.SUCCEEDED.value
            and row["attempt_plan"] == row["current_plan_id"]
            and row["attempt_plan_version"] == row["plan_version"]
            and row["attempt_root"] == row["root_event_key"]
            and row["attempt_thread"] == row["thread_id"]
            and row["attempt_cycle"] == row["cycle_id"]
            and row["plan_status"]
            in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
            and row["verdict"] == "ACCEPT"
            and row["review_thread"] == row["thread_id"]
            and row["review_cycle"] == row["cycle_id"]
            and row["review_root"] == row["root_event_key"]
            and row["review_plan"] == row["current_plan_id"]
            and row["review_plan_version"] == row["plan_version"]
            and row["review_attempt"] == row["attempt_id"]
        )

    def publication_for_event(self, event_key: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM event_publications WHERE event_key = ?", (event_key,)
        ).fetchone()
        return self._publication_record(row) if row else None

    def next_publication(
        self, event_key: str | None = None
    ) -> PublicationRecord | None:
        query = """SELECT ee.event_key FROM event_executions ee
                   JOIN source_events se ON se.event_key = ee.event_key
                   LEFT JOIN issue_workflow_state ws ON ws.thread_id = se.thread_id
                   LEFT JOIN event_publications ep ON ep.event_key = ee.event_key
                   LEFT JOIN execution_attempts ea ON ea.attempt_id = (
                     SELECT attempt_id FROM execution_attempts a2 WHERE a2.thread_id=ws.thread_id AND a2.cycle_id=ws.cycle_id ORDER BY attempt_number DESC LIMIT 1)
                   LEFT JOIN execution_reviews er ON er.attempt_id = ea.attempt_id
                   WHERE ee.status = ? AND ws.thread_id IS NOT NULL AND se.event_key = ws.root_event_key AND ws.phase = ? AND er.verdict = 'ACCEPT' AND ea.status = 'SUCCEEDED' AND ea.plan_id = ws.current_plan_id AND (ep.event_key IS NULL OR
                         ep.status IN (?, ?, ?, ?, ?))"""
        args: list[object] = [
            ExecutionStatus.SUCCEEDED.value,
            WorkflowPhase.AWAITING_PUBLICATION.value,
        ]
        args.extend(status.value for status in RESUMABLE_PUBLICATION_STATUSES)
        if event_key is not None:
            query += " AND ee.event_key = ?"
            args.append(event_key)
        query += " ORDER BY ee.completed_at, ee.event_key LIMIT 1"
        row = self.connection.execute(query, args).fetchone()
        if row is None:
            return None
        return self.ensure_publication(
            row["event_key"], now=datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )

    def ensure_publication(self, event_key: str, *, now: str) -> PublicationRecord:
        if not self.publication_is_eligible(event_key):
            raise ValueError("publication is blocked until a matching ACCEPT review")
        existing = self.publication_for_event(event_key)
        if existing:
            return existing
        with self.transaction() as db:
            if not self.publication_is_eligible(event_key):
                raise ValueError(
                    "publication eligibility changed during publication creation"
                )
            row = db.execute(
                """SELECT se.event_key, se.thread_id, se.repo_id, se.repo_full_name,
                          thread.issue_number, tw.branch_name
                   FROM source_events se
                   JOIN issue_threads thread ON thread.thread_id = se.thread_id
                   JOIN thread_workspaces tw ON tw.thread_id = se.thread_id
                   JOIN event_executions ee ON ee.event_key = se.event_key
                   WHERE se.event_key = ? AND ee.status = ?""",
                (event_key, ExecutionStatus.SUCCEEDED.value),
            ).fetchone()
            if row is None:
                raise ValueError("successful execution or workspace not found")
            db.execute(
                """INSERT INTO event_publications(
                   event_key, thread_id, repo_id, repo_full_name, issue_number,
                   status, branch_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_key,
                    row["thread_id"],
                    row["repo_id"],
                    row["repo_full_name"],
                    row["issue_number"],
                    PublicationStatus.PENDING.value,
                    row["branch_name"],
                    now,
                    now,
                ),
            )
        return self.publication_for_event(event_key)  # type: ignore[return-value]

    def update_publication(
        self, event_key: str, *, status: PublicationStatus, now: str, **fields: object
    ) -> PublicationRecord:
        allowed = {
            "local_commit_sha",
            "remote_commit_sha",
            "pr_number",
            "pr_url",
            "comment_id",
            "error_message",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown publication fields: {sorted(unknown)}")
        assignments = ["status = ?", "updated_at = ?"]
        values: list[object] = [status.value, now]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(event_key)
        with self.transaction() as db:
            if (
                db.execute(
                    "UPDATE event_publications SET "
                    f"{', '.join(assignments)} WHERE event_key = ?",
                    values,
                ).rowcount
                != 1
            ):
                raise ValueError("publication does not exist")
        return self.publication_for_event(event_key)  # type: ignore[return-value]

    def retry_publication(self, event_key: str, *, now: str) -> PublicationRecord:
        publication = self.publication_for_event(event_key)
        if publication is None:
            return self.ensure_publication(event_key, now=now)
        if publication.status != PublicationStatus.FAILED:
            raise ValueError("only failed publications can be retried")
        return self.update_publication(
            event_key, status=PublicationStatus.PENDING, now=now, error_message=None
        )

    @staticmethod
    def _publication_record(row: sqlite3.Row) -> PublicationRecord:
        values = dict(row)
        values["status"] = PublicationStatus(values["status"])
        return PublicationRecord(**values)

    def thread_workspace(self, thread_id: str) -> ThreadWorkspaceRecord | None:
        row = self.connection.execute(
            "SELECT * FROM thread_workspaces WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._workspace_record(row) if row else None

    def save_thread_workspace(self, record: ThreadWorkspaceRecord) -> None:
        with self.transaction() as db:
            thread = db.execute(
                "SELECT repo_id, issue_number FROM issue_threads WHERE thread_id = ?",
                (record.thread_id,),
            ).fetchone()
            if thread is None or (
                thread["repo_id"] != record.repo_id
                or thread["issue_number"] != record.issue_number
            ):
                raise ValueError("workspace metadata does not match the IssueThread")
            existing = db.execute(
                "SELECT * FROM thread_workspaces WHERE thread_id = ?",
                (record.thread_id,),
            ).fetchone()
            if existing:
                if self._workspace_record(existing) != record:
                    raise ValueError("workspace metadata conflicts with persisted data")
                return
            db.execute(
                """INSERT INTO thread_workspaces(
                   thread_id, repo_id, repo_full_name, issue_number,
                   source_repository_path, workspace_path, branch_name, base_commit,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.source_repository_path,
                    record.workspace_path,
                    record.branch_name,
                    record.base_commit,
                    record.created_at,
                    record.updated_at,
                ),
            )

    @staticmethod
    def _workspace_record(row: sqlite3.Row) -> ThreadWorkspaceRecord:
        return ThreadWorkspaceRecord(**dict(row))

    def _resolve_thread(
        self,
        db: sqlite3.Connection,
        event: SourceEvent,
        now: str,
        result: RecordBatchResult,
    ) -> str | None:
        if event.subject_kind == SubjectKind.PULL_REQUEST:
            row = db.execute(
                "SELECT thread_id FROM pr_thread_mappings "
                "WHERE repo_id = ? AND pr_number = ?",
                (event.repo_id, event.subject_number),
            ).fetchone()
            if row:
                result.events_routed += 1
                return row[0]
            result.pr_events_unrouted += 1
            return None
        thread_id = f"github:{event.repo_id}:issue:{event.subject_number}"
        existing = db.execute(
            "SELECT 1 FROM issue_threads WHERE repo_id = ? AND issue_number = ?",
            (event.repo_id, event.subject_number),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE issue_threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
        else:
            db.execute(
                """INSERT INTO issue_threads(thread_id, repo_id, repo_full_name,
                   issue_number, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    thread_id,
                    event.repo_id,
                    event.repo_full_name,
                    event.subject_number,
                    now,
                    now,
                ),
            )
            result.threads_created += 1
        result.events_routed += 1
        return thread_id

    def register_pr_mapping(self, repo_id: int, pr_number: int, thread_id: str) -> None:
        with self.transaction() as db:
            thread = db.execute(
                "SELECT repo_id FROM issue_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if thread is None or thread[0] != repo_id:
                raise ValueError("PR mapping thread must belong to the repository")
            existing = db.execute(
                "SELECT thread_id FROM pr_thread_mappings "
                "WHERE repo_id = ? AND pr_number = ?",
                (repo_id, pr_number),
            ).fetchone()
            if existing and existing[0] != thread_id:
                raise ValueError("PR mapping already points to another thread")
            db.execute(
                "INSERT INTO pr_thread_mappings(repo_id, pr_number, thread_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(repo_id, pr_number) DO NOTHING",
                (repo_id, pr_number, thread_id),
            )
            db.execute(
                "UPDATE source_events SET thread_id = ? WHERE repo_id = ? AND "
                "subject_kind = 'pull_request' AND subject_number = ? "
                "AND thread_id IS NULL",
                (thread_id, repo_id, pr_number),
            )

    def source_event(self, event_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_events WHERE event_key = ?", (event_key,)
        ).fetchone()

    def issue_thread(self, thread_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM issue_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()

    def source_events_for_thread(self, thread_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_events WHERE thread_id = ? "
            "ORDER BY source_updated_at, discovered_at, event_key",
            (thread_id,),
        ).fetchall()

    def workflow_state(self, thread_id: str) -> WorkflowStateRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_workflow_state WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._workflow_state_record(row) if row else None

    def save_workflow_state(self, record: WorkflowStateRecord) -> WorkflowStateRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO issue_workflow_state(
                   thread_id, repo_id, repo_full_name, issue_number, phase,
                   cycle_id, root_event_key, current_plan_id, mode, created_at,
                   updated_at, response_surface, response_subject_number,
                   response_comment_id, response_url, review_thread_root_id,
                   planning_feedback_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET phase=excluded.phase,
                   cycle_id=excluded.cycle_id, root_event_key=excluded.root_event_key,
                   current_plan_id=excluded.current_plan_id, mode=excluded.mode,
                   updated_at=excluded.updated_at,
                   response_surface=excluded.response_surface,
                   response_subject_number=excluded.response_subject_number,
                   response_comment_id=excluded.response_comment_id,
                   response_url=excluded.response_url,
                   review_thread_root_id=excluded.review_thread_root_id,
                   planning_feedback_event_key=excluded.planning_feedback_event_key""",
                (
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.phase.value,
                    record.cycle_id,
                    record.root_event_key,
                    record.current_plan_id,
                    record.mode.value,
                    record.created_at,
                    record.updated_at,
                    record.response_surface,
                    record.response_subject_number,
                    record.response_comment_id,
                    record.response_url,
                    record.review_thread_root_id,
                    record.planning_feedback_event_key,
                ),
            )
        return self.workflow_state(record.thread_id)  # type: ignore[return-value]

    def begin_workflow_cycle(
        self, plan: PlanRecord, state: WorkflowStateRecord, *, claimed_at: str
    ) -> PlanRecord:
        """Atomically consume the root and create its plan/state."""
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (state.thread_id,),
            ).fetchone()
            if existing is not None and existing["phase"] != WorkflowPhase.IDLE.value:
                raise ValueError("IssueThread already has an active workflow")
            if existing is not None and state.cycle_id != existing["cycle_id"] + 1:
                raise ValueError("workflow cycle must advance exactly one step")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    plan.root_event_key,
                    plan.thread_id,
                    plan.cycle_id,
                    InputPurpose.CYCLE_ROOT.value,
                    "CONSUMED",
                    claimed_at,
                    claimed_at,
                ),
            )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                """INSERT INTO issue_workflow_state(
                   thread_id, repo_id, repo_full_name, issue_number, phase,
                   cycle_id, root_event_key, current_plan_id, mode, created_at,
                   updated_at, response_surface, response_subject_number,
                   response_comment_id, response_url, review_thread_root_id,
                   planning_feedback_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET
                   repo_id=excluded.repo_id, repo_full_name=excluded.repo_full_name,
                   issue_number=excluded.issue_number, phase=excluded.phase,
                   cycle_id=excluded.cycle_id, root_event_key=excluded.root_event_key,
                   current_plan_id=excluded.current_plan_id, mode=excluded.mode,
                   updated_at=excluded.updated_at,
                   response_surface=excluded.response_surface,
                   response_subject_number=excluded.response_subject_number,
                   response_comment_id=excluded.response_comment_id,
                   response_url=excluded.response_url,
                   review_thread_root_id=excluded.review_thread_root_id,
                   planning_feedback_event_key=excluded.planning_feedback_event_key""",
                (
                    state.thread_id,
                    state.repo_id,
                    state.repo_full_name,
                    state.issue_number,
                    state.phase.value,
                    state.cycle_id,
                    state.root_event_key,
                    state.current_plan_id,
                    state.mode.value,
                    state.created_at,
                    state.updated_at,
                    state.response_surface,
                    state.response_subject_number,
                    state.response_comment_id,
                    state.response_url,
                    state.review_thread_root_id,
                    state.planning_feedback_event_key,
                ),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def approve_current_plan(
        self,
        *,
        event_key: str,
        author_login: str | None,
        permit: ExecutionPermit,
        approved_at: str,
    ) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (permit.thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit.plan_id,)
            ).fetchone()
            event = db.execute(
                "SELECT thread_id FROM source_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            if (
                state is None
                or plan is None
                or event is None
                or event["thread_id"] != permit.thread_id
                or state["phase"] != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value
                or state["current_plan_id"] != permit.plan_id
                or plan["status"] != PlanStatus.POSTED.value
            ):
                raise ValueError("current plan cannot be approved")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_key,
                    permit.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_APPROVAL.value,
                    "CONSUMED",
                    approved_at,
                    approved_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=event_key,
                thread_id=permit.thread_id,
                claimed_at=approved_at,
                purpose=InputPurpose.PLAN_APPROVAL,
            )
            db.execute(
                """UPDATE issue_plans SET status = ?, approved_at = ?,
                   approved_by = ?, approval_event_key = ? WHERE plan_id = ?""",
                (
                    PlanStatus.APPROVED.value,
                    approved_at,
                    author_login,
                    event_key,
                    permit.plan_id,
                ),
            )
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source, source_event_key, created_at,
                   consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    permit.permit_id,
                    permit.thread_id,
                    permit.cycle_id,
                    permit.plan_id,
                    permit.plan_version,
                    permit.root_event_key,
                    permit.source.value,
                    permit.source_event_key,
                    permit.created_at,
                    None,
                    None,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTION_READY.value, approved_at, permit.thread_id),
            )
        return self.permit(permit.permit_id)  # type: ignore[return-value]

    def mark_current_plan_posted(
        self,
        plan_id: str,
        *,
        comment_id: int,
        posted_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT p.*, w.phase AS workflow_phase, w.current_plan_id
                   FROM issue_plans p JOIN issue_workflow_state w
                   ON w.thread_id = p.thread_id WHERE p.plan_id = ?""",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown plan")
            if row["current_plan_id"] != plan_id:
                raise ValueError("plan is no longer current")
            if row["workflow_phase"] == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value:
                if row["status"] != PlanStatus.POSTED.value:
                    raise ValueError("posted workflow has inconsistent plan status")
                return self.plan(plan_id)  # type: ignore[return-value]
            if (
                row["workflow_phase"] == WorkflowPhase.PLANNING.value
                and row["status"] == PlanStatus.POSTED.value
            ):
                db.execute(
                    "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                    "WHERE thread_id = ?",
                    (
                        WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                        posted_at,
                        row["thread_id"],
                    ),
                )
                return self.plan(plan_id)  # type: ignore[return-value]
            if (
                row["workflow_phase"] != WorkflowPhase.PLANNING.value
                or row["status"] != PlanStatus.DRAFT.value
            ):
                raise ValueError("plan is not ready for publication")
            db.execute(
                "UPDATE issue_plans SET status = ?, posted_at = ?, "
                "posted_comment_id = ? WHERE plan_id = ?",
                (PlanStatus.POSTED.value, posted_at, comment_id, plan_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    posted_at,
                    row["thread_id"],
                ),
            )
        return self.plan(plan_id)  # type: ignore[return-value]

    def begin_plan_revision(self, event_key: str, *, now: str) -> WorkflowStateRecord:
        with self.transaction(immediate=True) as db:
            event = db.execute(
                "SELECT thread_id FROM source_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            if event is None:
                raise ValueError("unknown workflow input")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (event["thread_id"],),
            ).fetchone()
            if state is None or (
                state["phase"]
                not in (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    WorkflowPhase.EXECUTION_READY.value,
                )
                and not (
                    state["phase"] == WorkflowPhase.PLANNING.value
                    and state["planning_feedback_event_key"] == event_key
                )
            ):
                raise ValueError("feedback is not currently accepted for planning")
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, event["thread_id"], state["cycle_id"]),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, "
                "planning_feedback_event_key = ?, updated_at = ? WHERE thread_id = ?",
                (
                    WorkflowPhase.PLANNING.value,
                    event_key,
                    now,
                    event["thread_id"],
                ),
            )
        return self.workflow_state(event["thread_id"])  # type: ignore[return-value]

    def finish_plan_revision(
        self,
        *,
        plan: PlanRecord,
        feedback_event_key: str,
        finished_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (plan.thread_id,),
            ).fetchone()
            current = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?",
                (state["current_plan_id"],) if state else (None,),
            ).fetchone()
            if (
                state is None
                or current is None
                or state["phase"] != WorkflowPhase.PLANNING.value
                or state["planning_feedback_event_key"] != feedback_event_key
            ):
                raise ValueError("revision is not the active planning operation")
            db.execute(
                "UPDATE issue_plans SET status = ? WHERE plan_id = ?",
                (PlanStatus.SUPERSEDED.value, current["plan_id"]),
            )
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    feedback_event_key,
                    plan.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_FEEDBACK.value,
                    "CONSUMED",
                    finished_at,
                    finished_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=feedback_event_key,
                thread_id=plan.thread_id,
                claimed_at=finished_at,
                purpose=InputPurpose.PLAN_FEEDBACK,
            )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET planning_feedback_event_key = NULL, "
                "current_plan_id = ?, updated_at = ? WHERE thread_id = ?",
                (plan.plan_id, finished_at, plan.thread_id),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def revise_current_plan(
        self,
        *,
        event_key: str,
        plan: PlanRecord,
        revised_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (plan.thread_id,),
            ).fetchone()
            current = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?",
                (state["current_plan_id"],) if state else (None,),
            ).fetchone()
            if (
                state is None
                or current is None
                or state["phase"]
                not in (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    WorkflowPhase.EXECUTION_READY.value,
                )
            ):
                raise ValueError("current plan cannot be revised")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_key,
                    plan.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_FEEDBACK.value,
                    "CONSUMED",
                    revised_at,
                    revised_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=event_key,
                thread_id=plan.thread_id,
                claimed_at=revised_at,
                purpose=InputPurpose.PLAN_FEEDBACK,
            )
            db.execute(
                "UPDATE issue_plans SET status = ? WHERE plan_id = ?",
                (PlanStatus.SUPERSEDED.value, current["plan_id"]),
            )
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (revised_at, plan.thread_id, state["cycle_id"]),
            )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, current_plan_id = ?, "
                "updated_at = ? WHERE thread_id = ?",
                (
                    WorkflowPhase.PLANNING.value,
                    plan.plan_id,
                    revised_at,
                    plan.thread_id,
                ),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def authorize_current_plan(
        self,
        *,
        permit: ExecutionPermit,
        authorized_at: str,
    ) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (permit.thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit.plan_id,)
            ).fetchone()
            if (
                state is None
                or plan is None
                or state["phase"] != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value
                or state["current_plan_id"] != permit.plan_id
                or plan["status"] != PlanStatus.POSTED.value
            ):
                raise ValueError("current plan cannot be authorized")
            db.execute(
                "UPDATE issue_plans SET status = ?, approved_at = ? WHERE plan_id = ?",
                (PlanStatus.AUTO_APPROVED.value, authorized_at, permit.plan_id),
            )
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source, source_event_key, created_at,
                   consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    permit.permit_id,
                    permit.thread_id,
                    permit.cycle_id,
                    permit.plan_id,
                    permit.plan_version,
                    permit.root_event_key,
                    permit.source.value,
                    permit.source_event_key,
                    permit.created_at,
                    None,
                    None,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTION_READY.value, authorized_at, permit.thread_id),
            )
        return self.permit(permit.permit_id)  # type: ignore[return-value]

    @staticmethod
    def _workflow_state_record(row: sqlite3.Row) -> WorkflowStateRecord:
        values = dict(row)
        values["phase"] = WorkflowPhase(values["phase"])
        values["mode"] = WorkflowMode(values["mode"])
        return WorkflowStateRecord(**values)

    def plans_for_thread(self, thread_id: str) -> list[PlanRecord]:
        rows = self.connection.execute(
            "SELECT * FROM issue_plans WHERE thread_id = ? ORDER BY cycle_id, version",
            (thread_id,),
        ).fetchall()
        return [self._plan_record(row) for row in rows]

    def plan(self, plan_id: str) -> PlanRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return self._plan_record(row) if row else None

    def current_plan(self, thread_id: str) -> PlanRecord | None:
        state = self.workflow_state(thread_id)
        return (
            self.plan(state.current_plan_id)
            if state and state.current_plan_id
            else None
        )

    def insert_plan(self, record: PlanRecord) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.plan_id,
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.cycle_id,
                    record.version,
                    record.root_event_key,
                    record.plan_text,
                    record.status.value,
                    record.created_at,
                    record.posted_at,
                    record.posted_comment_id,
                    record.approved_at,
                    record.approved_by,
                    record.approval_event_key,
                ),
            )
        return self.plan(record.plan_id)  # type: ignore[return-value]

    def update_plan(self, plan_id: str, **fields: object) -> PlanRecord:
        allowed = {
            "plan_text",
            "status",
            "posted_at",
            "posted_comment_id",
            "approved_at",
            "approved_by",
            "approval_event_key",
        }
        if set(fields) - allowed:
            raise ValueError(f"unknown plan fields: {sorted(set(fields) - allowed)}")
        assignments: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value.value if isinstance(value, StrEnum) else value)
        if not assignments:
            return self.plan(plan_id)  # type: ignore[return-value]
        values.append(plan_id)
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    f"UPDATE issue_plans SET {', '.join(assignments)} "
                    "WHERE plan_id = ?",
                    values,
                ).rowcount
                != 1
            ):
                raise ValueError("unknown plan")
        return self.plan(plan_id)  # type: ignore[return-value]

    def permit(self, permit_id: str) -> ExecutionPermit | None:
        row = self.connection.execute(
            "SELECT * FROM execution_permits WHERE permit_id = ?", (permit_id,)
        ).fetchone()
        return self._permit_record(row) if row else None

    def permit_for_plan(self, plan_id: str) -> ExecutionPermit | None:
        row = self.connection.execute(
            "SELECT * FROM execution_permits WHERE plan_id = ? "
            "AND invalidated_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return self._permit_record(row) if row else None

    def insert_permit(self, record: ExecutionPermit) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source,
                   source_event_key, created_at, consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.permit_id,
                    record.thread_id,
                    record.cycle_id,
                    record.plan_id,
                    record.plan_version,
                    record.root_event_key,
                    record.source.value,
                    record.source_event_key,
                    record.created_at,
                    record.consumed_at,
                    record.invalidated_at,
                ),
            )
        return self.permit(record.permit_id)  # type: ignore[return-value]

    def consume_permit(self, permit_id: str, *, consumed_at: str) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    "UPDATE execution_permits SET consumed_at = ? "
                    "WHERE permit_id = ? AND consumed_at IS NULL "
                    "AND invalidated_at IS NULL",
                    (consumed_at, permit_id),
                ).rowcount
                != 1
            ):
                raise ValueError("permit is missing, consumed, or invalidated")
        return self.permit(permit_id)  # type: ignore[return-value]

    def bind_authorized_execution(
        self,
        permit_id: str,
        *,
        expected_thread_id: str,
        now: str,
    ) -> ClaimedEvent:
        """Atomically validate a permit, claim its exact root, and enter EXECUTING."""
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM execution_permits WHERE permit_id = ?", (permit_id,)
            ).fetchone()
            if permit is None or permit["invalidated_at"]:
                raise ValueError("execution permit is unavailable")
            if permit["thread_id"] != expected_thread_id:
                raise ValueError("execution permit belongs to another thread")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (expected_thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit["plan_id"],)
            ).fetchone()
            if state is None or plan is None:
                raise ValueError("execution permit references missing workflow data")
            if (
                state["phase"] != WorkflowPhase.EXECUTION_READY.value
                or state["cycle_id"] != permit["cycle_id"]
                or state["current_plan_id"] != permit["plan_id"]
                or plan["version"] != permit["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or plan["root_event_key"] != permit["root_event_key"]
            ):
                raise ValueError("execution permit is stale")
            row = db.execute(
                """SELECT se.*, thread.issue_number,
                          ee.status AS execution_status
                   FROM source_events se
                   JOIN issue_threads thread ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions ee ON ee.event_key = se.event_key
                   WHERE se.event_key = ?""",
                (permit["root_event_key"],),
            ).fetchone()
            if row is None or row["thread_id"] != expected_thread_id:
                raise ValueError("permit root event does not match the thread")
            pending = db.execute(
                """SELECT event_key, body FROM source_events
                   WHERE thread_id = ? AND event_key != ?
                   AND event_key NOT IN (
                     SELECT event_key FROM thread_input_consumptions
                   ) ORDER BY source_updated_at, discovered_at, event_key""",
                (expected_thread_id, permit["root_event_key"]),
            ).fetchall()
            for candidate in pending:
                if is_exact_agent_approval(candidate["body"]):
                    db.execute(
                        """INSERT INTO thread_input_consumptions(
                           event_key, thread_id, cycle_id, purpose, status,
                           claimed_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            candidate["event_key"],
                            expected_thread_id,
                            state["cycle_id"],
                            InputPurpose.STALE_PLAN_APPROVAL.value,
                            "CONSUMED",
                            now,
                            now,
                        ),
                    )
                    self._resolve_workflow_control(
                        db,
                        event_key=candidate["event_key"],
                        thread_id=expected_thread_id,
                        claimed_at=now,
                        purpose=InputPurpose.STALE_PLAN_APPROVAL,
                    )
                    continue
                if starts_with_agent_invocation(candidate["body"]):
                    raise PendingWorkflowInputError(candidate["event_key"])
            if row["execution_status"] not in (
                None,
                ExecutionStatus.RETRY_PENDING.value,
            ):
                raise ValueError("permit root event is not claimable")
            if (
                permit["consumed_at"]
                and row["execution_status"] != ExecutionStatus.RETRY_PENDING.value
            ):
                raise ValueError("execution permit was already consumed")
            self._assert_event_ordering(db, row)
            retrying = row["execution_status"] == ExecutionStatus.RETRY_PENDING.value
            if retrying:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, row["event_key"]),
                )
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (
                        row["event_key"],
                        expected_thread_id,
                        ExecutionStatus.RUNNING.value,
                        now,
                    ),
                )
            db.execute(
                "UPDATE execution_permits SET consumed_at = ? "
                "WHERE permit_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, permit_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTING.value, now, expected_thread_id),
            )
            db.execute(
                """INSERT OR IGNORE INTO execution_attempts(
                   attempt_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, attempt_number, kind, repair_round,
                   authorization_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"attempt-{permit_id}",
                    expected_thread_id,
                    permit["cycle_id"],
                    permit["plan_id"],
                    permit["plan_version"],
                    permit["root_event_key"],
                    1,
                    AttemptKind.INITIAL.value,
                    0,
                    permit_id,
                    AttemptStatus.RUNNING.value,
                    now,
                ),
            )
            return self._claimed_event(row, retrying=retrying)

    def invalidate_permits(self, thread_id: str, cycle_id: int, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, thread_id, cycle_id),
            )

    def input_consumption(self, event_key: str) -> WorkflowInputRecord | None:
        row = self.connection.execute(
            "SELECT * FROM thread_input_consumptions WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return self._input_record(row) if row else None

    def consume_input(
        self,
        event_key: str,
        *,
        thread_id: str,
        cycle_id: int,
        purpose: InputPurpose,
        claimed_at: str,
        status: str = "CONSUMED",
    ) -> WorkflowInputRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    event_key,
                    thread_id,
                    cycle_id,
                    purpose.value,
                    status,
                    claimed_at,
                    claimed_at,
                ),
            )
            if purpose != InputPurpose.CYCLE_ROOT:
                self._resolve_workflow_control(
                    db,
                    event_key=event_key,
                    thread_id=thread_id,
                    claimed_at=claimed_at,
                    purpose=purpose,
                )
        return self.input_consumption(event_key)  # type: ignore[return-value]

    @staticmethod
    def _resolve_workflow_control(
        db: sqlite3.Connection,
        *,
        event_key: str,
        thread_id: str,
        claimed_at: str,
        purpose: InputPurpose,
    ) -> None:
        existing = db.execute(
            "SELECT status FROM event_executions WHERE event_key = ?", (event_key,)
        ).fetchone()
        if existing is not None and existing[0] not in (ExecutionStatus.SKIPPED.value,):
            raise ValueError("workflow control input already has code execution state")
        db.execute(
            """INSERT INTO event_executions(
               event_key, thread_id, status, attempt_count, started_at,
               completed_at, error_message)
               VALUES (?, ?, ?, 0, ?, ?, ?)
               ON CONFLICT(event_key) DO UPDATE SET status = excluded.status,
               completed_at = excluded.completed_at,
               error_message = excluded.error_message""",
            (
                event_key,
                thread_id,
                ExecutionStatus.SKIPPED.value,
                claimed_at,
                claimed_at,
                f"workflow control input: {purpose.value}",
            ),
        )

    def claim_event_for_execution(
        self,
        event_key: str,
        *,
        expected_thread_id: str,
        now: str,
    ) -> ClaimedEvent:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT se.*, thread.issue_number,
                          ee.status AS execution_status
                   FROM source_events se
                   JOIN issue_threads thread ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions ee ON ee.event_key = se.event_key
                   WHERE se.event_key = ?""",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("target execution event does not exist")
            if row["thread_id"] != expected_thread_id:
                raise ValueError("target execution event belongs to another thread")
            status = row["execution_status"]
            if status not in (None, ExecutionStatus.RETRY_PENDING.value):
                raise ValueError("target execution event is not claimable")
            self._assert_event_ordering(db, row)
            if status == ExecutionStatus.RETRY_PENDING.value:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, event_key),
                )
                retrying = True
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (event_key, expected_thread_id, ExecutionStatus.RUNNING.value, now),
                )
                retrying = False
            return self._claimed_event(row, retrying=retrying)

    @staticmethod
    def _assert_event_ordering(db: sqlite3.Connection, row: sqlite3.Row) -> None:
        earlier = db.execute(
            """SELECT se.event_key, ee.status, ep.status AS publication_status
               FROM source_events se
               LEFT JOIN event_executions ee ON ee.event_key = se.event_key
               LEFT JOIN event_publications ep ON ep.event_key = se.event_key
               WHERE se.thread_id = ? AND (
                 se.source_updated_at < ? OR
                 (se.source_updated_at = ? AND se.discovered_at < ?) OR
                 (se.source_updated_at = ? AND se.discovered_at = ? AND
                  se.event_key < ?)
               ) ORDER BY se.source_updated_at, se.discovered_at, se.event_key""",
            (
                row["thread_id"],
                row["source_updated_at"],
                row["source_updated_at"],
                row["discovered_at"],
                row["source_updated_at"],
                row["discovered_at"],
                row["event_key"],
            ),
        ).fetchall()
        for item in earlier:
            if item["status"] == ExecutionStatus.SKIPPED.value:
                continue
            if item["status"] == ExecutionStatus.SUCCEEDED.value and item[
                "publication_status"
            ] in (
                PublicationStatus.COMPLETED.value,
                PublicationStatus.NO_CHANGES.value,
            ):
                continue
            raise ValueError("earlier IssueThread event is unresolved")

    @staticmethod
    def _claimed_event(row: sqlite3.Row, *, retrying: bool) -> ClaimedEvent:
        return ClaimedEvent(
            event_key=row["event_key"],
            thread_id=row["thread_id"],
            repo_id=row["repo_id"],
            repo_full_name=row["repo_full_name"],
            issue_number=row["issue_number"],
            body=row["body"],
            workspace_path=None,
            retrying=retrying,
            origin_surface=row["origin_surface"],
            path=row["path"],
            line=row["line"],
            start_line=row["start_line"],
            side=row["side"],
            start_side=row["start_side"],
            diff_hunk=row["diff_hunk"],
            commit_id=row["commit_id"],
            original_commit_id=row["original_commit_id"],
            in_reply_to_id=row["in_reply_to_id"],
            pull_request_review_id=row["pull_request_review_id"],
            review_thread_root_id=row["review_thread_root_id"],
        )

    def unconsumed_inputs(self, thread_id: str, *, after_event_key: str | None = None):
        rows = self.source_events_for_thread(thread_id)
        if after_event_key is not None:
            keys = [row["event_key"] for row in rows]
            if after_event_key in keys:
                rows = rows[keys.index(after_event_key) + 1 :]
        return [row for row in rows if self.input_consumption(row["event_key"]) is None]

    @staticmethod
    def _plan_record(row: sqlite3.Row) -> PlanRecord:
        values = dict(row)
        values["status"] = PlanStatus(values["status"])
        return PlanRecord(**values)

    @staticmethod
    def _permit_record(row: sqlite3.Row) -> ExecutionPermit:
        values = dict(row)
        if values.get("root_event_key") is None:
            raise ValueError("execution permit migration could not recover root event")
        values["source"] = PermitSource(values["source"])
        return ExecutionPermit(**values)

    @staticmethod
    def _input_record(row: sqlite3.Row) -> WorkflowInputRecord:
        values = dict(row)
        values["purpose"] = InputPurpose(values["purpose"])
        return WorkflowInputRecord(**values)

    def events(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_events ORDER BY discovered_at, event_key"
        ).fetchall()

    def threads(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM issue_threads ORDER BY repo_id, issue_number"
        ).fetchall()
