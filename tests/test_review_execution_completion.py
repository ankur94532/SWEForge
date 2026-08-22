from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain.agents.structured_output import ToolStrategy

from sweforge.execution import recover_stale
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore, WorkflowPhase
from sweforge.reviewer import (
    REVIEW_INSPECTION_MODEL_CALL_LIMIT,
    REVIEW_INSPECTION_TOOL_CALL_LIMIT,
    ChallengeReport,
    EvidenceKind,
    EvidenceRef,
    ExecutionReviewResult,
    InspectionObservation,
    InspectionReport,
    RequirementChallenge,
    RequirementChallengeVerdict,
    RequirementInspection,
    ReviewerContext,
    ReviewerReadError,
    ReviewFinding,
    ReviewRequirementCheck,
    ReviewRequirementClassification,
    ReviewRequirementStatus,
    _challenger_prompt,
    _guard_accept_coverage,
    _resolve_reviewer_file,
    _reviewer_read_tool,
    build_review_requirement_contract,
    build_reviewer,
    render_review_evidence,
    review_execution,
    review_requirement_contract,
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


def execution_ready_fixture(tmp_path, plan_text="edit README"):
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
        event_key=root.event_key, plan_text=plan_text, posted_comment_id=1
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


def test_execution_review_requirement_coverage_is_durable(tmp_path):
    store, engine, repo, root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path, plan_text="Requirements:\n1. edit README\n2. run tests"
    )
    execute_kwargs["runner"] = lambda **kwargs: "executor response"
    first = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert first.phase == WorkflowPhase.REVIEW_EXECUTION

    def reviewer(**kwargs):
        return ExecutionReviewResult(
            verdict="NEEDS_FIXES",
            summary="run tests is missing",
            requirement_checks=[
                ReviewRequirementCheck(
                    requirement_id=item["requirement_id"],
                    status=(
                        ReviewRequirementStatus.UNSATISFIED
                        if item["text"] == "run tests"
                        else ReviewRequirementStatus.SATISFIED
                    ),
                    evidence="durable test evidence",
                )
                for item in review_requirement_contract(kwargs["evidence"])
            ],
        )

    engine.reviewer = reviewer
    reviewed = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert reviewed.phase == WorkflowPhase.REPAIR_READY
    saved = store.execution_review_for_attempt(
        store.latest_attempt(thread_id, 1).attempt_id
    )
    assert saved is not None
    assert '"status": "UNSATISFIED"' in saved.requirement_checks_json
    store.close()

    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    persisted = reopened.execution_review(saved.review_id)
    assert persisted is not None
    assert persisted.requirement_checks_json == saved.requirement_checks_json
    reopened.close()


def test_repair_prompt_preserves_review_feedback_past_external_bound(tmp_path):
    long_plan = "approved step\n" * 550 + "PLAN_TAIL_SENTINEL"
    store, engine, repo, root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path, plan_text=long_plan
    )
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    execute_kwargs["runner"] = runner
    first = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert first.phase == WorkflowPhase.REVIEW_EXECUTION
    engine.reviewer = lambda **_: ExecutionReviewResult(
        verdict="NEEDS_FIXES",
        summary="REVIEW_SUMMARY_SENTINEL",
        findings=[
            ReviewFinding(
                severity="BLOCKING",
                path="src/main.py",
                description="BLOCKING_FINDING_SENTINEL",
                evidence="evidence",
            )
        ],
        repair_instructions=["REPAIR_INSTRUCTION_SENTINEL"],
    )
    reviewed = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert reviewed.phase == WorkflowPhase.REPAIR_READY

    repaired = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert repaired.phase == WorkflowPhase.REVIEW_EXECUTION
    task = calls[1]["task"]
    assert len(task) <= 50_000
    assert "PLAN_TAIL_SENTINEL" in task
    assert "[Execution Review NEEDS_FIXES]" in task
    assert "REVIEW_SUMMARY_SENTINEL" in task
    assert "BLOCKING_FINDING_SENTINEL" in task
    assert "REPAIR_INSTRUCTION_SENTINEL" in task
    assert "Review " in task


def test_distinct_repair_reviews_produce_distinct_tasks():
    from types import SimpleNamespace

    from sweforge.workflow import _build_repair_task

    plan = SimpleNamespace(version=1, plan_text="approved plan")
    review_a = SimpleNamespace(
        review_id="review-a",
        summary="summary",
        findings_json="[]",
        repair_instructions_json='["instruction A"]',
    )
    review_b = SimpleNamespace(
        review_id="review-b",
        summary="summary",
        findings_json="[]",
        repair_instructions_json='["instruction B"]',
    )
    task_a = _build_repair_task(plan, review_a)
    task_b = _build_repair_task(plan, review_b)
    assert "instruction A" in task_a and "instruction A" not in task_b
    assert "instruction B" in task_b and "instruction B" not in task_a
    assert task_a != task_b


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

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_agent", fake_create_agent)
    build_reviewer(ReviewerContext(worktree="/tmp/worktree"), model="reviewer")
    prompt = captured["system_prompt"]
    assert "exact approved plan" in prompt
    assert "NEEDS_FIXES" in prompt
    assert "BLOCKED" in prompt
    assert "evidence" in prompt
    assert "before publication" in prompt
    assert "Absence of a commit, push, or PR is not itself a defect" in prompt


def test_requirement_contract_is_deterministic_and_includes_validation():
    source = (
        "Requirements:\n"
        "1. Preserve duplicate IDs\n"
        "2. Interrupt running work\n"
        "3. Do not modify build config\n"
    )
    plan = (
        "Implementation steps:\n"
        "1. Add atomic state\n"
        "2. Track worker\n\n"
        "Validation:\n"
        "- verify interruption is observed\n"
        "- run tests\n"
    )
    first = build_review_requirement_contract(source, plan)
    second = build_review_requirement_contract(source, plan)
    assert first == second
    assert [item["requirement_id"] for item in first] == [
        "source:req:1",
        "source:req:2",
        "source:req:3",
        "plan:step:1",
        "plan:step:2",
        "plan:validation:1",
        "plan:validation:2",
    ]
    assert first[-2]["text"] == "verify interruption is observed"
    assert all(item["text"] for item in first)


def test_requirement_contract_deduplicates_literal_restatements_and_bounds():
    source = "Requirements:\n1. Preserve duplicate IDs\n2. Preserve duplicate IDs.\n"
    plan = "Validation:\n- preserve duplicate IDs\n- run tests\n"
    contract = build_review_requirement_contract(source, plan)
    assert [item["text"] for item in contract] == [
        "Preserve duplicate IDs",
        "run tests",
    ]
    huge = "Requirements:\n" + "\n".join(
        f"{index}. {'x' * 800}" for index in range(1, 200)
    )
    bounded = build_review_requirement_contract(huge, huge)
    assert len(bounded) <= 80
    assert all(len(item["text"]) <= 500 for item in bounded)


def test_requirement_contract_has_bounded_fallback_and_ignores_source_tail():
    contract = build_review_requirement_contract("plain request", "plain plan")
    assert [(item["requirement_id"], item["text"]) for item in contract] == [
        ("source:overall:1", "plain request"),
        ("plan:overall:1", "plain plan"),
    ]
    assert all(item["classification"] == "BEHAVIORAL" for item in contract)
    source = "Requirements:\n1. visible\n" + ("\nnoise" * 1_000)
    contract = build_review_requirement_contract(source[:4_000], "plan")
    assert all("noise" not in item["text"] for item in contract)


def test_accept_guard_rejects_partial_unknown_duplicate_and_non_satisfied():
    contract = [
        {"requirement_id": "REQ-A", "text": "A"},
        {"requirement_id": "REQ-B", "text": "B"},
        {"requirement_id": "REQ-C", "text": "C"},
    ]
    partial = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="partial",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="REQ-A",
                status=ReviewRequirementStatus.SATISFIED,
                evidence="A",
            )
        ],
    )
    assert _guard_accept_coverage(partial, contract).verdict == "BLOCKED"
    invalid = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="invalid",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="REQ-A",
                status=ReviewRequirementStatus.SATISFIED,
                evidence="A",
            ),
            ReviewRequirementCheck(
                requirement_id="REQ-A",
                status=ReviewRequirementStatus.SATISFIED,
                evidence="A again",
            ),
            ReviewRequirementCheck(
                requirement_id="REQ-C",
                status=ReviewRequirementStatus.UNVERIFIED,
                evidence="not enough",
            ),
        ],
    )
    guarded = _guard_accept_coverage(invalid, contract)
    assert guarded.verdict == "BLOCKED"
    assert guarded.findings[0].severity == "BLOCKING"


def test_accept_guard_allows_exact_satisfied_coverage():
    contract = [
        {"requirement_id": "REQ-A", "text": "A"},
        {"requirement_id": "REQ-B", "text": "B"},
    ]
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="complete",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id=item["requirement_id"],
                status=ReviewRequirementStatus.SATISFIED,
                evidence="verified",
            )
            for item in contract
        ],
    )
    assert _guard_accept_coverage(result, contract).verdict == "ACCEPT"


def test_requirement_classification_is_conservative_and_deterministic():
    structural = build_review_requirement_contract(
        "Requirements:\n1. Add enum value READY\n2. Create file src/new.py",
        "",
    )
    behavioral = build_review_requirement_contract(
        "Requirements:\n1. Callback occurs after state transition",
        "",
    )
    assert all(
        item["classification"] == ReviewRequirementClassification.STRUCTURAL.value
        for item in structural
    )
    assert (
        behavioral[0]["classification"]
        == ReviewRequirementClassification.BEHAVIORAL.value
    )


def test_read_ledger_records_only_successful_bounded_reads(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("one\ntwo\nthree\n")
    ledger = []
    tool = _reviewer_read_tool(ReviewerContext(str(tmp_path), read_ledger=ledger))
    assert tool.invoke({"path": "src/main.py", "offset": 1, "limit": 1}) == "two\n"
    assert ledger[0]["normalized_path"] == "src/main.py"
    assert ledger[0]["returned_lines"] == [2, 2]
    assert ledger[0]["excerpt"] == "two\n"


def test_accept_guard_rejects_unread_and_wrong_requirement_evidence():
    contract = [
        {
            "requirement_id": "REQ-B",
            "text": "callback follows state transition",
            "classification": "BEHAVIORAL",
        }
    ]
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id="REQ-B",
                classification="BEHAVIORAL",
                status="VERIFIED",
                observation_ids=["obs-1"],
                evidence_refs=[
                    EvidenceRef(
                        ref_id="ref-1",
                        requirement_id="REQ-B",
                        kind=EvidenceKind.INSPECTED_FILE,
                        path="src/missing.py",
                    )
                ],
            )
        ],
        observations=[
            InspectionObservation(
                observation_id="obs-1",
                requirement_id="REQ-B",
                kind="CODE",
                path="src/main.py",
                fact="state transition precedes callback",
            )
        ],
    )
    challenge = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id="REQ-B",
                verdict=RequirementChallengeVerdict.SUPPORTED,
                challenge_summary="no contradiction found",
            )
        ]
    )
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="accepted",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="REQ-B",
                status="SATISFIED",
                evidence="source fact",
            )
        ],
    )
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[{"normalized_path": "src/main.py"}],
    )
    assert guarded.verdict == "BLOCKED"


def test_challenger_prompt_contains_raw_resolved_evidence_not_only_summary():
    evidence = {"execution": {"status": "SUCCEEDED"}}
    contract = [
        {
            "requirement_id": "REQ-B",
            "text": "callback follows state transition",
            "classification": "BEHAVIORAL",
        }
    ]
    prompt = _challenger_prompt(
        evidence,
        contract,
        [
            {
                "requirement_id": "REQ-B",
                "summary": "looks safe",
                "observations": [],
                "evidence": [{"path": "src/main.py", "excerpt": "state = RUNNING"}],
            }
        ],
    )
    assert "state = RUNNING" in prompt
    assert "looks safe" in prompt


def test_review_execution_partial_accept_fails_closed(monkeypatch):
    evidence = _review_evidence()
    evidence["source_request"] = "Requirements:\n1. A\n2. B\n3. C"
    inspector = _FakeAgent({"messages": []})
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="ACCEPT",
                summary="only repaired item checked",
                requirement_checks=[
                    ReviewRequirementCheck(
                        requirement_id="source:req:1",
                        status=ReviewRequirementStatus.SATISFIED,
                        evidence="A",
                    )
                ],
            )
        }
    )
    monkeypatch.setattr("sweforge.reviewer.build_reviewer", lambda *a, **k: inspector)
    monkeypatch.setattr("sweforge.reviewer._build_finalizer", lambda *a, **k: finalizer)
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=evidence,
    )
    assert result.verdict == "BLOCKED"
    assert result.repair_instructions == []


def test_needs_fixes_with_incomplete_coverage_remains_needs_fixes(monkeypatch):
    evidence = _review_evidence()
    inspector = _FakeAgent({"messages": []})
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="NEEDS_FIXES", summary="B is not fixed"
            )
        }
    )
    monkeypatch.setattr("sweforge.reviewer.build_reviewer", lambda *a, **k: inspector)
    monkeypatch.setattr("sweforge.reviewer._build_finalizer", lambda *a, **k: finalizer)
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=evidence,
    )
    assert result.verdict == "NEEDS_FIXES"


def test_reviewer_prompts_require_full_contract_semantics():
    from sweforge.reviewer import FINALIZER_SYSTEM_PROMPT, INSPECTOR_SYSTEM_PROMPT

    for prompt in (INSPECTOR_SYSTEM_PROMPT, FINALIZER_SYSTEM_PROMPT):
        assert "complete" in prompt or "entire" in prompt
        assert "previous" in prompt.lower()
        assert "test name" in prompt.lower() or "test names" in prompt.lower()
        assert "interleavings" in prompt or "behavioral" in prompt


def test_finalizer_prompt_declares_uncommitted_worktree_authority():
    from sweforge.reviewer import FINALIZER_SYSTEM_PROMPT

    assert "before publication" in FINALIZER_SYSTEM_PROMPT
    assert (
        "Absence of a commit, push, or PR is not itself a defect"
        in FINALIZER_SYSTEM_PROMPT
    )
    assert "cumulative diff" in FINALIZER_SYSTEM_PROMPT


@pytest.mark.parametrize("path", ["/memories/AGENTS.md", "/memories/notes.md"])
def test_reviewer_is_structurally_read_only(monkeypatch, path):
    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_agent", fake_create_agent)
    build_reviewer(
        ReviewerContext(
            worktree="/tmp/worktree",
            memory_store=object(),
            memory_namespace=("sweforge", "repo", "1"),
        ),
        model="reviewer",
    )
    assert [tool.name for tool in captured["tools"]] == ["read_repo_file"]
    assert path.startswith("/memories/")


def test_reviewer_limits_and_tool_surface_are_scoped(monkeypatch):
    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_agent", fake_create_agent)
    build_reviewer(ReviewerContext(worktree="/tmp/worktree"), model="reviewer")

    assert [tool.name for tool in captured["tools"]] == ["read_repo_file"]
    assert all(
        tool.name
        not in {"ls", "glob", "grep", "write_file", "edit_file", "execute", "task"}
        for tool in captured["tools"]
    )
    limits = captured["middleware"][-2:]
    assert limits[0].run_limit == REVIEW_INSPECTION_MODEL_CALL_LIMIT
    assert limits[1].run_limit == REVIEW_INSPECTION_TOOL_CALL_LIMIT


@pytest.mark.parametrize(
    "path",
    [
        "../../secret",
        "/Users/someone/secret",
        ".git/config",
        "build/reports/tests.html",
        ".gradle/cache.bin",
        "/sweforge_internal/state",
    ],
)
def test_reviewer_read_policy_rejects_unsafe_paths(tmp_path, path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n")
    with pytest.raises(ReviewerReadError):
        _resolve_reviewer_file(str(tmp_path), path)


def test_reviewer_read_policy_accepts_relative_and_virtual_paths(tmp_path):
    (tmp_path / "src").mkdir()
    file_path = tmp_path / "src" / "main.py"
    file_path.write_text("one\ntwo\n")
    assert _resolve_reviewer_file(str(tmp_path), "src/main.py") == file_path
    assert _resolve_reviewer_file(str(tmp_path), "/src/main.py") == file_path


def test_reviewer_read_tool_bounds_file_output(tmp_path):
    from sweforge.reviewer import _reviewer_read_tool

    file_path = tmp_path / "main.py"
    file_path.write_text("one\ntwo\nthree\n")
    tool = _reviewer_read_tool(ReviewerContext(worktree=str(tmp_path)))
    assert tool.invoke({"path": "main.py", "offset": 1, "limit": 1}) == "two\n"
    with pytest.raises(Exception, match="limit"):
        tool.invoke({"path": "main.py", "limit": 4_001})


def test_finalizer_construction_has_no_filesystem_tools(monkeypatch):
    from sweforge.reviewer import _build_finalizer

    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_agent", fake_create_agent)
    _build_finalizer(
        ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        live_middleware=[],
    )
    assert captured["tools"] == []
    assert isinstance(captured["response_format"], ToolStrategy)


class _FakeAgent:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return self.result


def _review_evidence():
    return {
        "plan": {"id": "plan-1", "version": 1, "text": "edit src/main.py"},
        "source": {"event_key": "root"},
        "attempt": {"attempt_id": "attempt-1"},
        "execution": {"status": "SUCCEEDED"},
        "current_head": "head",
        "base_head": "base",
        "dirty": True,
        "changed_files": ["src/main.py"],
        "diff": "-old\n+new\n",
    }


def _complete_review_checks(evidence):
    return [
        ReviewRequirementCheck(
            requirement_id=item["requirement_id"],
            status=ReviewRequirementStatus.SATISFIED,
            evidence="observable evidence",
        )
        for item in review_requirement_contract(evidence)
    ]


def test_review_finalizer_has_no_filesystem_tools_and_accepts(monkeypatch):
    inspector = _FakeAgent({"messages": [type("M", (), {"content": "looks good"})()]})
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="ACCEPT",
                summary="ok",
                requirement_checks=_complete_review_checks(_review_evidence()),
            )
        }
    )
    captured = {}
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )

    def fake_finalizer(context, *, model, live_middleware):
        captured["middleware"] = live_middleware
        captured["tools"] = []
        return finalizer

    monkeypatch.setattr("sweforge.reviewer._build_finalizer", fake_finalizer)
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=_review_evidence(),
    )
    assert result.verdict == "BLOCKED"
    assert captured["tools"] == []
    assert "Trusted execution evidence" in finalizer.calls[0]["messages"][0]["content"]
    assert "Structured inspection" in finalizer.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("verdict", ["ACCEPT", "NEEDS_FIXES", "BLOCKED"])
def test_inspection_budget_exhaustion_reaches_finalizer(monkeypatch, verdict):
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

    inspector = _FakeAgent(error=ModelCallLimitExceededError(8, 8, None, 8))
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict=verdict,
                summary="result",
                requirement_checks=(
                    _complete_review_checks(_review_evidence())
                    if verdict == "ACCEPT"
                    else []
                ),
            )
        }
    )
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )
    monkeypatch.setattr(
        "sweforge.reviewer._build_finalizer", lambda *args, **kwargs: finalizer
    )
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=_review_evidence(),
    )
    assert result.verdict == ("BLOCKED" if verdict == "ACCEPT" else verdict)
    prompt = finalizer.calls[0]["messages"][0]["content"]
    assert '"budget_exhausted": true' in prompt
    assert "edit src/main.py" in prompt


def test_inspection_tool_budget_exhaustion_reaches_finalizer(monkeypatch):
    from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError

    inspector = _FakeAgent(error=ToolCallLimitExceededError(24, 24, None, 24))
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="BLOCKED", summary="limited"
            )
        }
    )
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )
    monkeypatch.setattr(
        "sweforge.reviewer._build_finalizer", lambda *args, **kwargs: finalizer
    )
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=_review_evidence(),
    )
    assert result.verdict == "BLOCKED"
    assert finalizer.calls


def test_finalizer_budget_fails_closed(monkeypatch):
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

    inspector = _FakeAgent({"messages": []})
    finalizer = _FakeAgent(error=ModelCallLimitExceededError(3, 3, None, 3))
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )
    monkeypatch.setattr(
        "sweforge.reviewer._build_finalizer", lambda *args, **kwargs: finalizer
    )
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=_review_evidence(),
    )
    assert result.verdict == "BLOCKED"
    assert result.repair_instructions == []


def test_reviewer_provider_errors_remain_retryable(monkeypatch):
    inspector = _FakeAgent(error=RuntimeError("provider unavailable"))
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=_review_evidence(),
        )
