from pathlib import Path

import pytest

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore, WorkflowPhase
from sweforge.reviewer import ExecutionReviewResult, render_review_evidence
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


@pytest.mark.parametrize("verdict", ["ACCEPT", "NEEDS_FIXES", "BLOCKED"])
def test_review_result_verdicts_are_bounded(verdict):
    result = ExecutionReviewResult(verdict=verdict, summary="summary")
    assert result.verdict == verdict
