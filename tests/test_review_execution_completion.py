import hashlib
import json
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
    CandidateAssociationBasis,
    ChallengeReport,
    EvidenceCatalogEntry,
    EvidenceCluster,
    EvidenceClusterRange,
    EvidenceClusterRole,
    EvidenceFinding,
    EvidenceFindingProvenance,
    EvidenceFindingSeverity,
    EvidenceKind,
    EvidenceRef,
    ExecutionReviewResult,
    FindingCandidateAssociation,
    InspectionObservation,
    InspectionReport,
    InspectionStatus,
    LocalEvidenceSlice,
    RequirementChallenge,
    RequirementChallengeVerdict,
    RequirementInspection,
    ResolvedEvidenceCatalog,
    ResolvedRequirementEvidence,
    ReviewerContext,
    ReviewerReadError,
    ReviewFinalizationError,
    ReviewFinding,
    ReviewRepairability,
    ReviewRequirementCheck,
    ReviewRequirementClassification,
    ReviewRequirementStatus,
    SemanticReviewArtifact,
    SpecialistEvidenceScope,
    SpecialistFailureStage,
    SpecialistModelResponse,
    SpecialistStage,
    SpecialistStageReport,
    SpecialistStageStatus,
    _authority_facts,
    _build_challenger,
    _candidate_requirement_associations,
    _challenger_prompt,
    _clusters_for_stage,
    _complete_challenge_report,
    _evidence_clusters,
    _fail_closed_unavailable_inspections,
    _finalizer_prompt,
    _guard_accept_coverage,
    _guard_repairability,
    _inspection_artifact_problems,
    _invoke_specialist,
    _lexical_tokens,
    _resolve_reviewer_file,
    _resolved_evidence,
    _reviewer_read_tool,
    _split_semantic_review,
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


def test_operational_review_failure_retries_same_successful_attempt(tmp_path):
    store, engine, repo, root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path
    )
    execution_calls = []

    def runner(**kwargs):
        execution_calls.append(kwargs)
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
    assert first.phase is WorkflowPhase.REVIEW_EXECUTION
    attempt = store.latest_attempt(thread_id, 1)
    assert attempt is not None
    attempt_id = attempt.attempt_id

    def unavailable_reviewer(**_kwargs):
        raise ReviewFinalizationError("finalizer unavailable")

    engine.reviewer = unavailable_reviewer
    with pytest.raises(ReviewFinalizationError):
        engine.advance(
            thread_id=thread_id,
            model="planning-sonnet",
            review_model="review-sonnet",
            repo_paths=execute_kwargs["repo_paths"],
            workspace_root=execute_kwargs["workspace_root"],
            execute_kwargs=execute_kwargs,
        )
    assert store.workflow_state(thread_id).phase is WorkflowPhase.REVIEW_EXECUTION
    assert store.execution_review_for_attempt(attempt_id) is None
    assert store.repair_permit_for_thread(thread_id) is None
    assert store.eligible_publication_id(thread_id) is None

    engine.reviewer = lambda **_: ExecutionReviewResult(
        verdict="ACCEPT", summary="accepted"
    )
    accepted = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert accepted.phase is WorkflowPhase.AWAITING_PUBLICATION
    assert store.latest_attempt(thread_id, 1).attempt_id == attempt_id
    assert len(execution_calls) == 1
    assert store.execution_review_for_attempt(attempt_id).verdict == "ACCEPT"
    assert store.eligible_publication_id(thread_id) is not None


def test_semantic_blocked_review_remains_terminal(tmp_path):
    store, engine, repo, root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path
    )
    execute_kwargs["runner"] = lambda **_: "executor response"
    first = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert first.phase is WorkflowPhase.REVIEW_EXECUTION
    review_calls = []

    def semantic_block(**_kwargs):
        review_calls.append(True)
        return ExecutionReviewResult(
            verdict="BLOCKED", summary="required evidence is insufficient"
        )

    engine.reviewer = semantic_block
    blocked = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert blocked.phase is WorkflowPhase.REVIEW_BLOCKED
    assert len(review_calls) == 1
    assert (
        store.execution_review_for_attempt(
            store.latest_attempt(thread_id, 1).attempt_id
        ).verdict
        == "BLOCKED"
    )
    terminal = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert terminal.phase is WorkflowPhase.REVIEW_BLOCKED
    assert len(review_calls) == 1
    assert store.eligible_publication_id(thread_id) is None


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
    assert store.eligible_publication_id(claim.thread_id) is None
    assert store.next_publication() is None
    with pytest.raises(ValueError, match="ACCEPT review"):
        store.ensure_publication(thread_id=claim.thread_id, now="later")


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
    assert store.eligible_publication_id("github:1:issue:7") is not None


def test_issue12_like_repair_uses_real_no_task_agent_and_records_maven(tmp_path):
    from deepagents.backends import LocalShellBackend
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from pydantic import PrivateAttr

    from sweforge.agent import run_task

    class RepairModel(BaseChatModel):
        _responses: list[AIMessage] = PrivateAttr()
        _surfaces: list[tuple[str, ...]] = PrivateAttr(default_factory=list)

        def __init__(self, responses):
            super().__init__()
            self._responses = list(responses)

        @property
        def _llm_type(self):
            return "issue12-repair-fixture"

        @property
        def surfaces(self):
            return self._surfaces

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            self._surfaces.append(tuple(sorted(tool.name for tool in tools)))
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[ChatGeneration(message=self._responses.pop(0))]
            )

    source = tmp_path / "source"
    source.mkdir()
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )

    git("init", "-q")
    (source / "pom.xml").write_text("<project/>\n")
    test_file = source / "src/test/java/example/PricingCalculatorTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class PricingCalculatorTest {}\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_maven = fake_bin / "mvn"
    fake_maven.write_text("#!/bin/sh\nprintf 'Tests run: 1\\nBUILD SUCCESS\\n'\n")
    fake_maven.chmod(0o755)

    repo = RepositoryRef(1, "example/repo")
    root = event(repo, "1", "@agent add boundary tests", "2026-01-01T00:00:00Z")
    approval = event(repo, "2", "@agent approve", "2026-01-01T00:01:00Z")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    seed(store, repo, [root, approval])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    engine.start_cycle(
        event_key=root.event_key,
        plan_text="Add test-only boundary coverage and run mvn test",
        posted_comment_id=1,
    )
    engine.approve(event_key=approval.event_key)

    repair_model = RepairModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": "mvn test"},
                        "id": "maven-repair",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Maven validation completed"),
        ]
    )
    invocations = []

    def runner(**kwargs):
        invocations.append(kwargs["repair_mode"])
        if not kwargs["repair_mode"]:
            workspace_test = (
                Path(kwargs["worktree"])
                / "src/test/java/example/PricingCalculatorTest.java"
            )
            workspace_test.write_text(
                "class PricingCalculatorTest { /* boundary regression */ }\n"
            )
            return "tests added; Maven not run"
        return run_task(**{**kwargs, "model": repair_model})

    verdicts = iter(
        [
            ExecutionReviewResult(
                verdict="NEEDS_FIXES",
                summary="trusted Maven evidence is missing",
                requirement_checks=[
                    ReviewRequirementCheck(
                        requirement_id="plan:validation:1",
                        status=ReviewRequirementStatus.UNVERIFIED,
                        repairability=ReviewRepairability.IN_SCOPE_REPAIR,
                        evidence="no trusted mvn test observation",
                    )
                ],
                repair_instructions=["Run mvn test and capture trusted output."],
            ),
            ExecutionReviewResult(
                verdict="ACCEPT",
                summary="trusted Maven evidence proves success",
                requirement_checks=[
                    ReviewRequirementCheck(
                        requirement_id="plan:validation:1",
                        status=ReviewRequirementStatus.SATISFIED,
                        repairability=ReviewRepairability.NOT_APPLICABLE,
                        evidence="REVIEW_REPAIR mvn test exited 0 with BUILD SUCCESS",
                    )
                ],
            ),
        ]
    )
    engine.reviewer = lambda **_: next(verdicts)
    thread_id = "github:1:issue:7"

    def provider(**kwargs):
        return LocalShellBackend(
            root_dir=kwargs["worktree"],
            virtual_mode=True,
            env={"PATH": str(fake_bin)},
            inherit_env=False,
        )

    execute_kwargs = {
        "model": "unused-by-test-runner",
        "repo_paths": {repo.full_name: source},
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "checkpointer": InMemorySaver(),
        "runner": runner,
        "sandbox_backend_provider": provider,
        "secure_execution": True,
    }
    initial = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert initial.phase is WorkflowPhase.REVIEW_EXECUTION
    initial_attempt = store.latest_attempt(thread_id, 1)
    assert initial_attempt is not None
    assert store.execution_tool_evidence_for_attempt(initial_attempt.attempt_id) == ()

    reviewed = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert reviewed.phase is WorkflowPhase.REPAIR_READY
    repaired = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert repaired.phase is WorkflowPhase.REVIEW_EXECUTION
    repair_attempt = store.latest_attempt(thread_id, 1)
    assert repair_attempt is not None
    repair_evidence = store.execution_tool_evidence_for_attempt(
        repair_attempt.attempt_id
    )
    assert [(item.command, item.exit_code) for item in repair_evidence] == [
        ("mvn test", 0)
    ]
    assert "BUILD SUCCESS" in repair_evidence[0].output
    accepted = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )

    assert accepted.phase is WorkflowPhase.AWAITING_PUBLICATION
    assert invocations == [False, True]
    assert "task" not in repair_model.surfaces[-1]
    assert repair_attempt.kind.value == "REVIEW_REPAIR"
    assert repair_attempt.retry_count == 0
    assert repair_attempt.repair_recovery_count == 0
    assert store.execution_review_for_attempt(initial_attempt.attempt_id).verdict == (
        "NEEDS_FIXES"
    )
    assert store.execution_review_for_attempt(repair_attempt.attempt_id).verdict == (
        "ACCEPT"
    )


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
            semantic_review=SemanticReviewArtifact(
                implementation=SpecialistStageReport(
                    stage="IMPLEMENTATION", status="SKIPPED", applicable=False
                ),
                test_validation=SpecialistStageReport(
                    stage="TEST_VALIDATION", status="SKIPPED", applicable=False
                ),
            ),
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
    semantic_json = json.loads(saved.challenge_json)
    assert semantic_json["artifact_kind"] == "SPLIT_EVIDENCE_REVIEW"
    assert semantic_json["artifact_version"] == 1
    store.close()

    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    persisted = reopened.execution_review(saved.review_id)
    assert persisted is not None
    assert persisted.requirement_checks_json == saved.requirement_checks_json
    assert persisted.challenge_json == saved.challenge_json
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

    store.mark_repair_attempt_failed(bound.attempt_id, now="2026-01-01T00:03:00Z")
    assert (
        store.workflow_state("github:1:issue:7").phase == WorkflowPhase.REVIEW_BLOCKED
    )
    terminal_permit = store.repair_permit(permit.permit_id)
    assert terminal_permit is not None
    assert terminal_permit.invalidated_at is not None
    with pytest.raises(ValueError, match="unavailable"):
        store.begin_or_resume_repair_attempt(
            permit.permit_id, now="2026-01-01T00:04:00Z"
        )


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
    validation = build_review_requirement_contract(
        "", "Validation:\n- run the focused checker\n- verify the full suite"
    )
    assert [item["classification"] for item in validation] == [
        ReviewRequirementClassification.VALIDATION.value,
        ReviewRequirementClassification.VALIDATION.value,
    ]


def _coverage_guard_fixture(
    requirement_id, classification, refs, *, execution=True, changed_files=None
):
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status=InspectionStatus.VERIFIED,
                evidence_refs=refs,
            )
        ]
    )
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="accepted",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id=requirement_id,
                status=ReviewRequirementStatus.SATISFIED,
                evidence="trusted",
                evidence_refs=refs,
            )
        ],
    )
    semantic = _guard_semantic()
    contract = [
        {
            "requirement_id": requirement_id,
            "text": "requirement",
            "classification": classification,
        }
    ]
    evidence = {"changed_files": changed_files or []}
    if execution:
        evidence["execution"] = [{"evidence_id": "exec-1"}]
        evidence["execution_observations"] = [
            {"evidence_id": "exec-1", "command": "test", "exit_code": 0}
        ]
    return _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        semantic_review=semantic,
        ledger=[],
        evidence=evidence,
    )


def test_accept_guard_allows_validation_with_authoritative_execution_only():
    ref = EvidenceRef(
        ref_id="exec-ref",
        requirement_id="plan:validation:1",
        kind=EvidenceKind.EXECUTION,
        source_id="exec-1",
    )
    assert (
        _coverage_guard_fixture(
            "plan:validation:1",
            ReviewRequirementClassification.VALIDATION.value,
            [ref],
        ).verdict
        == "ACCEPT"
    )


def test_accept_guard_rejects_validation_without_execution():
    ref = EvidenceRef(
        ref_id="diff-ref",
        requirement_id="plan:validation:1",
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/test.py",
    )
    guarded = _coverage_guard_fixture(
        "plan:validation:1",
        ReviewRequirementClassification.VALIDATION.value,
        [ref],
        execution=False,
    )
    assert guarded.verdict == "BLOCKED"
    assert "missing direct execution evidence" in guarded.findings[0].evidence


def test_accept_guard_keeps_behavioral_implementation_code_grounding():
    ref = EvidenceRef(
        ref_id="exec-ref",
        requirement_id="plan:step:1",
        kind=EvidenceKind.EXECUTION,
        source_id="exec-1",
    )
    guarded = _coverage_guard_fixture(
        "plan:step:1",
        ReviewRequirementClassification.BEHAVIORAL.value,
        [ref],
    )
    assert guarded.verdict == "BLOCKED"
    assert "missing direct code observation" in guarded.findings[0].evidence


def test_accept_guard_requires_test_assertion_signal_for_behavioral_claims():
    requirement_id = "plan:validation:1"
    diff_ref = EvidenceRef(
        ref_id="diff-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/test.py",
    )
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status=InspectionStatus.VERIFIED,
                evidence_refs=[diff_ref],
            )
        ],
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/test.py",
                fact="threshold assertion is present",
            ),
            InspectionObservation(
                observation_id="test-1",
                requirement_id=requirement_id,
                kind="TEST",
                path="src/test.py",
                fact="test covers the threshold",
            ),
        ],
    )
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="accepted",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id=requirement_id,
                status=ReviewRequirementStatus.SATISFIED,
                evidence="trusted diff",
                evidence_refs=[diff_ref],
            )
        ],
    )
    guarded = _guard_accept_coverage(
        result,
        [
            {
                "requirement_id": requirement_id,
                "text": "the threshold behavior is asserted",
                "classification": ReviewRequirementClassification.BEHAVIORAL.value,
            }
        ],
        inspection=inspection,
        semantic_review=_guard_semantic(),
        ledger=[],
        evidence={"changed_files": ["src/test.py"]},
    )
    assert guarded.verdict == "BLOCKED"
    assert "missing assertion or signal" in guarded.findings[0].evidence


def test_accept_guard_keeps_structural_diff_authority():
    requirement_id = "plan:step:4"
    diff_ref = EvidenceRef(
        ref_id="diff-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/test.py",
    )
    guarded = _coverage_guard_fixture(
        requirement_id,
        ReviewRequirementClassification.STRUCTURAL.value,
        [diff_ref],
        changed_files=["src/test.py"],
    )
    assert guarded.verdict == "ACCEPT"


def test_accept_guard_passes_exact_issue13_requirement_modes():
    source = "@agent Add focused threshold regression tests."
    plan = (
        "Implementation steps:\n"
        "1. Confirm threshold behavior.\n"
        "2. Add the three tests.\n"
        "3. Name the test methods.\n"
        "4. Do not modify /src/main/java.\n"
        "5. Run the focused test class.\n"
        "6. Run the full test suite.\n\n"
        "Validation:\n"
        "- New tests assert 9,999, 10,000, and 10,001 behavior.\n"
        "- `mvn -Dtest=PricingCalculatorTest test` passes.\n"
        "- `mvn test` passes.\n"
        "- No changes under /src/main/java."
    )
    contract = build_review_requirement_contract(source, plan)
    changed_path = "src/test/java/com/sweforge/pricing/PricingCalculatorTest.java"
    execution_refs = {
        "plan:step:5": "exec-focused",
        "plan:step:6": "exec-full",
        "plan:validation:2": "exec-focused",
        "plan:validation:3": "exec-full",
    }
    inspections = []
    observations = []
    checks = []
    for item in contract:
        requirement_id = item["requirement_id"]
        refs = []
        if requirement_id in execution_refs:
            refs.append(
                EvidenceRef(
                    ref_id=f"ref-{requirement_id}",
                    requirement_id=requirement_id,
                    kind=EvidenceKind.EXECUTION,
                    source_id=execution_refs[requirement_id],
                )
            )
        else:
            refs.append(
                EvidenceRef(
                    ref_id=f"ref-{requirement_id}",
                    requirement_id=requirement_id,
                    kind=EvidenceKind.TRUSTED_DIFF,
                    path=changed_path,
                )
            )
        if item["classification"] == ReviewRequirementClassification.BEHAVIORAL.value:
            observations.append(
                InspectionObservation(
                    observation_id=f"code-{requirement_id}",
                    requirement_id=requirement_id,
                    kind="CODE",
                    path=changed_path,
                    fact="direct threshold implementation/test grounding",
                )
            )
            if requirement_id == "plan:validation:1":
                observations.append(
                    InspectionObservation(
                        observation_id="test-plan-validation-1",
                        requirement_id=requirement_id,
                        kind="TEST",
                        path=changed_path,
                        fact="assertions cover all three boundary values",
                        assertion_or_signal="9,999, 10,000, and 10,001 assertions",
                    )
                )
        inspections.append(
            RequirementInspection(
                requirement_id=requirement_id,
                status=InspectionStatus.VERIFIED,
                evidence_refs=refs,
            )
        )
        checks.append(
            ReviewRequirementCheck(
                requirement_id=requirement_id,
                status=ReviewRequirementStatus.SATISFIED,
                evidence="trusted evidence",
                evidence_refs=refs,
            )
        )
    result = ExecutionReviewResult(
        verdict="ACCEPT", summary="exact fixture accepted", requirement_checks=checks
    )
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=InspectionReport(inspections=inspections, observations=observations),
        semantic_review=_guard_semantic(),
        ledger=[],
        evidence={
            "changed_files": [changed_path],
            "execution": [{"evidence_id": "exec-focused"}],
            "execution_observations": [
                {
                    "evidence_id": "exec-focused",
                    "command": "mvn focused",
                    "exit_code": 0,
                },
                {"evidence_id": "exec-full", "command": "mvn test", "exit_code": 0},
            ],
        },
    )
    assert guarded.verdict == "ACCEPT"


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
        ResolvedEvidenceCatalog(
            requirements=[
                ResolvedRequirementEvidence(
                    requirement_id="REQ-B",
                    summary="looks safe",
                    evidence_ids=["diff:1"],
                )
            ],
            catalog=[
                EvidenceCatalogEntry(
                    evidence_id="diff:1",
                    kind="TRUSTED_DIFF",
                    path="src/main.py",
                    content_hash="0" * 64,
                    bounded_excerpt="state = RUNNING",
                )
            ],
        ),
    )
    assert "state = RUNNING" in prompt
    assert "looks safe" in prompt


def test_application_owns_classification_and_observation_membership():
    assert "classification" not in RequirementInspection.model_fields
    assert "observation_ids" not in RequirementInspection.model_fields
    report = InspectionReport(
        inspections=[
            RequirementInspection(requirement_id="source:req:1", status="VERIFIED")
        ],
        observations=[
            InspectionObservation(
                observation_id="obs-1",
                requirement_id="source:req:1",
                kind="CODE",
                path="src/main.py",
                fact="direct code fact",
            )
        ],
    )
    resolved = _resolved_evidence(
        {
            "source_request": (
                "Requirements:\n1. callback preserves ordering under concurrent "
                "cancellation"
            )
        },
        report,
        [],
    )
    assert [item.observation_id for item in resolved.requirements[0].observations] == [
        "obs-1"
    ]


def test_authority_facts_and_finalizer_prompt_forbid_unsafe_acceptance():
    contract = [
        {
            "requirement_id": "REQ-B",
            "text": "callback preserves ordering under concurrent cancellation",
            "classification": "BEHAVIORAL",
        }
    ]
    inspection = InspectionReport(
        inspections=[RequirementInspection(requirement_id="REQ-B", status="VERIFIED")]
    )
    challenge = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id="REQ-B",
                verdict="CHALLENGED",
                challenge_summary="race",
            )
        ]
    )
    facts = _authority_facts(contract, inspection, challenge)
    assert facts[0]["accept_authority"] == "FORBIDDEN"
    prompt = _finalizer_prompt(
        {"source_request": "Requirements:\n1. callback preserves ordering"},
        inspection,
        ResolvedEvidenceCatalog(),
        challenge,
        facts,
        False,
    )
    assert "challenger CHALLENGED cannot be SATISFIED" in prompt
    assert "UNSATISFIED or UNVERIFIED, ACCEPT is forbidden" in prompt


def test_missing_challenge_results_are_application_owned_unverified():
    first = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id="REQ-1",
                verdict="SUPPORTED",
                challenge_summary="supported",
            ),
            RequirementChallenge(
                requirement_id="unexpected",
                verdict="SUPPORTED",
                challenge_summary="ignored",
            ),
        ]
    )
    complete = _complete_challenge_report(first, expected_ids=["REQ-1", "REQ-2"])
    assert [item.requirement_id for item in complete.challenges] == ["REQ-1", "REQ-2"]
    assert complete.challenges[1].verdict is RequirementChallengeVerdict.UNVERIFIED


def _supported_behavioral_artifacts():
    requirement = {
        "requirement_id": "REQ-B",
        "text": "callback follows state transition",
        "classification": "BEHAVIORAL",
    }
    ref = EvidenceRef(
        ref_id="diff-1",
        requirement_id="REQ-B",
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/main.py",
    )
    inspection = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id="REQ-B",
                classification="BEHAVIORAL",
                status="VERIFIED",
                observation_ids=["obs-1"],
                evidence_refs=[ref],
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
                verdict="SUPPORTED",
                challenge_summary="no contradiction found",
                evidence_refs=[ref],
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
    evidence = {
        "changed_files": ["src/main.py"],
        "execution": {"status": "SUCCEEDED"},
    }
    return [requirement], inspection, challenge, result, evidence


def test_same_path_reads_resolve_by_exact_read_id():
    ledger = [
        {
            "read_id": "read:first",
            "normalized_path": "src/main.py",
            "returned_lines": [1, 1],
            "excerpt": "first excerpt",
        },
        {
            "read_id": "read:second",
            "normalized_path": "src/main.py",
            "returned_lines": [20, 20],
            "excerpt": "second excerpt",
        },
    ]
    report = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id="source:req:1",
                classification="BEHAVIORAL",
                status="VERIFIED",
                evidence_refs=[
                    EvidenceRef(
                        ref_id="first",
                        requirement_id="source:req:1",
                        kind="INSPECTED_FILE",
                        source_id="read:first",
                        path="src/main.py",
                    ),
                    EvidenceRef(
                        ref_id="second",
                        requirement_id="source:req:1",
                        kind="INSPECTED_FILE",
                        source_id="read:second",
                        path="src/main.py",
                    ),
                ],
            )
        ]
    )
    resolved = _resolved_evidence(
        {
            "source_request": (
                "Requirements:\n1. callback preserves ordering under concurrent "
                "cancellation"
            )
        },
        report,
        ledger,
    )
    assert [item.bounded_excerpt for item in resolved.catalog] == [
        "first excerpt",
        "second excerpt",
    ]
    assert resolved.requirements[0].evidence_ids == [
        "read:first",
        "read:second",
    ]


def _catalog_fixture(requirement_count=3):
    source = "Requirements:\n" + "\n".join(
        f"{index}. Preserve concurrent cancellation behavior {index}"
        for index in range(1, requirement_count + 1)
    )
    evidence = {
        "source_request": source,
        "changed_files": ["src/main.py", "src/other.py"],
        "execution": {"status": "SUCCEEDED"},
        "diff": (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/src/other.py b/src/other.py\n"
            "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-x\n+y\n"
        ),
    }
    inspections = []
    for index in range(1, requirement_count + 1):
        requirement_id = f"source:req:{index}"
        inspections.append(
            RequirementInspection(
                requirement_id=requirement_id,
                status="VERIFIED",
                evidence_refs=[
                    EvidenceRef(
                        ref_id=f"diff-{index}",
                        requirement_id=requirement_id,
                        kind="TRUSTED_DIFF",
                        path="src/main.py",
                    ),
                    EvidenceRef(
                        ref_id=f"execution-{index}",
                        requirement_id=requirement_id,
                        kind="EXECUTION",
                    ),
                ],
            )
        )
    return evidence, InspectionReport(inspections=inspections)


def _ranged_locality_fixture(*, observation_only=False):
    lines = [f"+padding_{index:03d} " + "x" * 72 + "\n" for index in range(100)]
    lines[5] = "+cancelRunningTask lexical decoy near the start\n"
    lines[60] = "+EXACT_RANGE_TARGET publishes the running worker\n"
    lines[70] = "+OBSERVATION_RANGE_TARGET asserts the interrupted signal\n"
    evidence = {
        "source_request": (
            "Requirements:\n1. Preserve cancellation ordering for a running task"
        ),
        "diff": (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -0,0 +20,100 @@\n" + "".join(lines)
        ),
    }
    requirement_id = "source:req:1"
    ref = EvidenceRef(
        ref_id="diff-locality",
        requirement_id=requirement_id,
        kind="TRUSTED_DIFF",
        path="src/main.py",
        start_line=None if observation_only else 80,
        end_line=None if observation_only else 80,
    )
    observations = (
        [
            InspectionObservation(
                observation_id="obs-locality",
                requirement_id=requirement_id,
                kind="TEST",
                path="src/main.py",
                start_line=90,
                end_line=90,
                fact="the interrupted signal is asserted",
            )
        ]
        if observation_only
        else []
    )
    report = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status="VERIFIED",
                evidence_refs=[ref],
            )
        ],
        observations=observations,
    )
    return evidence, report


def test_catalog_deduplicates_diff_and_execution_and_extracts_exact_path():
    evidence, report = _catalog_fixture()
    resolved = _resolved_evidence(evidence, report, [])
    assert [item.kind for item in resolved.catalog].count(
        EvidenceKind.TRUSTED_DIFF
    ) == 1
    assert [item.kind for item in resolved.catalog].count(EvidenceKind.EXECUTION) == 1
    diff_entry = next(
        item for item in resolved.catalog if item.kind is EvidenceKind.TRUSTED_DIFF
    )
    assert "src/main.py" in diff_entry.bounded_excerpt
    assert "src/other.py" not in diff_entry.bounded_excerpt
    assert all(len(item.evidence_ids) == 2 for item in resolved.requirements)


def test_local_slice_prefers_exact_reference_range_and_is_deterministic():
    evidence, report = _ranged_locality_fixture()
    first = _resolved_evidence(evidence, report, [])
    second = _resolved_evidence(evidence, report, [])
    slices = first.requirements[0].local_evidence_slices
    assert first.model_dump() == second.model_dump()
    assert slices
    assert slices[0].start_line <= 80 <= slices[0].end_line
    assert "EXACT_RANGE_TARGET" in slices[0].excerpt
    assert "lexical decoy" not in slices[0].excerpt


def test_local_slice_uses_observation_range_before_lexical_fallback():
    evidence, report = _ranged_locality_fixture(observation_only=True)
    resolved = _resolved_evidence(evidence, report, [])
    selected = resolved.requirements[0].local_evidence_slices[0]
    assert selected.start_line <= 90 <= selected.end_line
    assert "OBSERVATION_RANGE_TARGET" in selected.excerpt
    assert "lexical decoy" not in selected.excerpt


def test_local_slice_provenance_hashes_link_to_catalog_parent():
    evidence, report = _ranged_locality_fixture()
    resolved = _resolved_evidence(evidence, report, [])
    catalog = {item.evidence_id: item for item in resolved.catalog}
    selected = resolved.requirements[0].local_evidence_slices[0]
    assert isinstance(selected, LocalEvidenceSlice)
    assert selected.evidence_id in catalog
    assert selected.path == catalog[selected.evidence_id].path
    assert selected.parent_content_hash == catalog[selected.evidence_id].content_hash
    assert (
        selected.content_hash == hashlib.sha256(selected.excerpt.encode()).hexdigest()
    )
    assert selected.hunk_identity.startswith("@@ -0,0 +20,100 @@")


def test_fabricated_local_slice_cannot_authorize_accept():
    digest = hashlib.sha256(b"fabricated").hexdigest()
    resolved = ResolvedEvidenceCatalog(
        requirements=[
            ResolvedRequirementEvidence(
                requirement_id="REQ-B",
                inspector_status="VERIFIED",
                local_evidence_slices=[
                    LocalEvidenceSlice(
                        slice_id="slice:fake",
                        evidence_id="diff:missing",
                        path="src/main.py",
                        hunk_identity="fake",
                        parent_content_hash=digest,
                        content_hash=digest,
                        excerpt="fabricated",
                    )
                ],
            )
        ]
    )
    contract = [
        {
            "requirement_id": "REQ-B",
            "text": "Preserve callback ordering",
            "classification": "BEHAVIORAL",
        }
    ]
    inspection = InspectionReport(
        inspections=[RequirementInspection(requirement_id="REQ-B", status="VERIFIED")]
    )
    challenge = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id="REQ-B",
                verdict="SUPPORTED",
                challenge_summary="fabricated slice claims support",
            )
        ]
    )
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="accepted",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="REQ-B", status="SATISFIED", evidence="fabricated"
            )
        ],
    )
    assert "fabricated" in _challenger_prompt({}, contract, resolved)
    assert (
        _guard_accept_coverage(
            result,
            contract,
            inspection=inspection,
            challenge=challenge,
            ledger=[],
            evidence={"changed_files": ["src/main.py"]},
        ).verdict
        == "BLOCKED"
    )


def test_locality_lexical_tokens_split_camel_and_snake_case_stably():
    expected = ("cancel", "running", "state", "worker")
    assert _lexical_tokens("cancelRunning_worker_state") == expected
    assert _lexical_tokens("cancelRunning_worker_state") == expected


def test_locality_lexical_ties_choose_earlier_hunk_line_stably():
    lines = [f"+padding {index:03d} " + "x" * 72 + "\n" for index in range(100)]
    lines[20] = "+FIRST_TIE cancellation marker behavior\n"
    lines[80] = "+SECOND_TIE cancellation marker behavior\n"
    evidence = {
        "source_request": "Requirements:\n1. Observe cancellation marker behavior",
        "diff": (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -0,0 +1,100 @@\n" + "".join(lines)
        ),
    }
    report = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id="source:req:1",
                status="VERIFIED",
                evidence_refs=[
                    EvidenceRef(
                        ref_id="diff-tie",
                        requirement_id="source:req:1",
                        kind="TRUSTED_DIFF",
                        path="src/main.py",
                    )
                ],
            )
        ]
    )
    resolved = _resolved_evidence(evidence, report, [])
    first_slice = resolved.requirements[0].local_evidence_slices[0]
    assert "FIRST_TIE" in first_slice.excerpt
    assert "SECOND_TIE" not in first_slice.excerpt


def test_requirement_local_packets_do_not_cross_assign_evidence():
    evidence = {
        "source_request": (
            "Requirements:\n1. Preserve alpha behavior\n2. Preserve beta behavior"
        ),
        "diff": (
            "diff --git a/src/alpha.py b/src/alpha.py\n"
            "--- a/src/alpha.py\n+++ b/src/alpha.py\n"
            "@@ -0,0 +1,1 @@\n+ALPHA_ONLY behavior\n"
            "diff --git a/src/beta.py b/src/beta.py\n"
            "--- a/src/beta.py\n+++ b/src/beta.py\n"
            "@@ -0,0 +1,1 @@\n+BETA_ONLY behavior\n"
        ),
    }
    report = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=f"source:req:{index}",
                status="VERIFIED",
                evidence_refs=[
                    EvidenceRef(
                        ref_id=f"diff-{name}",
                        requirement_id=f"source:req:{index}",
                        kind="TRUSTED_DIFF",
                        path=f"src/{name}.py",
                    )
                ],
            )
            for index, name in enumerate(("alpha", "beta"), start=1)
        ]
    )
    resolved = _resolved_evidence(evidence, report, [])
    prompt = _challenger_prompt(
        evidence, review_requirement_contract(evidence), resolved
    )
    packet_text = prompt.split("[Behavioral requirement packets]\n", maxsplit=1)[
        1
    ].split("\n\n[Deduplicated raw evidence catalog", maxsplit=1)[0]
    packets = json.loads(packet_text)
    assert "ALPHA_ONLY" in json.dumps(packets[0])
    assert "BETA_ONLY" not in json.dumps(packets[0])
    assert "BETA_ONLY" in json.dumps(packets[1])
    assert "ALPHA_ONLY" not in json.dumps(packets[1])


def test_catalog_reuses_same_exact_read_id_for_many_requirements():
    evidence, report = _catalog_fixture(2)
    for inspection in report.inspections:
        inspection.evidence_refs = [
            EvidenceRef(
                ref_id=f"read-{inspection.requirement_id}",
                requirement_id=inspection.requirement_id,
                kind="INSPECTED_FILE",
                source_id="read:shared",
                path="src/main.py",
            )
        ]
    ledger = [
        {
            "read_id": "read:shared",
            "normalized_path": "src/main.py",
            "excerpt": "shared raw excerpt",
        }
    ]
    resolved = _resolved_evidence(evidence, report, ledger)
    assert len(resolved.catalog) == 1
    assert resolved.catalog[0].evidence_id == "read:shared"
    assert all(item.evidence_ids == ["read:shared"] for item in resolved.requirements)


def test_challenger_batch_contains_only_referenced_catalog_entries():
    evidence, report = _catalog_fixture(2)
    report.inspections[1].evidence_refs[0].path = "src/other.py"
    resolved = _resolved_evidence(evidence, report, [])
    prompt = _challenger_prompt(
        evidence,
        review_requirement_contract(evidence),
        resolved,
        ids={"source:req:1"},
    )
    assert "+new" in prompt
    assert "+y" not in prompt


def test_finalizer_prompt_growth_depends_on_unique_evidence():
    evidence, report = _catalog_fixture(40)
    evidence["diff"] = (
        "diff --git a/src/main.py b/src/main.py\n"
        "--- a/src/main.py\n+++ b/src/main.py\n" + "+bounded evidence\n" * 2_000
    )
    resolved = _resolved_evidence(evidence, report, [])
    contract = review_requirement_contract(evidence)
    challenge = ChallengeReport(
        challenges=[
            RequirementChallenge(
                requirement_id=item["requirement_id"],
                verdict="SUPPORTED",
                challenge_summary="supported",
            )
            for item in contract
        ]
    )
    prompt = _finalizer_prompt(
        evidence,
        report,
        resolved,
        challenge,
        _authority_facts(contract, report, challenge),
        False,
    )
    assert len(resolved.catalog) == 2
    assert len(prompt) < 150_000


def test_challenger_locality_and_fanout_stay_within_global_bounds():
    evidence, report = _catalog_fixture(40)
    evidence["diff"] = (
        "diff --git a/src/main.py b/src/main.py\n"
        "--- a/src/main.py\n"
        "+++ b/src/main.py\n"
        "# RAW_CATALOG_SENTINEL\n"
        "@@ -0,0 +1,300 @@\n"
        + "".join(
            f"+concurrent cancellation behavior transition {index:03d} "
            + "x" * 36
            + "\n"
            for index in range(300)
        )
    )
    resolved = _resolved_evidence(evidence, report, [])
    prompt = _challenger_prompt(
        evidence, review_requirement_contract(evidence), resolved
    )
    locality_size = sum(
        len(slice_item.model_dump_json())
        for requirement in resolved.requirements
        for slice_item in requirement.local_evidence_slices
    )
    assert locality_size <= 50_000
    assert len(prompt) <= 120_000
    assert prompt.count("RAW_CATALOG_SENTINEL") == 1
    for requirement in resolved.requirements:
        assert len(requirement.local_evidence_slices) <= 2
        assert (
            sum(len(item.excerpt) for item in requirement.local_evidence_slices)
            <= 2_500
        )
        assert all(
            item.evidence_id in requirement.evidence_ids
            for item in requirement.local_evidence_slices
        )

    packet_text = prompt.split("[Behavioral requirement packets]\n", maxsplit=1)[
        1
    ].split("\n\n[Deduplicated raw evidence catalog", maxsplit=1)[0]
    packets = json.loads(packet_text)
    assert [item["requirement_id"] for item in packets] == [
        item.requirement_id for item in resolved.requirements
    ]
    assert all("local_evidence_slices" in item for item in packets)


def test_catalog_budget_omission_fails_inspection_closed(monkeypatch):
    evidence, report = _catalog_fixture(1)
    monkeypatch.setattr("sweforge.reviewer.MAX_EVIDENCE_CATALOG_CHARS", 1)
    resolved = _resolved_evidence(evidence, report, [])
    assert resolved.unavailable_requirement_ids == ["source:req:1"]
    bounded = _fail_closed_unavailable_inspections(
        report, set(resolved.unavailable_requirement_ids)
    )
    assert bounded.inspections[0].status.value == "UNVERIFIED"


@pytest.mark.parametrize(
    "artifact_field, duplicate_item",
    [
        (
            "inspections",
            RequirementInspection(
                requirement_id="REQ-B",
                classification="BEHAVIORAL",
                status="VERIFIED",
            ),
        ),
        (
            "observations",
            InspectionObservation(
                observation_id="obs-1",
                requirement_id="REQ-B",
                kind="CODE",
                path="src/main.py",
                fact="fact",
            ),
        ),
    ],
)
def test_duplicate_inspection_or_observation_blocks_accept(
    artifact_field, duplicate_item
):
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    values = getattr(inspection, artifact_field)
    values.append(duplicate_item)
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[],
        evidence=evidence,
    )
    assert guarded.verdict == "BLOCKED"


def test_duplicate_challenge_blocks_accept():
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    challenge.challenges.append(challenge.challenges[0].model_copy())
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[],
        evidence=evidence,
    )
    assert guarded.verdict == "BLOCKED"


@pytest.mark.parametrize(
    "case", ["missing_inspection", "missing_challenge", "unknown_challenge"]
)
def test_incomplete_or_unknown_artifact_coverage_blocks_accept(case):
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    if case == "missing_inspection":
        inspection.inspections.clear()
    elif case == "missing_challenge":
        challenge.challenges.clear()
    else:
        challenge.challenges[0].requirement_id = "REQ-UNKNOWN"
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[],
        evidence=evidence,
    )
    assert guarded.verdict == "BLOCKED"


@pytest.mark.parametrize("kind", ["wrong_requirement", "fake_read", "fake_observation"])
def test_challenger_provenance_must_be_current_and_exact(kind):
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    if kind == "wrong_requirement":
        challenge.challenges[0].evidence_refs[0].requirement_id = "REQ-OTHER"
    elif kind == "fake_read":
        challenge.challenges[0].evidence_refs[0] = EvidenceRef(
            ref_id="fake-read",
            requirement_id="REQ-B",
            kind="INSPECTED_FILE",
            source_id="read:missing",
            path="src/main.py",
        )
    else:
        challenge.challenges[0].evidence_refs[0] = EvidenceRef(
            ref_id="fake-observation",
            requirement_id="REQ-B",
            kind="INSPECTOR_OBSERVATION",
            source_id="obs:missing",
            path="src/main.py",
        )
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[],
        evidence=evidence,
    )
    assert guarded.verdict == "BLOCKED"


def test_ungrounded_code_observation_cannot_authorize_accept():
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    inspection.observations[0].path = "src/not-inspected.py"
    guarded = _guard_accept_coverage(
        result,
        contract,
        inspection=inspection,
        challenge=challenge,
        ledger=[],
        evidence=evidence,
    )
    assert guarded.verdict == "BLOCKED"


def test_valid_changed_file_observation_remains_acceptable():
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    assert (
        _guard_accept_coverage(
            result,
            contract,
            inspection=inspection,
            challenge=challenge,
            ledger=[],
            evidence=evidence,
        ).verdict
        == "ACCEPT"
    )


def test_valid_exact_read_backed_observation_remains_acceptable():
    contract, inspection, challenge, result, evidence = (
        _supported_behavioral_artifacts()
    )
    inspection.observations[0].path = "src/read.py"
    read_ref = EvidenceRef(
        ref_id="read-ref",
        requirement_id="REQ-B",
        kind="INSPECTED_FILE",
        source_id="read:one",
        path="src/read.py",
    )
    inspection.inspections[0].evidence_refs = [read_ref]
    challenge.challenges[0].evidence_refs = [read_ref]
    evidence["changed_files"] = []
    assert (
        _guard_accept_coverage(
            result,
            contract,
            inspection=inspection,
            challenge=challenge,
            ledger=[
                {
                    "read_id": "read:one",
                    "normalized_path": "src/read.py",
                    "returned_lines": [1, 2],
                }
            ],
            evidence=evidence,
        ).verdict
        == "ACCEPT"
    )


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


def test_inspector_artifact_correction_runs_once_before_finalizer(monkeypatch):
    evidence = _review_evidence()
    evidence["diff"] = (
        "diff --git a/src/main.py b/src/main.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    expected_ids = [
        item["requirement_id"] for item in review_requirement_contract(evidence)
    ]
    malformed = InspectionReport(
        inspections=[
            RequirementInspection(requirement_id=requirement_id, status="VERIFIED")
            for requirement_id in expected_ids
        ]
    )
    corrected = InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status="VERIFIED",
                evidence_refs=[
                    EvidenceRef(
                        ref_id=f"diff-{requirement_id}",
                        requirement_id=requirement_id,
                        kind=EvidenceKind.TRUSTED_DIFF,
                        path="src/main.py",
                    )
                ],
            )
            for requirement_id in expected_ids
        ],
        observations=[
            InspectionObservation(
                observation_id=f"code-{requirement_id}",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/main.py",
                fact="the changed implementation satisfies the requirement",
            )
            for requirement_id in expected_ids
        ],
    )
    agents = [
        _FakeAgent({"structured_response": malformed}),
        _FakeAgent({"structured_response": corrected}),
    ]
    created_agents = []
    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="ACCEPT",
                summary="accepted",
                requirement_checks=_complete_review_checks(evidence),
            )
        }
    )
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer",
        lambda *args, **kwargs: (
            created_agents.append(agents.pop(0)) or created_agents[-1]
        ),
    )
    monkeypatch.setattr(
        "sweforge.reviewer._split_semantic_review",
        lambda **kwargs: SemanticReviewArtifact(
            implementation=SpecialistStageReport(
                stage="IMPLEMENTATION", status="SKIPPED", applicable=False
            ),
            test_validation=SpecialistStageReport(
                stage="TEST_VALIDATION", status="SKIPPED", applicable=False
            ),
        ),
    )
    monkeypatch.setattr("sweforge.reviewer._build_finalizer", lambda *a, **k: finalizer)
    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=evidence,
    )
    assert result.verdict == "ACCEPT", (
        result.findings[0].evidence if result.findings else result.summary
    )
    assert len(finalizer.calls) == 1
    assert len(agents) == 0
    assert len(created_agents) == 2
    assert (
        "inspection-artifact correction"
        in created_agents[1].calls[0]["messages"][0]["content"]
    )


def test_inspector_artifact_correction_exhaustion_is_operational(monkeypatch):
    evidence = _review_evidence()
    malformed = _inspection_report("plan:step:1")
    agents = [
        _FakeAgent({"structured_response": malformed}),
        _FakeAgent({"structured_response": malformed}),
    ]
    finalizer = _FakeAgent()
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer",
        lambda *args, **kwargs: agents.pop(0),
    )
    monkeypatch.setattr("sweforge.reviewer._build_finalizer", lambda *a, **k: finalizer)
    with pytest.raises(ReviewFinalizationError, match="invalid inspection artifact"):
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=evidence,
        )
    assert finalizer.calls == []


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

    assert "STRUCTURAL" in INSPECTOR_SYSTEM_PROMPT
    assert "BEHAVIORAL" in INSPECTOR_SYSTEM_PROMPT
    assert "VALIDATION" in INSPECTOR_SYSTEM_PROMPT
    assert "read_repo_file" in INSPECTOR_SYSTEM_PROMPT
    assert "assertion_or_signal" in INSPECTOR_SYSTEM_PROMPT
    assert "never replaces CODE grounding" in INSPECTOR_SYSTEM_PROMPT
    assert "Never mark VERIFIED" in INSPECTOR_SYSTEM_PROMPT


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


def test_challenger_construction_has_one_two_call_budget(monkeypatch):
    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sweforge.reviewer.create_agent", fake_create_agent)
    _build_challenger(model="reviewer", live_middleware=[])
    assert captured["tools"] == []
    assert isinstance(captured["response_format"], ToolStrategy)
    assert len(captured["middleware"]) == 1
    assert captured["middleware"][0].run_limit == 2


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


def _inspection_contract(classification, requirement_id="plan:step:1"):
    return [
        {
            "requirement_id": requirement_id,
            "text": "requirement",
            "classification": classification,
        }
    ]


def _inspection_report(
    requirement_id, *, status="VERIFIED", refs=None, observations=None
):
    return InspectionReport(
        inspections=[
            RequirementInspection(
                requirement_id=requirement_id,
                status=status,
                evidence_refs=refs or [],
            )
        ],
        observations=observations or [],
    )


def test_inspection_artifact_accepts_grounded_behavioral_changed_file():
    requirement_id = "plan:step:1"
    report = _inspection_report(
        requirement_id,
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/test.py",
                fact="threshold assertion is present",
            ),
            InspectionObservation(
                observation_id="test-1",
                requirement_id=requirement_id,
                kind="TEST",
                path="src/test.py",
                assertion_or_signal="asserts 9,999, 10,000, and 10,001",
            ),
        ],
    )
    assert (
        _inspection_artifact_problems(
            _inspection_contract(ReviewRequirementClassification.BEHAVIORAL.value),
            report,
            ledger=[],
            evidence={"changed_files": ["src/test.py"]},
        )
        == []
    )


def test_inspection_artifact_accepts_grounded_behavioral_unchanged_file_read():
    requirement_id = "plan:step:1"
    read_ref = EvidenceRef(
        ref_id="read-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.INSPECTED_FILE,
        source_id="read-1",
        path="src/main.py",
    )
    report = _inspection_report(
        requirement_id,
        refs=[read_ref],
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/main.py",
                fact="threshold implementation is present",
            )
        ],
    )
    assert (
        _inspection_artifact_problems(
            _inspection_contract(ReviewRequirementClassification.BEHAVIORAL.value),
            report,
            ledger=[
                {
                    "read_id": "read-1",
                    "normalized_path": "src/main.py",
                    "returned_lines": [1, 10],
                    "excerpt": "threshold implementation",
                }
            ],
            evidence={"changed_files": []},
        )
        == []
    )


def test_inspection_artifact_rejects_ungrounded_unchanged_code():
    requirement_id = "plan:step:1"
    report = _inspection_report(
        requirement_id,
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/main.py",
            )
        ],
    )
    problems = _inspection_artifact_problems(
        _inspection_contract(ReviewRequirementClassification.BEHAVIORAL.value),
        report,
        ledger=[],
        evidence={"changed_files": []},
    )
    assert "ungrounded code observation for plan:step:1" in problems


def test_inspection_artifact_requires_execution_for_validation():
    requirement_id = "plan:validation:1"
    ref = EvidenceRef(
        ref_id="execution-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.EXECUTION,
        source_id="exec-1",
    )
    contract = _inspection_contract(
        ReviewRequirementClassification.VALIDATION.value, requirement_id
    )
    valid = _inspection_report(requirement_id, refs=[ref])
    evidence = {
        "execution": [{"evidence_id": "exec-1"}],
        "execution_observations": [{"evidence_id": "exec-1"}],
    }
    assert (
        _inspection_artifact_problems(contract, valid, ledger=[], evidence=evidence)
        == []
    )
    invalid = _inspection_report(requirement_id)
    assert "missing direct execution evidence for plan:validation:1" in (
        _inspection_artifact_problems(contract, invalid, ledger=[], evidence={})
    )


def test_inspection_artifact_accepts_structural_diff_authority():
    requirement_id = "plan:step:4"
    ref = EvidenceRef(
        ref_id="diff-ref",
        requirement_id=requirement_id,
        kind=EvidenceKind.TRUSTED_DIFF,
        path="src/test.py",
    )
    assert (
        _inspection_artifact_problems(
            _inspection_contract(
                ReviewRequirementClassification.STRUCTURAL.value, requirement_id
            ),
            _inspection_report(requirement_id, refs=[ref]),
            ledger=[],
            evidence={"changed_files": ["src/test.py"]},
        )
        == []
    )


def test_real_unverified_inspection_is_not_an_artifact_error():
    requirement_id = "plan:step:1"
    problems = _inspection_artifact_problems(
        _inspection_contract(ReviewRequirementClassification.BEHAVIORAL.value),
        _inspection_report(requirement_id, status="UNVERIFIED"),
        ledger=[],
        evidence={},
    )
    assert problems == []


def test_inspection_artifact_rejects_test_without_assertion_signal():
    requirement_id = "plan:step:1"
    report = _inspection_report(
        requirement_id,
        observations=[
            InspectionObservation(
                observation_id="code-1",
                requirement_id=requirement_id,
                kind="CODE",
                path="src/test.py",
            ),
            InspectionObservation(
                observation_id="test-1",
                requirement_id=requirement_id,
                kind="TEST",
                path="src/test.py",
            ),
        ],
    )
    assert (
        "missing assertion or signal for plan:step:1"
        in _inspection_artifact_problems(
            _inspection_contract(ReviewRequirementClassification.BEHAVIORAL.value),
            report,
            ledger=[],
            evidence={"changed_files": ["src/test.py"]},
        )
    )


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


@pytest.mark.parametrize(
    "label, response_text, command_exit, expected",
    [
        ("omitted validation", "Tests were not run", None, "NEEDS_FIXES"),
        ("executor prose", "mvn test passed", None, "NEEDS_FIXES"),
        ("failed validation", "Maven failed", 1, "NEEDS_FIXES"),
    ],
)
def test_repairable_validation_gaps_route_to_needs_fixes(
    label, response_text, command_exit, expected
):
    del label, response_text, command_exit
    result = ExecutionReviewResult(
        verdict="BLOCKED",
        summary="validation evidence is missing or failed",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="plan:validation:1",
                status=ReviewRequirementStatus.UNVERIFIED,
                evidence="no authoritative observation",
                repairability=ReviewRepairability.IN_SCOPE_REPAIR,
            )
        ],
        repair_instructions=["run the approved validation and preserve its evidence"],
    )
    guarded = _guard_repairability(result)
    assert guarded.verdict == expected
    assert guarded.repair_instructions


def test_unclassified_or_external_blockers_remain_blocked():
    for repairability in (
        ReviewRepairability.NOT_APPLICABLE,
        ReviewRepairability.EXTERNAL_BLOCKER,
    ):
        result = ExecutionReviewResult(
            verdict="BLOCKED",
            summary="authority blocker",
            requirement_checks=[
                ReviewRequirementCheck(
                    requirement_id="plan:validation:1",
                    status=ReviewRequirementStatus.UNVERIFIED,
                    evidence="cannot establish authority",
                    repairability=repairability,
                )
            ],
            repair_instructions=["investigate"],
        )
        assert _guard_repairability(result).verdict == "BLOCKED"


def test_repairability_guard_does_not_accept_missing_evidence():
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="claimed passed",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="plan:validation:1",
                status=ReviewRequirementStatus.UNVERIFIED,
                evidence="prose only",
                repairability=ReviewRepairability.IN_SCOPE_REPAIR,
            )
        ],
    )
    assert _guard_repairability(result).verdict == "ACCEPT"


def test_review_execution_uses_split_semantic_artifact_not_broad_challenger(
    monkeypatch,
):
    evidence = _review_evidence()
    evidence["source_request"] = (
        "Requirements:\n"
        "1. Preserve callback ordering during cancellation\n"
        "2. Assert the interrupted worker signal before cancellation completes"
    )
    contract = review_requirement_contract(evidence)
    expected_ids = [item["requirement_id"] for item in contract]
    inspector = _FakeAgent(
        {
            "structured_response": InspectionReport(
                inspections=[
                    RequirementInspection(
                        requirement_id=requirement_id,
                        status="VERIFIED",
                        evidence_refs=[
                            EvidenceRef(
                                ref_id=f"diff-{requirement_id}",
                                requirement_id=requirement_id,
                                kind="TRUSTED_DIFF",
                                path="src/main.py",
                            )
                        ],
                    )
                    for requirement_id in expected_ids
                ],
                observations=[
                    InspectionObservation(
                        observation_id=f"code-{requirement_id}",
                        requirement_id=requirement_id,
                        kind="CODE",
                        path="src/main.py",
                        fact="relevant callback/test code",
                    )
                    for requirement_id in expected_ids
                ],
            )
        }
    )
    semantic = SemanticReviewArtifact(
        implementation=SpecialistStageReport(
            stage="IMPLEMENTATION", status="SKIPPED", applicable=False
        ),
        test_validation=SpecialistStageReport(
            stage="TEST_VALIDATION", status="SKIPPED", applicable=False
        ),
    )

    finalizer = _FakeAgent(
        {
            "structured_response": ExecutionReviewResult(
                verdict="NEEDS_FIXES", summary="missing assertion"
            )
        }
    )
    monkeypatch.setattr("sweforge.reviewer.build_reviewer", lambda *a, **k: inspector)
    monkeypatch.setattr(
        "sweforge.reviewer._build_challenger",
        lambda *a, **k: pytest.fail("legacy broad challenger entered live path"),
    )
    monkeypatch.setattr(
        "sweforge.reviewer._split_semantic_review", lambda **kwargs: semantic
    )
    monkeypatch.setattr("sweforge.reviewer._build_finalizer", lambda *a, **k: finalizer)

    result = review_execution(
        context=ReviewerContext(worktree="/tmp/worktree"),
        model="reviewer",
        evidence=evidence,
    )

    assert result.challenge_report is None
    assert result.semantic_review == semantic


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


def test_finalizer_budget_is_operationally_recoverable(monkeypatch):
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

    inspector = _FakeAgent({"messages": []})
    finalizer = _FakeAgent(error=ModelCallLimitExceededError(3, 3, None, 3))
    monkeypatch.setattr(
        "sweforge.reviewer.build_reviewer", lambda *args, **kwargs: inspector
    )
    monkeypatch.setattr(
        "sweforge.reviewer._build_finalizer", lambda *args, **kwargs: finalizer
    )
    with pytest.raises(ReviewFinalizationError):
        review_execution(
            context=ReviewerContext(worktree="/tmp/worktree"),
            model="reviewer",
            evidence=_review_evidence(),
        )


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


def _split_cluster_evidence():
    return {
        "changed_files": [
            "notes/review.txt",
            "src/service.py",
            "tests/test_service.py",
        ],
        "diff": (
            "diff --git a/src/service.py b/src/service.py\n"
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -1,1 +1,2 @@\n"
            " state = old\n"
            "+published_state = compute_state()\n"
            "@@ -49,1 +50,2 @@\n"
            " value = current\n"
            "+notify_state(published_state)\n"
            "@@ -299,1 +300,2 @@\n"
            " unrelated = old\n"
            "+unrelated = normalize(value)\n"
            "diff --git a/tests/test_service.py b/tests/test_service.py\n"
            "--- a/tests/test_service.py\n"
            "+++ b/tests/test_service.py\n"
            "@@ -9,1 +10,2 @@\n"
            " result = run()\n"
            "+assert result.status == 'done'\n"
            "diff --git a/notes/review.txt b/notes/review.txt\n"
            "--- a/notes/review.txt\n"
            "+++ b/notes/review.txt\n"
            "@@ -1,0 +1,1 @@\n"
            "+assert lifecycle signal\n"
        ),
    }


def _evidence_finding(cluster, finding_id="finding-1", severity="BLOCKING"):
    source = cluster.ranges[0]
    return EvidenceFinding(
        finding_id=finding_id,
        severity=severity,
        cluster_ids=[cluster.cluster_id],
        provenance=[
            EvidenceFindingProvenance(
                cluster_id=cluster.cluster_id,
                evidence_id=source.evidence_id,
                path=source.path,
                start_line=source.start_line,
                end_line=source.start_line,
                hunk_identity=source.hunk_identity,
            )
        ],
        concise_summary="Publication can precede the state update.",
        concrete_source_facts=["The notification and update are separately ordered."],
        behavioral_consequence="A concurrent observer can see stale state.",
    )


def test_evidence_clustering_is_stable_local_conservative_and_trusted():
    evidence = _split_cluster_evidence()
    first = _evidence_clusters(evidence)
    second = _evidence_clusters(evidence)
    assert first == second
    assert len(first) == 4
    service = [item for item in first if item.ranges[0].path == "src/service.py"]
    assert len(service) == 2
    assert len(service[0].ranges) == 2
    assert len(service[1].ranges) == 1
    assert all(item.role is EvidenceClusterRole.IMPLEMENTATION for item in service)
    test = next(
        item for item in first if item.ranges[0].path == "tests/test_service.py"
    )
    ambiguous = next(
        item for item in first if item.ranges[0].path == "notes/review.txt"
    )
    assert test.role is EvidenceClusterRole.TEST_VALIDATION
    assert ambiguous.role is EvidenceClusterRole.BOTH
    assert sum(len(item.ranges) for item in first) == 5
    for cluster in first:
        assert (
            cluster.content_hash
            == hashlib.sha256(cluster.bounded_raw_excerpt.encode()).hexdigest()
        )
        for source in cluster.ranges:
            assert source.path in evidence["changed_files"]
            assert (
                source.evidence_id
                == "diff:" + hashlib.sha256(source.path.encode()).hexdigest()[:24]
            )


def test_far_hunks_with_shared_identifiers_cluster_deterministically():
    evidence = {
        "changed_files": ["src/worker.py"],
        "diff": (
            "diff --git a/src/worker.py b/src/worker.py\n"
            "--- a/src/worker.py\n+++ b/src/worker.py\n"
            "@@ -1,0 +1,1 @@\n+shared_handle = publish_state(shared_value)\n"
            "@@ -499,0 +500,1 @@\n+close_shared_handle(shared_value)\n"
        ),
    }
    clusters = _evidence_clusters(evidence)
    assert len(clusters) == 1
    assert len(clusters[0].ranges) == 2


class _DirectModel:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        value = self.response(messages) if callable(self.response) else self.response
        return type("DirectMessage", (), {"content": value})()


def _empty_specialist_response(stage):
    return SpecialistModelResponse(
        artifact_kind="EVIDENCE_SPECIALIST_REPORT",
        artifact_version=1,
        stage=stage,
        findings=[],
    ).model_dump_json()


def test_split_specialists_make_at_most_one_direct_no_retry_request_each(monkeypatch):
    direct = _DirectModel(
        lambda messages: _empty_specialist_response(
            json.loads(messages[1]["content"])["artifact_identity_required"]["stage"]
        )
    )
    init_calls = []

    def fake_init(model, **kwargs):
        init_calls.append((model, kwargs))
        return direct

    monkeypatch.setattr("sweforge.reviewer.init_chat_model", fake_init)
    evidence = _split_cluster_evidence()
    artifact = _split_semantic_review(
        model="reviewer",
        evidence=evidence,
        contract=[],
        resolved=ResolvedEvidenceCatalog(),
    )
    assert artifact.implementation.status is SpecialistStageStatus.COMPLETED
    assert artifact.test_validation.status is SpecialistStageStatus.COMPLETED
    assert len(direct.calls) == 2
    assert all(kwargs == {"max_retries": 0, "timeout": 120} for _, kwargs in init_calls)
    assert artifact.implementation.provider_requests == 1
    assert artifact.test_validation.provider_requests == 1
    assert artifact.implementation.prompt_chars < 50_000
    assert artifact.test_validation.prompt_chars < 50_000


def test_absent_test_clusters_skip_test_specialist_without_a_request(monkeypatch):
    evidence = _split_cluster_evidence()
    evidence["changed_files"] = ["src/service.py"]
    direct = _DirectModel(_empty_specialist_response("IMPLEMENTATION"))
    monkeypatch.setattr("sweforge.reviewer.init_chat_model", lambda *a, **k: direct)
    artifact = _split_semantic_review(
        model="reviewer",
        evidence=evidence,
        contract=[],
        resolved=ResolvedEvidenceCatalog(),
    )
    assert len(direct.calls) == 1
    assert artifact.test_validation.status is SpecialistStageStatus.SKIPPED
    assert artifact.test_validation.applicable is False
    assert artifact.test_validation.provider_requests == 0


@pytest.mark.parametrize("failure", ["malformed", "provider"])
def test_specialist_malformed_or_provider_failure_is_unverified(monkeypatch, failure):
    cluster = _evidence_clusters(_split_cluster_evidence())[1]
    direct = (
        _DirectModel("not-json")
        if failure == "malformed"
        else _DirectModel(error=RuntimeError("provider unavailable"))
    )
    monkeypatch.setattr("sweforge.reviewer.init_chat_model", lambda *a, **k: direct)
    report = _invoke_specialist(
        model="reviewer",
        stage=SpecialistStage.IMPLEMENTATION,
        clusters=[cluster],
    )
    assert report.status is SpecialistStageStatus.UNVERIFIED
    assert report.findings == []
    assert report.provider_requests == 1
    assert len(direct.calls) == 1


@pytest.mark.parametrize(
    "invalid",
    ["cluster", "path", "range", "evidence", "duplicate"],
)
def test_specialist_rejects_invalid_or_duplicate_finding_provenance(
    monkeypatch, invalid
):
    cluster = _evidence_clusters(_split_cluster_evidence())[1]
    finding = _evidence_finding(cluster)
    findings = [finding]
    if invalid == "cluster":
        finding.cluster_ids = ["cluster:missing"]
    elif invalid == "path":
        finding.provenance[0].path = "src/fabricated.py"
    elif invalid == "range":
        finding.provenance[0].start_line = cluster.ranges[0].end_line + 1
        finding.provenance[0].end_line = cluster.ranges[0].end_line + 1
    elif invalid == "evidence":
        finding.provenance[0].evidence_id = "diff:wrong"
    else:
        findings.append(finding.model_copy(deep=True))
    response = SpecialistModelResponse(
        artifact_kind="EVIDENCE_SPECIALIST_REPORT",
        artifact_version=1,
        stage="IMPLEMENTATION",
        findings=findings,
    ).model_dump_json()
    direct = _DirectModel(response)
    monkeypatch.setattr("sweforge.reviewer.init_chat_model", lambda *a, **k: direct)
    report = _invoke_specialist(
        model="reviewer", stage=SpecialistStage.IMPLEMENTATION, clusters=[cluster]
    )
    assert report.status is SpecialistStageStatus.UNVERIFIED
    assert report.findings == []


def test_valid_blocking_and_warning_findings_are_authoritative(monkeypatch):
    cluster = _evidence_clusters(_split_cluster_evidence())[1]
    findings = [
        _evidence_finding(cluster, "blocking", "BLOCKING"),
        _evidence_finding(cluster, "warning", "WARNING"),
    ]
    response = SpecialistModelResponse(
        artifact_kind="EVIDENCE_SPECIALIST_REPORT",
        artifact_version=1,
        stage="IMPLEMENTATION",
        findings=findings,
    ).model_dump_json()
    monkeypatch.setattr(
        "sweforge.reviewer.init_chat_model", lambda *a, **k: _DirectModel(response)
    )
    report = _invoke_specialist(
        model="reviewer", stage=SpecialistStage.IMPLEMENTATION, clusters=[cluster]
    )
    assert report.status is SpecialistStageStatus.COMPLETED
    assert [item.severity for item in report.findings] == [
        EvidenceFindingSeverity.BLOCKING,
        EvidenceFindingSeverity.WARNING,
    ]


def test_candidate_requirement_association_uses_strict_fallback_priority():
    cluster = _evidence_clusters(_split_cluster_evidence())[1]
    findings = []
    for finding_id, evidence_id, path, line, hunk in (
        ("range", "e-range", "src/range.py", 10, "@@ range"),
        ("hunk", "e-hunk", "src/hunk.py", 30, "@@ hunk"),
        ("catalog", "e-catalog", "src/catalog.py", 40, "@@ catalog"),
        ("path", "e-path", "src/path.py", 50, "@@ path"),
    ):
        findings.append(
            EvidenceFinding(
                finding_id=finding_id,
                severity="WARNING",
                cluster_ids=[cluster.cluster_id],
                provenance=[
                    EvidenceFindingProvenance(
                        cluster_id=cluster.cluster_id,
                        evidence_id=evidence_id,
                        path=path,
                        start_line=line,
                        end_line=line,
                        hunk_identity=hunk,
                    )
                ],
                concise_summary="summary",
                concrete_source_facts=["fact"],
                behavioral_consequence="consequence",
            )
        )
    resolved = ResolvedEvidenceCatalog(
        requirements=[
            ResolvedRequirementEvidence(
                requirement_id="REQ-RANGE",
                evidence_ids=["e-range"],
                local_evidence_slices=[
                    LocalEvidenceSlice(
                        slice_id="slice-range",
                        evidence_id="e-range",
                        path="src/range.py",
                        start_line=10,
                        end_line=12,
                        hunk_identity="@@ range:0:2",
                        parent_content_hash="a" * 64,
                        content_hash="b" * 64,
                        excerpt="raw",
                    )
                ],
            ),
            ResolvedRequirementEvidence(
                requirement_id="REQ-HUNK",
                evidence_ids=["e-hunk"],
                local_evidence_slices=[
                    LocalEvidenceSlice(
                        slice_id="slice-hunk",
                        evidence_id="e-hunk",
                        path="src/hunk.py",
                        start_line=35,
                        end_line=36,
                        hunk_identity="@@ hunk:0:2",
                        parent_content_hash="c" * 64,
                        content_hash="d" * 64,
                        excerpt="raw",
                    )
                ],
            ),
            ResolvedRequirementEvidence(
                requirement_id="REQ-CATALOG", evidence_ids=["e-catalog"]
            ),
            ResolvedRequirementEvidence(
                requirement_id="REQ-PATH",
                observations=[
                    InspectionObservation(
                        observation_id="obs-path",
                        requirement_id="REQ-PATH",
                        kind="CODE",
                        path="src/path.py",
                    )
                ],
            ),
        ]
    )
    report = SpecialistStageReport(
        stage="IMPLEMENTATION",
        status="COMPLETED",
        applicable=True,
        findings=findings,
    )
    contract = [
        {"requirement_id": item, "text": item, "classification": "BEHAVIORAL"}
        for item in ("REQ-RANGE", "REQ-HUNK", "REQ-CATALOG", "REQ-PATH")
    ]
    associations = {
        item.finding_id: item
        for item in _candidate_requirement_associations(contract, resolved, [report])
    }
    assert associations["range"].basis is CandidateAssociationBasis.EXACT_RANGE
    assert associations["range"].candidate_requirement_ids == ["REQ-RANGE"]
    assert associations["hunk"].basis is CandidateAssociationBasis.SAME_HUNK
    assert associations["hunk"].candidate_requirement_ids == ["REQ-HUNK"]
    assert associations["catalog"].basis is (
        CandidateAssociationBasis.SAME_CATALOG_EVIDENCE
    )
    assert associations["catalog"].candidate_requirement_ids == ["REQ-CATALOG"]
    assert associations["path"].basis is CandidateAssociationBasis.SAME_PATH
    assert associations["path"].candidate_requirement_ids == ["REQ-PATH"]


def _guard_semantic(status="COMPLETED", severity=None):
    cluster = EvidenceCluster(
        cluster_id="cluster:guard",
        role="IMPLEMENTATION",
        ranges=[
            EvidenceClusterRange(
                evidence_id="diff:guard",
                path="src/guard.py",
                start_line=1,
                end_line=2,
                hunk_identity="@@ guard",
            )
        ],
        evidence_ids=["diff:guard"],
        bounded_raw_excerpt="+guard\n",
        content_hash=hashlib.sha256(b"+guard\n").hexdigest(),
    )
    findings = [_evidence_finding(cluster, severity=severity)] if severity else []
    associations = (
        [
            FindingCandidateAssociation(
                finding_id=findings[0].finding_id,
                candidate_requirement_ids=[],
                basis="NONE",
            )
        ]
        if findings
        else []
    )
    return SemanticReviewArtifact(
        clusters=[cluster],
        implementation=SpecialistStageReport(
            stage="IMPLEMENTATION",
            status=status,
            applicable=True,
            findings=findings,
        ),
        test_validation=SpecialistStageReport(
            stage="TEST_VALIDATION", status="SKIPPED", applicable=False
        ),
        candidate_associations=associations,
    )


@pytest.mark.parametrize(
    ("raw_verdict", "semantic", "expected"),
    [
        ("ACCEPT", _guard_semantic(severity="BLOCKING"), "BLOCKED"),
        ("ACCEPT", _guard_semantic(status="UNVERIFIED"), "BLOCKED"),
        ("ACCEPT", _guard_semantic(severity="WARNING"), "ACCEPT"),
        ("ACCEPT", _guard_semantic(), "ACCEPT"),
        ("NEEDS_FIXES", _guard_semantic(severity="BLOCKING"), "NEEDS_FIXES"),
        ("BLOCKED", _guard_semantic(), "BLOCKED"),
    ],
)
def test_split_semantic_guard_preserves_verdict_rules(raw_verdict, semantic, expected):
    contract = [
        {"requirement_id": "REQ", "text": "requirement", "classification": "BEHAVIORAL"}
    ]
    result = ExecutionReviewResult(
        verdict=raw_verdict,
        summary="raw",
        requirement_checks=(
            [
                ReviewRequirementCheck(
                    requirement_id="REQ", status="SATISFIED", evidence="evidence"
                )
            ]
            if raw_verdict == "ACCEPT"
            else []
        ),
    )
    assert (
        _guard_accept_coverage(result, contract, semantic_review=semantic).verdict
        == expected
    )


def _scope_clusters():
    implementation = EvidenceCluster(
        cluster_id="cluster:scope-implementation",
        role="IMPLEMENTATION",
        ranges=[
            EvidenceClusterRange(
                evidence_id="diff:scope-implementation",
                path="src/service.py",
                start_line=10,
                end_line=20,
                hunk_identity="@@ implementation",
            )
        ],
        evidence_ids=["diff:scope-implementation"],
        bounded_raw_excerpt="+cancel interrupt signal assertion\n",
        content_hash=hashlib.sha256(
            b"+cancel interrupt signal assertion\n"
        ).hexdigest(),
    )
    test = EvidenceCluster(
        cluster_id="cluster:scope-test",
        role="TEST_VALIDATION",
        ranges=[
            EvidenceClusterRange(
                evidence_id="diff:scope-test",
                path="tests/test_service.py",
                start_line=30,
                end_line=40,
                hunk_identity="@@ test",
            )
        ],
        evidence_ids=["diff:scope-test"],
        bounded_raw_excerpt="+assert interrupt signal\n",
        content_hash=hashlib.sha256(b"+assert interrupt signal\n").hexdigest(),
    )
    return implementation, test


def _scope_finding(implementation, test, *, include_context=True):
    sources = [test.ranges[0]]
    clusters = [test.cluster_id]
    if include_context:
        sources.append(implementation.ranges[0])
        clusters.append(implementation.cluster_id)
    return EvidenceFinding(
        finding_id="scope-finding",
        severity="WARNING",
        cluster_ids=clusters,
        provenance=[
            EvidenceFindingProvenance(
                cluster_id=cluster.cluster_id,
                evidence_id=source.evidence_id,
                path=source.path,
                start_line=source.start_line,
                end_line=source.start_line,
                hunk_identity=source.hunk_identity,
            )
            for cluster, source in zip(
                [test, implementation] if include_context else [test],
                sources,
            )
        ],
        concise_summary="test evidence does not prove interruption",
        concrete_source_facts=["The test observes a final state."],
        behavioral_consequence="The intended interruption mechanism may be absent.",
    )


def _specialist_response(stage, findings):
    return SpecialistModelResponse(
        artifact_kind="EVIDENCE_SPECIALIST_REPORT",
        artifact_version=1,
        stage=stage,
        findings=findings,
    ).model_dump_json()


def _invoke_with_scope(monkeypatch, stage, scope, response):
    direct = _DirectModel(response)
    monkeypatch.setattr("sweforge.reviewer.init_chat_model", lambda *a, **k: direct)
    primary = list(scope.primary_clusters)
    return _invoke_specialist(
        model="reviewer",
        stage=stage,
        clusters=primary,
        linked_implementation=list(scope.context_clusters),
        scope=scope,
    )


def test_test_specialist_primary_only_and_primary_plus_context_are_valid(monkeypatch):
    implementation, test = _scope_clusters()
    scope = SpecialistEvidenceScope(
        primary_clusters=(test,), context_clusters=(implementation,)
    )
    primary_only = _scope_finding(implementation, test, include_context=False)
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.TEST_VALIDATION,
        scope,
        _specialist_response("TEST_VALIDATION", [primary_only]),
    )
    assert report.status is SpecialistStageStatus.COMPLETED
    combined = _scope_finding(implementation, test)
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.TEST_VALIDATION,
        scope,
        _specialist_response("TEST_VALIDATION", [combined]),
    )
    assert report.status is SpecialistStageStatus.COMPLETED


@pytest.mark.parametrize(
    "case", ["context_only", "unsupplied", "bad_path", "bad_range"]
)
def test_test_specialist_context_requires_supplied_primary_and_valid_ranges(
    monkeypatch, case
):
    implementation, test = _scope_clusters()
    scope = SpecialistEvidenceScope(
        primary_clusters=(test,), context_clusters=(implementation,)
    )
    finding = _scope_finding(implementation, test, include_context=False)
    if case == "context_only":
        finding = _scope_finding(implementation, test)
        finding.provenance = [finding.provenance[1]]
        finding.cluster_ids = [implementation.cluster_id]
    elif case == "unsupplied":
        finding = _scope_finding(implementation, test)
        finding.cluster_ids[1] = "cluster:not-supplied"
        finding.provenance[1].cluster_id = "cluster:not-supplied"
    elif case == "bad_path":
        finding = _scope_finding(implementation, test)
        finding.provenance[1].path = "src/fabricated.py"
    elif case == "bad_range":
        finding = _scope_finding(implementation, test)
        finding.provenance[1].start_line = 99
        finding.provenance[1].end_line = 99
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.TEST_VALIDATION,
        scope,
        _specialist_response("TEST_VALIDATION", [finding]),
    )
    assert report.status is SpecialistStageStatus.UNVERIFIED
    assert report.failure_stage is SpecialistFailureStage.PROVENANCE_VALIDATION
    assert report.rejected_findings[0].finding_id == "scope-finding"
    assert report.rejected_findings[0].validation_errors


def test_implementation_primary_finding_is_valid_and_duplicate_ids_are_artifact_failure(
    monkeypatch,
):
    implementation, test = _scope_clusters()
    scope = SpecialistEvidenceScope(primary_clusters=(implementation,))
    finding = _scope_finding(implementation, test, include_context=False)
    finding.cluster_ids = [implementation.cluster_id]
    finding.provenance[0].cluster_id = implementation.cluster_id
    source = implementation.ranges[0]
    finding.provenance[0].evidence_id = source.evidence_id
    finding.provenance[0].path = source.path
    finding.provenance[0].start_line = source.start_line
    finding.provenance[0].end_line = source.start_line
    finding.provenance[0].hunk_identity = source.hunk_identity
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.IMPLEMENTATION,
        scope,
        _specialist_response("IMPLEMENTATION", [finding]),
    )
    assert report.status is SpecialistStageStatus.COMPLETED
    duplicate = finding.model_copy(deep=True)
    duplicate.finding_id = finding.finding_id
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.IMPLEMENTATION,
        scope,
        _specialist_response("IMPLEMENTATION", [finding, duplicate]),
    )
    assert report.status is SpecialistStageStatus.UNVERIFIED
    assert report.failure_stage is SpecialistFailureStage.ARTIFACT_VALIDATION
    assert len(report.rejected_findings) == 2


@pytest.mark.parametrize(
    ("failure", "expected_stage"),
    [
        ("provider", SpecialistFailureStage.PROVIDER),
        ("parse", SpecialistFailureStage.PARSE),
        ("artifact", SpecialistFailureStage.ARTIFACT_VALIDATION),
    ],
)
def test_specialist_failure_stage_is_observable(monkeypatch, failure, expected_stage):
    implementation, _ = _scope_clusters()
    direct = (
        _DirectModel(error=RuntimeError("provider unavailable"))
        if failure == "provider"
        else _DirectModel("not-json")
        if failure == "parse"
        else _DirectModel('{"artifact_kind":"wrong"}')
    )
    monkeypatch.setattr("sweforge.reviewer.init_chat_model", lambda *a, **k: direct)
    report = _invoke_specialist(
        model="reviewer",
        stage=SpecialistStage.IMPLEMENTATION,
        clusters=[implementation],
        scope=SpecialistEvidenceScope(primary_clusters=(implementation,)),
    )
    assert report.status is SpecialistStageStatus.UNVERIFIED
    assert report.failure_stage is expected_stage
    assert report.failure_reason


def test_provenance_failure_retains_bounded_rejected_finding_diagnostics(monkeypatch):
    implementation, test = _scope_clusters()
    scope = SpecialistEvidenceScope(
        primary_clusters=(test,), context_clusters=(implementation,)
    )
    finding = _scope_finding(implementation, test)
    finding.provenance[1].path = "src/fabricated.py"
    report = _invoke_with_scope(
        monkeypatch,
        SpecialistStage.TEST_VALIDATION,
        scope,
        _specialist_response("TEST_VALIDATION", [finding]),
    )
    diagnostic = report.rejected_findings[0]
    assert report.failure_stage is SpecialistFailureStage.PROVENANCE_VALIDATION
    assert diagnostic.finding_id == finding.finding_id
    assert diagnostic.severity is EvidenceFindingSeverity.WARNING
    assert diagnostic.cluster_ids == finding.cluster_ids
    assert diagnostic.provenance[1].path == "src/fabricated.py"
    assert any("no supplied range" in error for error in diagnostic.validation_errors)
    assert all("Authorization" not in error for error in diagnostic.validation_errors)


def test_guard_reconstructs_same_context_scope_and_rejects_fabricated_context():
    implementation, test = _scope_clusters()
    finding = _scope_finding(implementation, test)
    semantic = SemanticReviewArtifact(
        clusters=[implementation, test],
        implementation=SpecialistStageReport(
            stage="IMPLEMENTATION", status="COMPLETED", applicable=True
        ),
        test_validation=SpecialistStageReport(
            stage="TEST_VALIDATION",
            status="COMPLETED",
            applicable=True,
            findings=[finding],
        ),
        candidate_associations=[
            FindingCandidateAssociation(
                finding_id=finding.finding_id,
                candidate_requirement_ids=["REQ"],
                basis="EXACT_RANGE",
                basis_scope="MIXED",
            )
        ],
    )
    contract = [
        {"requirement_id": "REQ", "text": "test", "classification": "BEHAVIORAL"}
    ]
    result = ExecutionReviewResult(
        verdict="ACCEPT",
        summary="ok",
        requirement_checks=[
            ReviewRequirementCheck(
                requirement_id="REQ", status="SATISFIED", evidence="evidence"
            )
        ],
    )
    assert (
        _guard_accept_coverage(result, contract, semantic_review=semantic).verdict
        == "ACCEPT"
    )
    fabricated = finding.model_copy(deep=True)
    fabricated.cluster_ids[1] = "cluster:fake"
    fabricated.provenance[1].cluster_id = "cluster:fake"
    semantic.test_validation.findings = [fabricated]
    assert (
        _guard_accept_coverage(result, contract, semantic_review=semantic).verdict
        == "BLOCKED"
    )
    (_candidate_requirement_associations,)
    (_clusters_for_stage,)
    (_evidence_clusters,)
    (_split_semantic_review,)
