"""Fail-closed campaign isolation checks for durable scenario state."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    path: Path
    row_counts: dict[str, int]
    digest: str
    checks: int
    violations: tuple[dict[str, Any], ...]

    def payload(self, *, workspace: Path) -> dict[str, Any]:
        return {
            "path": str(self.path.relative_to(workspace)),
            "row_counts": self.row_counts,
            "sha256": self.digest,
        }


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    scenario_id: str
    repetition: int
    workspace: Path
    databases: tuple[DatabaseSnapshot, ...]

    @property
    def checks(self) -> int:
        return sum(item.checks for item in self.databases)

    @property
    def violations(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "scenario_id": self.scenario_id,
                "repetition": self.repetition,
                "database": str(database.path.relative_to(self.workspace)),
                **violation,
            }
            for database in self.databases
            for violation in database.violations
        )

    def payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "repetition": self.repetition,
            "workspace": str(self.workspace),
            "databases": [
                item.payload(workspace=self.workspace) for item in self.databases
            ],
        }


def snapshot_database(path: Path) -> DatabaseSnapshot:
    """Observe row counts and reject references outside canonical identities."""
    target = path.resolve()
    if not target.is_file():
        raise RuntimeError(f"state database is not observable: {target}")

    checks = 0
    violations: list[dict[str, Any]] = []
    with _readonly(target) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if not tables:
            raise RuntimeError(f"state database has no observable tables: {target}")
        required = {"repositories", "issue_threads"}
        missing = sorted(required - set(tables))
        if missing:
            raise RuntimeError(
                f"state database lacks canonical identity tables {missing}: {target}"
            )

        allowed_threads = {
            str(row[0])
            for row in connection.execute(
                "SELECT thread_id FROM issue_threads WHERE thread_id IS NOT NULL"
            )
        }
        allowed_repositories = {
            int(row[0])
            for row in connection.execute(
                "SELECT repo_id FROM repositories WHERE repo_id IS NOT NULL"
            )
        }
        row_counts: dict[str, int] = {}
        for table in tables:
            quoted = _quoted(table)
            row_counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            )
            checks += 1
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted})")
            }
            if "thread_id" in columns and table != "issue_threads":
                observed = {
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT DISTINCT thread_id FROM {quoted} "
                        "WHERE thread_id IS NOT NULL"
                    )
                }
                foreign = sorted(observed - allowed_threads)
                checks += 1
                if foreign:
                    violations.append(
                        {
                            "kind": "foreign_thread_reference",
                            "table": table,
                            "values": foreign,
                        }
                    )
            if "repo_id" in columns and table != "repositories":
                observed = {
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT DISTINCT repo_id FROM {quoted} "
                        "WHERE repo_id IS NOT NULL"
                    )
                }
                foreign = sorted(observed - allowed_repositories)
                checks += 1
                if foreign:
                    violations.append(
                        {
                            "kind": "foreign_repository_reference",
                            "table": table,
                            "values": foreign,
                        }
                    )

    return DatabaseSnapshot(
        path=target,
        row_counts=row_counts,
        digest=_digest(target),
        checks=checks,
        violations=tuple(violations),
    )


def snapshot_run(scenario_id: str, repetition: int, workspace: Path) -> RunSnapshot:
    """Capture every state database owned by one otherwise-isolated run."""
    root = workspace.resolve()
    if not root.is_dir():
        raise RuntimeError(f"scenario workspace is not observable: {root}")
    paths = sorted(path for path in root.rglob("state.db") if path.is_file())
    if not paths:
        raise RuntimeError(f"scenario produced no observable state.db under {root}")
    return RunSnapshot(
        scenario_id=scenario_id,
        repetition=repetition,
        workspace=root,
        databases=tuple(snapshot_database(path) for path in paths),
    )


def audit_snapshot(
    snapshot: RunSnapshot, *, after_scenario_id: str
) -> tuple[int, list]:
    """Reobserve an earlier run and report any later durable mutation."""
    checks = 0
    violations: list[dict[str, Any]] = []
    for expected in snapshot.databases:
        current = snapshot_database(expected.path)
        checks += current.checks
        common = {
            "scenario_id": snapshot.scenario_id,
            "repetition": snapshot.repetition,
            "after_scenario_id": after_scenario_id,
            "database": str(expected.path.relative_to(snapshot.workspace)),
        }
        violations.extend({**common, **item} for item in current.violations)
        if current.row_counts != expected.row_counts:
            violations.append(
                {
                    **common,
                    "kind": "row_count_changed",
                    "expected": expected.row_counts,
                    "actual": current.row_counts,
                }
            )
        elif current.digest != expected.digest:
            violations.append(
                {
                    **common,
                    "kind": "durable_content_changed",
                    "expected_sha256": expected.digest,
                    "actual_sha256": current.digest,
                }
            )
    return checks, violations
