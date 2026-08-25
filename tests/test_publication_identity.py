"""Post-execution lifecycle identity: publication and repository learning.

One SourceEvent can back several logical workflow inputs.  Everything after
REVIEW ACCEPT -- publication, the execution-summary comment, finalization and
repository-memory learning -- must therefore belong to the exact
thread/cycle/logical-input lifecycle, never to the SourceEvent.
"""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from harness.github_fake import FakeGitHub

from sweforge.execution import ClarificationRequestProposal
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_publisher import GitHubPublisher, publication_comment_markers
from sweforge.github_store import (
    AmbiguousLifecycleError,
    PublicationStatus,
    SQLiteGitHubStore,
    WorkflowPhase,
    execution_id_for,
    memory_learning_id_for,
    publication_id_for,
)
from sweforge.reviewer import ExecutionReviewResult
from sweforge.workflow import WorkflowEngine

THREAD_ID = "github:1:issue:7"


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


class TokenProvider:
    def token_for(self, repository, profile):
        return "installation-token"


class Clock:
    def __init__(self) -> None:
        self.minute = 0

    def __call__(self) -> str:
        self.minute += 1
        return f"2026-01-02T{self.minute // 60:02d}:{self.minute % 60:02d}:00Z"


def source_event(repo, source_id, body, created):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=source_id,
        source_updated_at=created,
        source_created_at=created,
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body=body,
        html_url=None,
    )


def repository_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("base\n")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    return source


class Harness:
    """Drives complete offline lifecycles against one durable database."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.source = repository_checkout(tmp_path)
        self.remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.repo = RepositoryRef(1, "example/repo")
        self.client = FakeGitHub()
        self.clock = Clock()
        self.runner_inputs: list[str] = []
        self.db = tmp_path / "state.db"
        self.store = SQLiteGitHubStore(self.db)
        self.store.upsert_repository(1, self.repo.full_name, "now")
        self.open_engine()

    def open_engine(self):
        self.engine = WorkflowEngine(
            store=self.store,
            client=self.client,
            planner=lambda **kwargs: "Requirements:\n1. apply the requested change",
            reviewer=lambda **_: ExecutionReviewResult(verdict="ACCEPT", summary="ok"),
            memory_learner=lambda **_: [],
            clock=self.clock,
        )
        self.publisher = GitHubPublisher(
            store=self.store,
            client=self.client,
            token_provider=TokenProvider(),
            lock_root=self.tmp_path / "locks",
            remote_url_factory=lambda _: f"file://{self.remote}",
        )

    def reopen(self):
        self.store.close()
        self.store = SQLiteGitHubStore(self.db)
        self.open_engine()

    def record(self, *events):
        self.store.record_batch(
            1, "issue_comments", list(events), since="now", etag=None, polled_at="now"
        )

    def execute_kwargs(self, filename):
        def runner(**kwargs):
            self.runner_inputs.append(kwargs["task"])
            Path(kwargs["worktree"], filename).write_text(f"{filename}\n")
            return f"wrote {filename}"

        return {
            "model": "cheap-haiku",
            "repo_paths": {self.repo.full_name: self.source},
            "workspace_root": self.tmp_path / "workspaces",
            "lock_root": self.tmp_path / "locks",
            "checkpointer": object(),
            "runner": runner,
        }

    def advance(self, filename):
        return self.engine.advance(
            thread_id=THREAD_ID,
            model="planning-sonnet",
            review_model="review-sonnet",
            repo_paths={self.repo.full_name: self.source},
            workspace_root=self.tmp_path / "workspaces",
            execute_kwargs=self.execute_kwargs(filename),
        )

    def plan_and_approve(self, *, event_key, root_input_id, approval_event):
        plan = self.engine.plan_event(
            event_key=event_key,
            model="planning-sonnet",
            repo_paths={self.repo.full_name: self.source},
            workspace_root=self.tmp_path / "workspaces",
            root_input_id=root_input_id,
        )
        self.engine.publish_plan(plan.plan_id)
        self.record(approval_event)
        self.engine.approve(event_key=approval_event.event_key)
        return plan

    def run_to_publication(self, filename):
        assert self.advance(filename).phase is WorkflowPhase.REVIEW_EXECUTION
        assert self.advance(filename).phase is WorkflowPhase.AWAITING_PUBLICATION

    def finalize(self, filename):
        result = self.publisher.publish_one()
        assert result.status in {"COMPLETED", "NO_CHANGES"}, result.error
        assert self.advance(filename).phase is WorkflowPhase.IDLE
        return result


def summary_marker(publication):
    return f"<!-- sweforge:execution-summary:{publication.publication_id} -->"


def approval(repo, source_id, created):
    return source_event(repo, source_id, "@agent approve", created)


def two_logical_inputs(harness, event_key):
    first = harness.store.defer_followup(
        source_event_key=event_key,
        thread_id=THREAD_ID,
        originating_cycle_id=0,
        queued_at="2026-01-01T00:00:02Z",
        residual_text="update the README",
    )
    second = harness.store.defer_followup(
        source_event_key=event_key,
        thread_id=THREAD_ID,
        originating_cycle_id=0,
        queued_at="2026-01-01T00:00:03Z",
        residual_text="add tests",
    )
    return first, second


def test_one_source_event_backs_two_independent_publication_lifecycles(tmp_path):
    harness = Harness(tmp_path)
    origin = source_event(
        harness.repo,
        "1",
        "@agent update the README and add tests",
        "2026-01-01T00:00:00Z",
    )
    harness.record(origin)
    before = dict(harness.store.source_event(origin.event_key))
    first, second = two_logical_inputs(harness, origin.event_key)
    assert first.deferred_id != second.deferred_id

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=first.deferred_id,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("readme.txt")
    publication_one = harness.store.publication_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=first.deferred_id,
    )
    assert publication_one is None
    first_result = harness.finalize("readme.txt")
    publication_one = harness.store.publication_for_id(first_result.publication_id)
    learning_one = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=first.deferred_id,
    )
    summary_one = summary_marker(publication_one)
    assert harness.client.bodies_with(summary_one)
    plan_one = harness.store.plan_for_cycle(THREAD_ID, 1)
    assert plan_one.status.value == "EXECUTED"
    assert plan_one.root_input_id == first.deferred_id

    # The queued sibling stayed queued while the first lifecycle ran.
    assert (
        harness.store.deferred_followup(
            origin.event_key, deferred_id=second.deferred_id
        ).status
        == "QUEUED"
    )

    # A restart must rediscover the queued sibling as the next logical input.
    harness.reopen()
    pending = harness.engine.next_workflow_input(THREAD_ID)
    assert pending is not None
    assert pending.input_id == second.deferred_id
    assert pending.source_event_key == origin.event_key

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=second.deferred_id,
        approval_event=approval(harness.repo, "3", "2026-01-01T02:00:00Z"),
    )
    harness.run_to_publication("tests.txt")
    second_result = harness.finalize("tests.txt")
    publication_two = harness.store.publication_for_id(second_result.publication_id)
    learning_two = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=2,
        root_event_key=origin.event_key,
        root_input_id=second.deferred_id,
    )
    summary_two = summary_marker(publication_two)

    execution_one = execution_id_for(
        thread_id=THREAD_ID, cycle_id=1, root_input_id=first.deferred_id
    )
    execution_two = execution_id_for(
        thread_id=THREAD_ID, cycle_id=2, root_input_id=second.deferred_id
    )

    # Distinct lifecycle identity at every post-execution layer ...
    assert first.deferred_id != second.deferred_id
    assert execution_one != execution_two
    assert publication_one.publication_id != publication_two.publication_id
    assert learning_one.learning_id != learning_two.learning_id
    assert summary_one != summary_two

    # ... while every one of them retains the same SourceEvent provenance.
    assert publication_one.source_event_key == origin.event_key
    assert publication_two.source_event_key == origin.event_key
    assert learning_one.source_event_key == origin.event_key
    assert learning_two.source_event_key == origin.event_key
    assert {
        record.publication_id
        for record in harness.store.publications_for_event(origin.event_key)
    } == {publication_one.publication_id, publication_two.publication_id}
    assert {
        record.learning_id
        for record in harness.store.memory_learnings_for_event(origin.event_key)
    } == {learning_one.learning_id, learning_two.learning_id}
    with pytest.raises(AmbiguousLifecycleError):
        harness.store.publication_for_event(origin.event_key)
    with pytest.raises(AmbiguousLifecycleError):
        harness.store.repo_memory_learning(origin.event_key)
    with pytest.raises(AmbiguousLifecycleError, match="backs 2 publications"):
        harness.store.resolve_publication_id(origin.event_key)

    # Only each cycle's own plan was marked executed.
    plan_two = harness.store.plan_for_cycle(THREAD_ID, 2)
    assert plan_two.plan_id != plan_one.plan_id
    assert plan_two.status.value == "EXECUTED"
    assert plan_two.root_input_id == second.deferred_id
    assert harness.store.plan(plan_one.plan_id).status.value == "EXECUTED"

    # P1 does not suppress P2, and each summary comment exists exactly once.
    assert publication_one.status is PublicationStatus.COMPLETED
    assert publication_two.status is PublicationStatus.COMPLETED
    assert len(harness.client.bodies_with(summary_one)) == 1
    assert len(harness.client.bodies_with(summary_two)) == 1
    assert learning_one.status == "NO_UPDATE"
    assert learning_two.status == "NO_UPDATE"

    # Both retries are idempotent: no new publication, PR or comment.
    comments_before = len(harness.client.created)
    pulls_before = len(harness.client.pulls)
    assert harness.publisher.publish_one().status == "NO_WORK"
    assert (
        harness.publisher.publish_one(publication_one.publication_id).status
        == "NO_WORK"
    )
    assert (
        harness.publisher.publish_one(publication_two.publication_id).status
        == "NO_WORK"
    )
    assert len(harness.client.created) == comments_before
    assert len(harness.client.pulls) == pulls_before

    # Each logical input executed exactly once.
    assert len(harness.runner_inputs) == 2
    assert "update the README" in harness.runner_inputs[0]
    assert "add tests" in harness.runner_inputs[1]
    executions = harness.store.connection.execute(
        "SELECT execution_id, attempt_count FROM logical_executions ORDER BY cycle_id"
    ).fetchall()
    assert [row["execution_id"] for row in executions] == [
        execution_one,
        execution_two,
    ]
    assert [row["attempt_count"] for row in executions] == [1, 1]

    # The SourceEvent itself was never mutated.
    assert dict(harness.store.source_event(origin.event_key)) == before
    harness.store.close()


def test_stale_publication_cannot_finalize_or_mutate_a_newer_cycle(tmp_path):
    harness = Harness(tmp_path)
    origin = source_event(
        harness.repo, "1", "@agent two things", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    first, second = two_logical_inputs(harness, origin.event_key)

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=first.deferred_id,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("readme.txt")
    stale = harness.finalize("readme.txt").publication_id

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=second.deferred_id,
        approval_event=approval(harness.repo, "3", "2026-01-01T02:00:00Z"),
    )
    harness.run_to_publication("tests.txt")
    current = harness.store.eligible_publication_id(THREAD_ID)
    assert current is not None and current != stale

    # While cycle 2 is current, the cycle 1 publication is inert.
    assert not harness.store.publication_is_eligible(stale)
    assert harness.publisher.publish_one(stale).status == "NO_WORK"
    with pytest.raises(ValueError, match="stale"):
        harness.store.finalize_publication(stale, now="never")
    with pytest.raises(ValueError, match="blocked until review ACCEPT"):
        harness.engine.complete_publication(
            thread_id=THREAD_ID,
            publication_id=stale,
            publication_status="COMPLETED",
        )

    # Nothing about cycle 2 moved.
    state = harness.store.workflow_state(THREAD_ID)
    assert state.phase is WorkflowPhase.AWAITING_PUBLICATION
    assert state.cycle_id == 2
    assert harness.store.current_plan(THREAD_ID).status.value in {
        "APPROVED",
        "AUTO_APPROVED",
    }
    assert harness.store.publication_for_id(current) is None
    assert (
        harness.store.memory_learning_for_cycle(
            thread_id=THREAD_ID,
            cycle_id=2,
            root_event_key=origin.event_key,
            root_input_id=second.deferred_id,
        )
        is None
    )
    summary_two = f"<!-- sweforge:execution-summary:{current} -->"
    assert harness.client.bodies_with(summary_two) == []

    # The stale record itself stays readable and unchanged.
    assert harness.store.publication_for_id(stale).status is PublicationStatus.COMPLETED
    harness.store.close()


def test_no_changes_lifecycle_does_not_collide_with_a_later_real_publication(tmp_path):
    harness = Harness(tmp_path)
    origin = source_event(
        harness.repo, "1", "@agent two things", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    first, second = two_logical_inputs(harness, origin.event_key)

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=first.deferred_id,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )

    def no_op_runner(**kwargs):
        harness.runner_inputs.append(kwargs["task"])
        return "nothing to do"

    kwargs = harness.execute_kwargs("unused.txt")
    kwargs["runner"] = no_op_runner
    assert (
        harness.engine.advance(
            thread_id=THREAD_ID,
            model="planning-sonnet",
            review_model="review-sonnet",
            repo_paths={harness.repo.full_name: harness.source},
            workspace_root=harness.tmp_path / "workspaces",
            execute_kwargs=kwargs,
        ).phase
        is WorkflowPhase.REVIEW_EXECUTION
    )
    assert (
        harness.engine.advance(
            thread_id=THREAD_ID,
            model="planning-sonnet",
            review_model="review-sonnet",
            repo_paths={harness.repo.full_name: harness.source},
            workspace_root=harness.tmp_path / "workspaces",
            execute_kwargs=kwargs,
        ).phase
        is WorkflowPhase.AWAITING_PUBLICATION
    )
    empty = harness.publisher.publish_one()
    assert empty.status == "NO_CHANGES"
    assert harness.advance("unused.txt").phase is WorkflowPhase.IDLE

    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=second.deferred_id,
        approval_event=approval(harness.repo, "3", "2026-01-01T02:00:00Z"),
    )
    harness.run_to_publication("tests.txt")
    real = harness.finalize("tests.txt")
    assert real.status == "COMPLETED"
    assert real.publication_id != empty.publication_id
    assert (
        harness.store.publication_for_id(empty.publication_id).status
        is PublicationStatus.NO_CHANGES
    )
    assert harness.store.publication_for_id(real.publication_id).pr_number is not None
    harness.store.close()


def test_legacy_event_keyed_rows_migrate_onto_lifecycle_identity(tmp_path):
    db = tmp_path / "legacy.db"
    store = SQLiteGitHubStore(db)
    repo = RepositoryRef(1, "example/repo")
    store.upsert_repository(1, repo.full_name, "now")
    origin = source_event(repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    store.record_batch(
        1, "issue_comments", [origin], since="now", etag=None, polled_at="now"
    )
    store.connection.execute(
        """INSERT INTO issue_workflow_state(
           thread_id, repo_id, repo_full_name, issue_number, phase, cycle_id,
           root_event_key, current_plan_id, mode, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            THREAD_ID,
            1,
            repo.full_name,
            7,
            "IDLE",
            3,
            origin.event_key,
            None,
            "INTERACTIVE",
            "now",
            "now",
        ),
    )
    # Recreate the pre-migration shape and seed one row in each table.
    store.connection.executescript(
        """DROP TABLE logical_publications;
           DROP TABLE repo_memory_learning;
           CREATE TABLE event_publications (
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
           CREATE TABLE repo_memory_learning (
               event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
               thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
               cycle_id INTEGER NOT NULL,
               repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
               status TEXT NOT NULL,
               accepted_candidates INTEGER NOT NULL DEFAULT 0,
               rejected_candidates INTEGER NOT NULL DEFAULT 0,
               proposal_json TEXT NOT NULL DEFAULT '{}',
               error_message TEXT,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           );"""
    )
    store.connection.execute(
        """INSERT INTO event_publications(
           event_key, thread_id, repo_id, repo_full_name, issue_number, status,
           branch_name, local_commit_sha, remote_commit_sha, pr_number, pr_url,
           comment_id, error_message, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            origin.event_key,
            THREAD_ID,
            1,
            repo.full_name,
            7,
            "COMPLETED",
            "sweforge/issue-7",
            "localsha",
            "remotesha",
            41,
            "https://github.com/example/repo/pull/41",
            99,
            None,
            "created",
            "updated",
        ),
    )
    store.connection.execute(
        """INSERT INTO repo_memory_learning(
           event_key, thread_id, cycle_id, repo_id, status, accepted_candidates,
           rejected_candidates, proposal_json, error_message, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            origin.event_key,
            THREAD_ID,
            3,
            1,
            "UPDATED",
            2,
            1,
            '[{"candidate_id": "one"}]',
            None,
            "created",
            "updated",
        ),
    )
    store.connection.commit()
    store.close()

    migrated = SQLiteGitHubStore(db)
    expected_publication = publication_id_for(
        thread_id=THREAD_ID, cycle_id=3, root_input_id=origin.event_key
    )
    expected_learning = memory_learning_id_for(
        thread_id=THREAD_ID, cycle_id=3, root_input_id=origin.event_key
    )
    publication = migrated.publication_for_id(expected_publication)
    assert publication is not None
    assert publication.source_event_key == origin.event_key
    assert publication.root_input_id == origin.event_key
    assert publication.cycle_id == 3
    assert publication.status is PublicationStatus.COMPLETED
    assert publication.local_commit_sha == "localsha"
    assert publication.remote_commit_sha == "remotesha"
    assert publication.pr_number == 41
    assert publication.comment_id == 99
    assert publication.created_at == "created"
    assert migrated.publication_for_event(origin.event_key) == publication

    learning = migrated.memory_learning_for_id(expected_learning)
    assert learning is not None
    assert learning.source_event_key == origin.event_key
    assert learning.root_input_id == origin.event_key
    assert learning.cycle_id == 3
    assert learning.status == "UPDATED"
    assert learning.accepted_candidates == 2
    assert learning.rejected_candidates == 1
    assert learning.proposal_json == '[{"candidate_id": "one"}]'
    assert migrated.repo_memory_learning(origin.event_key) == learning
    assert publication_comment_markers(publication) == (
        f"<!-- sweforge:publication:{expected_publication} -->",
        f"<!-- sweforge:publication:{origin.event_key} -->",
    )
    assert (
        migrated.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("event_publications",),
        ).fetchone()
        is None
    )
    assert migrated.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    migrated.close()

    # Reopening is idempotent and yields the same identities.
    again = SQLiteGitHubStore(db)
    assert again.publication_for_id(expected_publication) == publication
    assert again.memory_learning_for_id(expected_learning) == learning
    assert len(again.publications_for_event(origin.event_key)) == 1
    assert len(again.memory_learnings_for_event(origin.event_key)) == 1
    again.close()

    # A fresh database and the migrated one are equivalent for the new APIs.
    fresh = SQLiteGitHubStore(tmp_path / "fresh.db")
    reopened = SQLiteGitHubStore(db)
    for table in ("logical_publications", "repo_memory_learning"):
        assert [
            row[1] for row in fresh.connection.execute(f"PRAGMA table_info({table})")
        ] == [
            row[1] for row in reopened.connection.execute(f"PRAGMA table_info({table})")
        ]
    assert fresh.publications_for_event(origin.event_key) == []
    assert fresh.memory_learnings_for_event(origin.event_key) == []
    reopened.close()
    fresh.close()


def test_clarification_residual_gets_its_own_publication_and_learning(tmp_path):
    """The motivating mixed answer, carried through to post-publication state.

    A human SourceEvent both answers the open clarification for the running
    cycle and adds a new task.  The answer resumes that cycle, and the residual
    becomes a separate logical input whose publication, summary comment and
    learning identity are independent of the cycle it came from.  Interrupt
    mechanics themselves are covered by test_human_input_routing.
    """
    harness = Harness(tmp_path)
    root = source_event(
        harness.repo, "1", "@agent fix the scheduler", "2026-01-01T00:00:00Z"
    )
    harness.record(root)
    harness.plan_and_approve(
        event_key=root.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )

    # The execution pauses on an application-owned clarification.
    state = harness.store.workflow_state(THREAD_ID)
    clarification = harness.engine._persist_clarification(
        state=state,
        proposal=ClarificationRequestProposal(
            question="Which region?",
            reason="Two valid regions were found.",
            answer_type="CHOICE",
            choices=("us-east-1", "eu-west-1"),
            occurrence_key="tool-call-1",
        ),
    )
    harness.store.save_workflow_state(
        replace(state, phase=WorkflowPhase.WAITING_FOR_INPUT)
    )

    answer = source_event(
        harness.repo,
        "3",
        "@agent us-east-1, also update the README",
        "2026-01-01T02:00:00Z",
    )
    harness.record(answer)
    harness.engine._resolve_open_clarification(harness.store.workflow_state(THREAD_ID))

    resolved = harness.store.clarification(clarification.clarification_id)
    assert resolved.status == "ANSWERED"
    assert resolved.answer_event_key == answer.event_key
    residual = harness.store.deferred_followup(answer.event_key)
    assert residual is not None
    assert residual.residual_text == "update the README"
    assert residual.deferred_id != answer.event_key
    assert (
        harness.store.workflow_state(THREAD_ID).phase is WorkflowPhase.EXECUTION_READY
    )

    # Cycle 1 completes on its own logical input, the original root event.
    harness.run_to_publication("scheduler.txt")
    cycle_one = harness.finalize("scheduler.txt")
    publication_one = harness.store.publication_for_id(cycle_one.publication_id)
    assert publication_one.root_input_id == root.event_key
    assert publication_one.source_event_key == root.event_key
    learning_one = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=root.event_key,
        root_input_id=None,
    )
    assert learning_one is not None

    # The residual is the next logical cycle.
    harness.reopen()
    pending = harness.engine.next_workflow_input(THREAD_ID)
    assert pending is not None
    assert pending.input_id == residual.deferred_id
    assert pending.source_event_key == answer.event_key

    harness.plan_and_approve(
        event_key=answer.event_key,
        root_input_id=residual.deferred_id,
        approval_event=approval(harness.repo, "4", "2026-01-01T03:00:00Z"),
    )
    assert harness.store.current_plan(THREAD_ID).root_input_id == residual.deferred_id
    harness.run_to_publication("readme.txt")
    assert "update the README" in harness.runner_inputs[-1]
    cycle_two = harness.finalize("readme.txt")
    publication_two = harness.store.publication_for_id(cycle_two.publication_id)
    learning_two = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=2,
        root_event_key=answer.event_key,
        root_input_id=residual.deferred_id,
    )

    assert publication_two.publication_id != publication_one.publication_id
    assert publication_two.root_input_id == residual.deferred_id
    assert publication_two.source_event_key == answer.event_key
    assert learning_two.learning_id != learning_one.learning_id
    assert learning_two.source_event_key == answer.event_key

    summary_one = summary_marker(publication_one)
    summary_two = summary_marker(publication_two)
    assert summary_one != summary_two
    assert len(harness.client.bodies_with(summary_one)) == 1
    assert len(harness.client.bodies_with(summary_two)) == 1

    # The residual lifecycle reached IDLE exactly once.
    comments = len(harness.client.created)
    assert harness.advance("readme.txt").phase is WorkflowPhase.IDLE
    assert len(harness.client.created) == comments
    assert harness.store.workflow_state(THREAD_ID).cycle_id == 2
    assert (
        harness.store.deferred_followup(
            answer.event_key, deferred_id=residual.deferred_id
        ).status
        == "CONSUMED"
    )
    assert harness.engine.next_workflow_input(THREAD_ID) is None
    harness.store.close()


def test_publication_crash_boundaries_resume_the_same_identity(tmp_path):
    """Every durable checkpoint is reconciled instead of redone on retry."""
    harness = Harness(tmp_path)
    origin = source_event(
        harness.repo, "1", "@agent fix the scheduler", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("scheduler.txt")

    # A: the publication row exists before any Git or GitHub side effect.
    created = harness.store.ensure_publication(thread_id=THREAD_ID, now="a")
    assert created.status is PublicationStatus.PENDING
    assert harness.store.ensure_publication(thread_id=THREAD_ID, now="a2") == created
    publication_id = created.publication_id

    result = harness.publisher.publish_one()
    assert result.status == "COMPLETED", result.error
    assert result.publication_id == publication_id
    published = harness.store.publication_for_id(publication_id)
    commit_sha = published.local_commit_sha
    comment_id = published.comment_id
    pr_number = published.pr_number
    comments = len(harness.client.created)
    pulls = len(harness.client.pulls)

    # E: the summary comment exists but its id was never persisted.
    harness.store.update_publication(
        publication_id,
        status=PublicationStatus.PR_CREATED,
        now="e",
        comment_id=None,
    )
    assert harness.publisher.publish_one().status == "COMPLETED"
    assert harness.store.publication_for_id(publication_id).comment_id == comment_id
    assert len(harness.client.created) == comments

    # C/D: the branch was pushed but the PR identity was never persisted.
    harness.store.update_publication(
        publication_id,
        status=PublicationStatus.PUSHED,
        now="d",
        pr_number=None,
        pr_url=None,
    )
    assert harness.publisher.publish_one().status == "COMPLETED"
    assert harness.store.publication_for_id(publication_id).pr_number == pr_number
    assert len(harness.client.pulls) == pulls

    # B: the commit was made but its sha was never persisted.
    harness.store.update_publication(
        publication_id,
        status=PublicationStatus.PENDING,
        now="b",
        local_commit_sha=None,
    )
    assert harness.publisher.publish_one().status == "COMPLETED"
    reconciled = harness.store.publication_for_id(publication_id)
    assert reconciled.local_commit_sha == commit_sha
    assert reconciled.pr_number == pr_number
    assert len(harness.client.created) == comments
    assert len(harness.client.pulls) == pulls

    # F: publication is complete but the workflow has not been finalized yet.
    assert harness.store.workflow_state(THREAD_ID).phase is (
        WorkflowPhase.AWAITING_PUBLICATION
    )
    assert harness.advance("scheduler.txt").phase is WorkflowPhase.IDLE
    assert len(harness.store.publications_for_event(origin.event_key)) == 1
    assert len(harness.store.memory_learnings_for_event(origin.event_key)) == 1
    harness.store.close()
