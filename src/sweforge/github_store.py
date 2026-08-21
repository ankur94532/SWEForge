"""Durable SQLite persistence for GitHub ingestion."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .github_models import SourceEvent

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
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SQLiteGitHubStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
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
    ) -> int:
        inserted = 0
        with self.transaction() as db:
            for event in events:
                thread_id = self._resolve_thread(db, event, polled_at)
                cursor = db.execute(
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
                inserted += cursor.rowcount
            db.execute(
                """INSERT INTO poll_cursors(repo_id, stream, since, etag,
                   last_successful_poll_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repo_id, stream) DO UPDATE SET since=excluded.since,
                   etag=excluded.etag,
                   last_successful_poll_at=excluded.last_successful_poll_at""",
                (repo_id, stream, since, etag, polled_at),
            )
        return inserted

    def _resolve_thread(self, db: sqlite3.Connection, event: SourceEvent, now: str):
        if event.subject_kind.value == "pull_request":
            row = db.execute(
                "SELECT thread_id FROM pr_thread_mappings "
                "WHERE repo_id = ? AND pr_number = ?",
                (event.repo_id, event.subject_number),
            ).fetchone()
            return row[0] if row else None
        thread_id = f"github:{event.repo_id}:issue:{event.subject_number}"
        db.execute(
            """INSERT INTO issue_threads(thread_id, repo_id, repo_full_name,
               issue_number, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, issue_number) DO UPDATE SET
               updated_at=excluded.updated_at""",
            (
                thread_id,
                event.repo_id,
                event.repo_full_name,
                event.subject_number,
                now,
                now,
            ),
        )
        return thread_id

    def register_pr_mapping(self, repo_id: int, pr_number: int, thread_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO pr_thread_mappings(repo_id, pr_number, thread_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(repo_id, pr_number) DO UPDATE SET "
                "thread_id=excluded.thread_id",
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
