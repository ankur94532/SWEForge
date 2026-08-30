"""Resolved-issue memory: historical cases, retrieval and containment.

Cases are generated automatically for finalized lifecycles, keyed by lifecycle
rather than by issue, and retrieved as bounded clues.  They are never
repository truth and never authorization.
"""

import json

import pytest
from test_publication_identity import THREAD_ID, Harness, approval, source_event

from sweforge.github_store import (
    MAX_ISSUE_RESOLUTION_ATTEMPTS,
    IssueResolutionStatus,
    SQLiteGitHubStore,
    WorkflowPhase,
    resolution_id_for,
)
from sweforge.issue_resolution import IssueResolutionCase
from sweforge.workflow import UNCONFIGURED_ISSUE_RESOLUTION


class NullMemoryStore:
    def get(self, *args, **kwargs):
        return None

    def put(self, *args, **kwargs):
        return None

    def search(self, *args, **kwargs):
        return []


def offline_case(**overrides):
    defaults = {
        "useful": True,
        "task_summary": "Fix tenant search failures.",
        "symptom_summary": "Search requests returned 500 for newly added tenants.",
        "root_cause": "Tenant configuration skipped the new region mapping.",
        "fix_summary": "Added the mapping and made the config fallback explicit.",
        "affected_components": ["config loader", "search routing"],
        "validation_summary": "tenant tests and the regression search suite",
        "search_terms": ["tenant", "region mapping", "search 500"],
        "limitations": "",
    }
    return IssueResolutionCase(**{**defaults, **overrides})


def patch_curator(monkeypatch, case, calls=None):
    def fake(*, model, evidence):
        if calls is not None:
            calls.append((model, evidence))
        if isinstance(case, Exception):
            raise case
        return case

    monkeypatch.setattr("sweforge.workflow.curate_issue_resolution", fake)


def finalized_lifecycle(tmp_path, *, title="Tenant search fails", body="500s"):
    harness = Harness(tmp_path)
    harness.store.upsert_issue_metadata(
        repo_id=harness.repo.repo_id,
        issue_number=7,
        title=title,
        body=body,
        observed_at="2026-01-01T00:00:00Z",
    )
    origin = source_event(
        harness.repo, "1", "@agent fix tenant search", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("fix.txt")
    assert harness.publisher.publish_one().status == "COMPLETED"
    harness.engine.memory_learner = None
    return harness, origin


def advance(harness, **overrides):
    kwargs = {
        "thread_id": THREAD_ID,
        "model": "planning-sonnet",
        "review_model": "review-sonnet",
        "memory_model": None,
        "resolution_model": "provider:strong",
        "repo_paths": {harness.repo.full_name: harness.source},
        "workspace_root": harness.tmp_path / "workspaces",
        "memory_store": NullMemoryStore(),
        "execute_kwargs": harness.execute_kwargs("fix.txt"),
    }
    kwargs.update(overrides)
    return harness.engine.advance(**kwargs)


def test_finalized_lifecycle_creates_exactly_one_case(tmp_path, monkeypatch):
    harness, origin = finalized_lifecycle(tmp_path)
    calls = []
    patch_curator(monkeypatch, offline_case(), calls)
    assert advance(harness).phase is WorkflowPhase.IDLE

    record = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == IssueResolutionStatus.COMPLETED.value
    assert record.resolution_id == resolution_id_for(
        thread_id=THREAD_ID, cycle_id=1, root_input_id=origin.event_key
    )
    # Issue title and description snapshot are preserved.
    assert record.issue_title == "Tenant search fails"
    assert record.issue_description_snapshot == "500s"
    # Structured RCA persisted.
    assert record.root_cause.startswith("Tenant configuration skipped")
    assert record.fix_summary.startswith("Added the mapping")
    assert json.loads(record.affected_components_json) == [
        "config loader",
        "search routing",
    ]
    assert record.validation_summary == "tenant tests and the regression search suite"
    # Bounded changed-file representation, not a raw diff.
    assert json.loads(record.changed_files_json) == ["fix.txt"]
    # Provenance.
    assert record.plan_id and record.publication_id and record.execution_id
    assert record.review_id
    assert record.pr_number == 41
    assert record.commit_sha
    assert len(calls) == 1

    # Replay is idempotent: one case, no duplicate curator call.
    assert advance(harness).phase is WorkflowPhase.IDLE
    assert len(calls) == 1
    assert (
        harness.store.connection.execute(
            "SELECT count(*) FROM issue_resolution_memory"
        ).fetchone()[0]
        == 1
    )
    harness.store.close()


def test_resolution_identity_is_stable_across_restart(tmp_path, monkeypatch):
    harness, origin = finalized_lifecycle(tmp_path)
    patch_curator(monkeypatch, offline_case())
    advance(harness)
    before = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    harness.reopen()
    after = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert before == after
    harness.store.close()


def test_unsuccessful_work_never_produces_a_solved_case(tmp_path):
    """Only finalization creates a case; blocked work must not claim a fix."""
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    assert harness.advance("fix.txt").phase is WorkflowPhase.REVIEW_EXECUTION
    from sweforge.reviewer import ExecutionReviewResult

    harness.engine.reviewer = lambda **_: ExecutionReviewResult(
        verdict="BLOCKED", summary="not acceptable"
    )
    harness.advance("fix.txt")
    assert harness.store.workflow_state(THREAD_ID).phase is not WorkflowPhase.IDLE
    assert (
        harness.store.connection.execute(
            "SELECT count(*) FROM issue_resolution_memory"
        ).fetchone()[0]
        == 0
    )
    harness.store.close()


def test_second_cycle_from_one_source_event_is_a_distinct_case(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.store.upsert_issue_metadata(
        repo_id=harness.repo.repo_id,
        issue_number=7,
        title="Two tasks",
        body="do both",
        observed_at="2026-01-01T00:00:00Z",
    )
    origin = source_event(
        harness.repo, "1", "@agent do both things", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    first = harness.store.defer_followup(
        source_event_key=origin.event_key,
        thread_id=THREAD_ID,
        originating_cycle_id=0,
        queued_at="2026-01-01T00:00:02Z",
        residual_text="update the README",
    )
    second = harness.store.defer_followup(
        source_event_key=origin.event_key,
        thread_id=THREAD_ID,
        originating_cycle_id=0,
        queued_at="2026-01-01T00:00:03Z",
        residual_text="add tests",
    )
    harness.engine.memory_learner = None
    patch_curator(monkeypatch, offline_case())

    resolutions = []
    for index, (deferred, filename, approval_id, when) in enumerate(
        (
            (first, "readme.txt", "3", "2026-01-01T01:00:00Z"),
            (second, "tests.txt", "4", "2026-01-01T02:00:00Z"),
        ),
        start=1,
    ):
        harness.plan_and_approve(
            event_key=origin.event_key,
            root_input_id=deferred.deferred_id,
            approval_event=approval(harness.repo, approval_id, when),
        )
        harness.run_to_publication(filename)
        assert harness.publisher.publish_one().status == "COMPLETED"
        advance(harness, execute_kwargs=harness.execute_kwargs(filename))
        resolutions.append(
            harness.store.issue_resolution_for_cycle(
                thread_id=THREAD_ID,
                cycle_id=index,
                root_event_key=origin.event_key,
                root_input_id=deferred.deferred_id,
            )
        )

    assert resolutions[0] is not None and resolutions[1] is not None
    assert resolutions[0].resolution_id != resolutions[1].resolution_id
    # Both keep the same SourceEvent and issue for grouping.
    assert {item.source_event_key for item in resolutions} == {origin.event_key}
    assert {item.issue_number for item in resolutions} == {7}
    assert {item.cycle_id for item in resolutions} == {1, 2}
    harness.store.close()


def test_no_useful_case_is_distinct_from_failure_and_no_curator(tmp_path, monkeypatch):
    harness, origin = finalized_lifecycle(tmp_path)
    patch_curator(monkeypatch, offline_case(useful=False))
    advance(harness)
    record = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == IssueResolutionStatus.NO_CASE.value
    assert record.error_message is None
    harness.store.close()


def test_unconfigured_curator_is_recorded_distinctly(tmp_path):
    harness, origin = finalized_lifecycle(tmp_path)
    advance(harness, resolution_model=None, memory_model=None)
    record = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == IssueResolutionStatus.NO_CASE.value
    assert record.error_message == UNCONFIGURED_ISSUE_RESOLUTION
    harness.store.close()


def test_failing_curator_retries_boundedly_then_yields(tmp_path, monkeypatch):
    harness, origin = finalized_lifecycle(tmp_path)
    calls: list[tuple] = []
    patch_curator(monkeypatch, RuntimeError("curator exploded"), calls)
    harness.record(
        source_event(harness.repo, "9", "@agent next task", "2026-01-05T00:00:00Z")
    )
    for _ in range(MAX_ISSUE_RESOLUTION_ATTEMPTS + 2):
        advance(harness)
    assert len(calls) == MAX_ISSUE_RESOLUTION_ATTEMPTS
    record = harness.store.issue_resolution_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == IssueResolutionStatus.FAILED.value
    assert record.attempt_count == MAX_ISSUE_RESOLUTION_ATTEMPTS
    assert "curator exploded" in record.error_message
    assert harness.store.pending_issue_resolution(THREAD_ID) is None
    # A permanent failure must not block the next actionable issue.
    assert harness.store.workflow_state(THREAD_ID).cycle_id == 2
    harness.store.close()


def test_hard_crash_during_curation_consumes_attempt_budget(tmp_path, monkeypatch):
    """The attempt is claimed durably before the external call."""
    harness, origin = finalized_lifecycle(tmp_path)
    resolution_id = resolution_id_for(
        thread_id=THREAD_ID, cycle_id=1, root_input_id=origin.event_key
    )

    class HardCrash(BaseException):
        """Not an Exception: models a process-level abort mid-curation."""

    def crashing(*, model, evidence):
        raise HardCrash()

    monkeypatch.setattr("sweforge.workflow.curate_issue_resolution", crashing)
    with pytest.raises(HardCrash):
        advance(harness)
    assert harness.store.issue_resolution(resolution_id).attempt_count == 1
    with pytest.raises(HardCrash):
        advance(harness)
    assert harness.store.issue_resolution(resolution_id).attempt_count == 2
    harness.store.close()


def test_hard_crash_during_repository_curation_consumes_budget(tmp_path, monkeypatch):
    harness, origin = finalized_lifecycle(tmp_path)
    from sweforge.github_store import memory_learning_id_for

    learning_id = memory_learning_id_for(
        thread_id=THREAD_ID, cycle_id=1, root_input_id=origin.event_key
    )

    class HardCrash(BaseException):
        pass

    def crashing(**kwargs):
        raise HardCrash()

    monkeypatch.setattr("sweforge.workflow.curate_repository_memory", crashing)
    for expected in (1, 2):
        with pytest.raises(HardCrash):
            advance(harness, memory_model="provider:memory")
        assert (
            harness.store.memory_learning_for_id(learning_id).attempt_count == expected
        )
    harness.store.close()


def test_issue_title_and_body_are_snapshotted_during_ingestion(tmp_path):
    """Historical learning must not need a network call to recover context."""
    from sweforge.github_client import PollResponse
    from sweforge.github_models import RepositoryRef
    from sweforge.github_poller import GitHubPoller

    class Client:
        def __init__(self):
            self.issue_calls = 0

        def repository(self, full_name):
            return RepositoryRef(1, full_name, "main")

        def issues(self, repo, since, etag):
            return PollResponse(
                items=[
                    {
                        "number": 7,
                        "title": "Search 500s for new tenants",
                        "body": "@agent fix the tenant search",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "created_at": "2026-01-01T00:00:00Z",
                        "id": 1,
                        "user": {"login": "octocat"},
                        "html_url": "https://example/1",
                    }
                ]
            )

        def issue_comments(self, repo, since, etag):
            return PollResponse(
                items=[
                    {
                        "id": 2,
                        "body": "@agent also add tests",
                        "issue_url": "https://api.github.com/repos/o/r/issues/9",
                        "updated_at": "2026-01-01T00:05:00Z",
                        "created_at": "2026-01-01T00:05:00Z",
                        "user": {"login": "octocat"},
                        "html_url": "https://example/2",
                    }
                ]
            )

        def review_comments(self, repo, since, etag):
            return PollResponse(items=[])

        def pull_request_reviews(self, repo, since, etag):
            return PollResponse(items=[])

        def issue(self, repo, number):
            self.issue_calls += 1
            return {
                "number": number,
                "title": "Flaky tenant tests",
                "body": "Tests fail intermittently.",
                "updated_at": "2026-01-01T00:04:00Z",
            }

    store = SQLiteGitHubStore(tmp_path / "state.db")
    client = Client()
    GitHubPoller(client=client, store=store).poll(["example/repo"])

    issue = store.issue_metadata(repo_id=1, issue_number=7)
    assert issue.title == "Search 500s for new tenants"
    assert issue.body == "@agent fix the tenant search"
    # The comment stream reuses the classification fetch; no extra call.
    commented = store.issue_metadata(repo_id=1, issue_number=9)
    assert commented.title == "Flaky tenant tests"
    assert commented.body == "Tests fail intermittently."
    assert client.issue_calls == 1
    store.close()


def test_issue_metadata_keeps_the_newest_observation(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(1, "example/repo", "now")
    store.upsert_issue_metadata(
        repo_id=1,
        issue_number=7,
        title="Old title",
        body="old",
        observed_at="2026-01-01T00:00:00Z",
    )
    store.upsert_issue_metadata(
        repo_id=1,
        issue_number=7,
        title="New title",
        body="new",
        observed_at="2026-01-02T00:00:00Z",
    )
    assert store.issue_metadata(repo_id=1, issue_number=7).title == "New title"
    # An older observation must not overwrite a newer snapshot.
    store.upsert_issue_metadata(
        repo_id=1,
        issue_number=7,
        title="Stale title",
        body="stale",
        observed_at="2025-01-01T00:00:00Z",
    )
    assert store.issue_metadata(repo_id=1, issue_number=7).title == "New title"
    store.close()


def test_new_tables_appear_on_reopened_pre_existing_databases(tmp_path):
    """The dual-memory tables are additive and idempotent on reopen."""
    path = tmp_path / "legacy.db"
    first = SQLiteGitHubStore(path)
    first.connection.executescript(
        """DROP TABLE issue_resolution_memory;
           DROP TABLE repo_memory_candidates;
           DROP TABLE issue_metadata;
           DROP TABLE issue_resolution_fts;"""
    )
    first.connection.commit()
    first.close()

    migrated = SQLiteGitHubStore(path)
    tables = {
        row[0]
        for row in migrated.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "issue_resolution_memory",
        "repo_memory_candidates",
        "issue_metadata",
        "issue_resolution_fts",
    } <= tables
    assert migrated.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    migrated.close()

    fresh = SQLiteGitHubStore(tmp_path / "fresh.db")
    again = SQLiteGitHubStore(path)
    for table in (
        "issue_resolution_memory",
        "repo_memory_candidates",
        "issue_metadata",
    ):
        assert [
            row[1] for row in fresh.connection.execute(f"PRAGMA table_info({table})")
        ] == [row[1] for row in again.connection.execute(f"PRAGMA table_info({table})")]
    again.close()
    fresh.close()
