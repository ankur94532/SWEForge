"""Durable ledger of acceptance probe calls.

The original probe server kept its counters in a JSON file. That cannot serve
S15-S17, which kill and restart the dispatcher: a probe's call count has to
survive the process that made the calls. SQLite gives that plus concurrent
readers, which the JSON file with an flock did not.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LEDGER_ENV = "SWEFORGE_PROBE_LEDGER"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    repo_id INTEGER NOT NULL,
    repo_full_name TEXT NOT NULL,
    result_class TEXT NOT NULL,
    call_number INTEGER NOT NULL,
    args_received TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS probe_calls_tool
    ON probe_calls(tool_name, operation_id);
"""


@dataclass(frozen=True, slots=True)
class ProbeCall:
    tool_name: str
    operation_id: str
    repo_id: int
    repo_full_name: str
    result_class: str
    call_number: int
    args_received: dict

    @property
    def point(self) -> str:
        """Named like a fault point so scenario reporting reads uniformly."""
        return self.tool_name


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    target = Path(path or os.environ[LEDGER_ENV])
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA)
    return connection


def record(
    connection: sqlite3.Connection,
    *,
    tool_name: str,
    operation_id: str,
    repo_id: int,
    repo_full_name: str,
    result_class: str,
    args_received: dict | None = None,
) -> int:
    """Append one call and return its 1-based number for this operation.

    Counting and appending happen in one immediate transaction so two workers
    calling the same probe cannot both believe they were first.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM probe_calls WHERE tool_name=? AND operation_id=?",
            (tool_name, operation_id),
        ).fetchone()
        call_number = int(row[0]) + 1
        connection.execute(
            "INSERT INTO probe_calls (recorded_at,tool_name,operation_id,repo_id,"
            "repo_full_name,result_class,call_number,args_received) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                tool_name,
                operation_id,
                repo_id,
                repo_full_name,
                result_class,
                call_number,
                json.dumps(args_received or {}, sort_keys=True),
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return call_number


class ProbeLedger:
    """Read-only view for scenario predicates and the Observation bundle."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self._path.exists():
            raise RuntimeError(
                f"probe ledger {self._path} does not exist; the scenario cannot "
                "observe probe calls and must not report success"
            )
        connection = connect(self._path)
        try:
            return list(connection.execute(sql, params))
        finally:
            connection.close()

    def calls(self, tool_name: str | None = None) -> list[ProbeCall]:
        sql = "SELECT * FROM probe_calls"
        params: tuple = ()
        if tool_name is not None:
            sql += " WHERE tool_name=?"
            params = (tool_name,)
        sql += " ORDER BY id"
        return [
            ProbeCall(
                tool_name=row["tool_name"],
                operation_id=row["operation_id"],
                repo_id=row["repo_id"],
                repo_full_name=row["repo_full_name"],
                result_class=row["result_class"],
                call_number=row["call_number"],
                args_received=json.loads(row["args_received"]),
            )
            for row in self._rows(sql, params)
        ]

    def count(self, tool_name: str, operation_id: str | None = None) -> int:
        if operation_id is None:
            return len(self.calls(tool_name))
        return len(
            [
                item
                for item in self.calls(tool_name)
                if item.operation_id == operation_id
            ]
        )

    def __iter__(self):
        return iter(self.calls())
