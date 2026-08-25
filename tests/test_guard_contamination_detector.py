"""The campaign contamination detector must observe real durable state."""

import sqlite3

import pytest

from acceptance.runner import cli
from acceptance.runner.contamination import (
    audit_snapshot,
    snapshot_database,
    snapshot_run,
)
from acceptance.runner.exit_conditions import evaluate_exit_conditions


def _database(path, *, foreign: bool = False):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE repositories(repo_id INTEGER PRIMARY KEY);
            CREATE TABLE issue_threads(
                thread_id TEXT PRIMARY KEY,
                repo_id INTEGER NOT NULL
            );
            CREATE TABLE durable_work(thread_id TEXT, repo_id INTEGER);
            INSERT INTO repositories VALUES(1);
            INSERT INTO issue_threads VALUES('own-thread', 1);
            INSERT INTO durable_work VALUES('own-thread', 1);
            """
        )
        if foreign:
            connection.execute("INSERT INTO durable_work VALUES('foreign-thread', 2)")


def test_detector_passes_a_scoped_database_and_counts_real_tables(tmp_path):
    """Positive control: the detector read all three non-empty tables."""
    path = tmp_path / "state.db"
    _database(path)

    snapshot = snapshot_database(path)

    assert snapshot.violations == ()
    assert snapshot.row_counts == {
        "durable_work": 1,
        "issue_threads": 1,
        "repositories": 1,
    }
    assert snapshot.checks >= len(snapshot.row_counts)


def test_detector_reports_foreign_thread_and_repository_references(tmp_path):
    path = tmp_path / "state.db"
    _database(path, foreign=True)

    violations = snapshot_database(path).violations

    assert {item["kind"] for item in violations} == {
        "foreign_thread_reference",
        "foreign_repository_reference",
    }
    assert all(item["table"] == "durable_work" for item in violations)


def test_later_run_mutation_of_an_earlier_database_is_a_violation(tmp_path):
    workspace = tmp_path / "pass-1-s1" / "workspace"
    workspace.mkdir(parents=True)
    path = workspace / "state.db"
    _database(path)
    baseline = snapshot_run("S1", 1, workspace)

    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO durable_work VALUES('own-thread', 1)")

    checks, violations = audit_snapshot(baseline, after_scenario_id="S2")

    assert checks > 0
    assert [item["kind"] for item in violations] == ["row_count_changed"]
    assert violations[0]["scenario_id"] == "S1"
    assert violations[0]["after_scenario_id"] == "S2"


def test_detector_refuses_to_pass_without_a_state_database(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="no observable state.db"):
        snapshot_run("S1", 1, workspace)


def test_campaign_wires_positive_detector_evidence_without_claiming_full_coverage(
    tmp_path,
):
    status = cli.execute_campaign(
        ["S14"],
        repetitions=1,
        status_path=tmp_path / "campaign-status.json",
        runs_root=tmp_path / "runs",
        campaign_id="contamination-wiring",
    )

    assert status["contamination"]["checks"] > 0
    assert status["contamination"]["violations"] == []
    assert status["contamination"]["observed_ids"] == ["S14"]
    condition = next(
        item
        for item in evaluate_exit_conditions(status)["conditions"]
        if item["condition_id"] == "E6"
    )
    assert condition["state"] == "CANNOT_EVALUATE"
