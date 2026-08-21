"""Durable SQLite persistence for GitHub ingestion."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .github_models import SourceEvent, SubjectKind

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
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL,
    author_login TEXT,
    body TEXT NOT NULL,
    html_url TEXT,
    thread_id TEXT REFERENCES issue_threads(thread_id),
    discovered_at TEXT NOT NULL
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
                       source_updated_at, subject_kind, subject_number, author_login,
                       body, html_url, thread_id, discovered_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_key,
                        event.repo_id,
                        event.repo_full_name,
                        event.source_kind.value,
                        event.source_id,
                        event.source_updated_at,
                        event.subject_kind.value,
                        event.subject_number,
                        event.author_login,
                        event.body,
                        event.html_url,
                        thread_id,
                        polled_at,
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
                   LEFT JOIN event_publications ep ON ep.event_key = ee.event_key
                   WHERE ee.status = ? AND (ep.event_key IS NULL OR
                         ep.status IN (?, ?, ?, ?, ?))"""
        args: list[object] = [ExecutionStatus.SUCCEEDED.value]
        args.extend(status.value for status in RESUMABLE_PUBLICATION_STATUSES)
        if event_key is not None:
            query += " AND ep.event_key = ?"
            args.append(event_key)
        query += " ORDER BY ee.completed_at, ee.event_key LIMIT 1"
        row = self.connection.execute(query, args).fetchone()
        if row is None:
            return None
        return self.ensure_publication(
            row["event_key"], now=datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )

    def ensure_publication(self, event_key: str, *, now: str) -> PublicationRecord:
        existing = self.publication_for_event(event_key)
        if existing:
            return existing
        with self.transaction() as db:
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

    def events(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_events ORDER BY discovered_at, event_key"
        ).fetchall()

    def threads(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM issue_threads ORDER BY repo_id, issue_number"
        ).fetchall()
