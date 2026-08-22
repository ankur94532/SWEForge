from datetime import UTC, datetime
from pathlib import Path

import pytest

from sweforge.execution import recover_stale
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore, WorkflowPhase
from sweforge.reviewer import (
    ExecutionReviewResult,
    ReviewerContext,
    build_reviewer,
    render_review_evidence,
)
from sweforge.workflow import WorkflowEngine


def event(repo, source_id, body, created):
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


def seed(store, repo, events):
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id, "issue_comments", events, since="now", etag=None, polled_at="now"
    )


def execution_ready_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )

    git("init", "-q")
    (source / "README.md").write_text("base\n")
    git("add", "README.md")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    repo = RepositoryRef(1, "example/repo")
    root = event(repo, "1", "@agent fix the bug", "2026-01-01T00:00:00Z")
    approval = event(repo, "2", "@agent approve", "2026-01-01T00:01:00Z")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    seed(store, repo, [root, approval])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    engine.start_cycle(
        event_key=root.event_key, plan_text="edit README", posted_comment_id=1
    )
    engine.approve(event_key=approval.event_key)
    execute_kwargs = {
        "model": "cheap-haiku",
        "repo_paths": {repo.full_name: source},
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "checkpointer": object(),
    }
    return store, engine, repo, root, "github:1:issue:7", execute_kwargs


def test_advance_execution_uses_durable_default_runner(monkeypatch, tmp_path):
    (
        store,
        engine,
        repo,
        root,
        thread_id,
        execute_kwargs,
    ) = execution_ready_fixture(tmp_path)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        assert kwargs["model"] == "cheap-haiku"
        assert kwargs["thread_id"] == thread_id
        assert "edit README" in kwargs["task"]
        Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    monkeypatch.setattr("sweforge.workflow.run_task", runner)
    result = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths={repo.full_name: execute_kwargs["repo_paths"][repo.full_name]},
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )

    assert result.phase == WorkflowPhase.REVIEW_EXECUTION
    assert len(calls) == 1
    execution = store.execution_for_event(root.event_key)
    assert execution["status"] == "SUCCEEDED"
    attempt = store.latest_attempt(thread_id, 1)
    assert attempt is not None
    assert attempt.status.value == "SUCCEEDED"


def test_execute_authorized_required_arguments_fail_before_mutation(tmp_path):
    (
        store,
        engine,
        repo,
        root,
        thread_id,
        execute_kwargs,
    ) = execution_ready_fixture(tmp_path)
    permit = store.permit_for_plan(store.workflow_state(thread_id).current_plan_id)
    assert permit is not None

    with pytest.raises(TypeError, match="checkpointer"):
        engine.execute_authorized(
            permit_id=permit.permit_id,
            model=execute_kwargs["model"],
            repo_paths=execute_kwargs["repo_paths"],
            workspace_root=execute_kwargs["workspace_root"],
            lock_root=execute_kwargs["lock_root"],
        )

    assert store.workflow_state(thread_id).phase == WorkflowPhase.EXECUTION_READY
    assert store.permit(permit.permit_id).consumed_at is None
    assert store.execution_for_event(root.event_key) is None
    assert store.latest_attempt(thread_id, 1) is None
    assert not execute_kwargs["workspace_root"].exists()


def test_initial_execution_recovery_reuses_same_attempt(tmp_path):
    (
        store,
        engine,
        repo,
        root,
        thread_id,
        execute_kwargs,
    ) = execution_ready_fixture(tmp_path)
    permit = store.permit_for_plan(store.workflow_state(thread_id).current_plan_id)
    assert permit is not None
    store.bind_authorized_execution(
        permit.permit_id,
        expected_thread_id=thread_id,
        now="2026-01-01T00:00:30Z",
    )
    attempt = store.execution_attempt(f"attempt-{permit.permit_id}")
    assert attempt is not None
    assert attempt.retry_count == 0
    assert store.workflow_state(thread_id).phase == WorkflowPhase.EXECUTING

    assert recover_stale(
        store=store,
        lock_root=execute_kwargs["lock_root"],
        older_than_seconds=60,
        now=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    ) == [root.event_key]
    assert store.retry_execution(root.event_key).value == "RETRY_PENDING"
    assert store.workflow_state(thread_id).phase == WorkflowPhase.EXECUTION_READY
    reusable = store.permit(permit.permit_id)
    assert reusable is not None
    assert reusable.consumed_at is None
    assert reusable.invalidated_at is None

    def runner(**kwargs):
        Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    execute_kwargs["runner"] = runner
    result = engine.execute_authorized(
        permit_id=permit.permit_id,
        **execute_kwargs,
    )

    assert result.status == "SUCCEEDED"
    assert store.workflow_state(thread_id).phase == WorkflowPhase.REVIEW_EXECUTION
    resumed = store.execution_attempt(attempt.attempt_id)
    assert resumed is not None
    assert resumed.attempt_id == attempt.attempt_id
    assert resumed.attempt_number == 1
    assert resumed.kind.value == "INITIAL"
    assert resumed.retry_count == 1
    assert resumed.status.value == "SUCCEEDED"
    assert store.execution_for_event(root.event_key)["status"] == "SUCCEEDED"
    latest = store.latest_attempt(thread_id, 1)
    assert latest is not None
    assert latest.attempt_id == attempt.attempt_id


def test_direct_success_without_review_is_not_publishable(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/repo")
    root = event(repo, "1", "@agent fix", "2026-01-01T00:00:00Z")
    seed(store, repo, [root])
    claim = store.claim_next_event(now="2026-01-01T00:01:00Z")
    store.mark_execution_succeeded(
        claim.event_key,
        completed_at="later",
        response_text="done",
        workspace_path="/tmp",
    )
    assert not store.publication_is_eligible(root.event_key)
    assert store.next_publication() is None
    with pytest.raises(ValueError, match="ACCEPT review"):
        store.ensure_publication(root.event_key, now="later")


def test_review_evidence_keeps_plan_before_large_diff():
    evidence = {
        "plan": {"id": "plan-1", "version": 2, "text": "trusted plan"},
        "source": {"event_key": "root"},
        "attempt": {"attempt_id": "attempt-1"},
        "current_head": "head",
        "base_head": "base",
        "changed_files": ["a.py"],
        "diff": "x" * 100_000,
    }
    rendered = render_review_evidence(evidence)
    assert rendered.index("trusted plan") < rendered.index("Cumulative diff")
    assert "Diff truncated" in rendered


def test_repair_ready_executes_same_workspace_and_reaches_accept(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )

    git("init", "-q")
    (source / "README.md").write_text("base\n")
    git("add", "README.md")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    repo = RepositoryRef(1, "example/repo")
    root = event(repo, "1", "@agent fix the bug", "2026-01-01T00:00:00Z")
    approval = event(repo, "2", "@agent approve", "2026-01-01T00:01:00Z")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    seed(store, repo, [root, approval])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    engine.start_cycle(
        event_key=root.event_key, plan_text="edit README", posted_comment_id=1
    )
    engine.approve(event_key=approval.event_key)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    execute_kwargs = {
        "model": "cheap-haiku",
        "repo_paths": {repo.full_name: source},
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "checkpointer": object(),
        "runner": runner,
    }
    first = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert first.phase == WorkflowPhase.REVIEW_EXECUTION

    verdicts = iter(
        [
            ExecutionReviewResult(verdict="NEEDS_FIXES", summary="fix it"),
            ExecutionReviewResult(verdict="ACCEPT", summary="good"),
        ]
    )
    engine.reviewer = lambda **_: next(verdicts)
    review = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert review.phase == WorkflowPhase.REPAIR_READY
    repaired = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert repaired.phase == WorkflowPhase.REVIEW_EXECUTION
    accepted = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert accepted.phase == WorkflowPhase.AWAITING_PUBLICATION
    assert len(calls) == 2
    assert calls[0]["thread_id"] == calls[1]["thread_id"]
    assert calls[0]["message_id"] != calls[1]["message_id"]
    assert store.publication_is_eligible(root.event_key)


def test_sqlite_orphaned_repair_recovery_validates_parent_attempt(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        import subprocess

        return subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )

    git("init", "-q")
    (source / "README.md").write_text("base\n")
    git("add", "README.md")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    repo = RepositoryRef(1, "example/repo")
    root = event(repo, "1", "@agent fix the bug", "2026-01-01T00:00:00Z")
    approval = event(repo, "2", "@agent approve", "2026-01-01T00:01:00Z")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    seed(store, repo, [root, approval])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    engine.start_cycle(
        event_key=root.event_key, plan_text="edit README", posted_comment_id=1
    )
    engine.approve(event_key=approval.event_key)

    def runner(**kwargs):
        Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    execute_kwargs = {
        "model": "cheap-haiku",
        "repo_paths": {repo.full_name: source},
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "checkpointer": object(),
        "runner": runner,
    }
    first = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert first.phase == WorkflowPhase.REVIEW_EXECUTION
    engine.reviewer = lambda **_: ExecutionReviewResult(
        verdict="NEEDS_FIXES", summary="fix it"
    )
    review = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert review.phase == WorkflowPhase.REPAIR_READY
    permit = store.repair_permit_for_thread("github:1:issue:7")
    assert permit is not None
    bound = store.begin_or_resume_repair_attempt(
        permit.permit_id, now="2026-01-01T00:01:00Z"
    )
    assert bound.attempt_number == 2
    assert bound.kind.value == "REVIEW_REPAIR"
    assert store.workflow_state("github:1:issue:7").phase == WorkflowPhase.EXECUTING

    recovered = engine.advance(
        thread_id="github:1:issue:7",
        model="planning-sonnet",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        execute_kwargs=execute_kwargs,
    )
    assert recovered.phase == WorkflowPhase.REPAIR_READY
    failed = store.execution_attempt(bound.attempt_id)
    assert failed is not None
    assert failed.status.value == "FAILED"
    assert failed.attempt_id == bound.attempt_id
    assert failed.repair_round == 1
    parent = store.execution_review(permit.parent_review_id)
    assert parent is not None
    assert parent.attempt_id != bound.attempt_id
    permit_after = store.repair_permit(permit.permit_id)
    assert permit_after is not None
    assert permit_after.consumed_at is None
    assert permit_after.invalidated_at is None

    resumed = store.begin_or_resume_repair_attempt(
        permit.permit_id, now="2026-01-01T00:02:00Z"
    )
    assert resumed.attempt_id == bound.attempt_id
    assert resumed.repair_round == bound.repair_round
    assert resumed.retry_count == 1
    latest = store.latest_attempt("github:1:issue:7", 1)
    assert latest is not None
    assert latest.attempt_id == bound.attempt_id


@pytest.mark.parametrize("verdict", ["ACCEPT", "NEEDS_FIXES", "BLOCKED"])
def test_review_result_verdicts_are_bounded(verdict):
    result = ExecutionReviewResult(verdict=verdict, summary="summary")
    assert result.verdict == verdict


def test_reviewer_prompt_declares_bounded_authority(monkeypatch):
    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_deep_agent", fake_create_deep_agent)
    build_reviewer(ReviewerContext(worktree="/tmp/worktree"), model="reviewer")
    prompt = captured["system_prompt"]
    assert "exact approved plan" in prompt
    assert "NEEDS_FIXES" in prompt
    assert "BLOCKED" in prompt
    assert "evidence" in prompt


@pytest.mark.parametrize("path", ["/memories/AGENTS.md", "/memories/notes.md"])
def test_reviewer_is_structurally_read_only(monkeypatch, path):
    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_deep_agent", fake_create_deep_agent)
    build_reviewer(
        ReviewerContext(
            worktree="/tmp/worktree",
            memory_store=object(),
            memory_namespace=("sweforge", "repo", "1"),
        ),
        model="reviewer",
    )
    permission = captured["permissions"][0]
    assert permission.mode == "deny"
    assert path.startswith("/memories/")
