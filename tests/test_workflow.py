from dataclasses import replace

import pytest

from sweforge.github_models import (
    OriginSurface,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
    format_source_context,
)
from sweforge.github_store import (
    ExecutionStatus,
    PendingWorkflowInputError,
    PlanStatus,
    SQLiteGitHubStore,
    WorkflowMode,
    WorkflowPhase,
)
from sweforge.workflow import (
    WorkflowEngine,
    invocation_text,
    is_exact_approval,
    issue_has_auto_label,
)


def make_event(repo: RepositoryRef, *, source_id: str, body: str) -> SourceEvent:
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=source_id,
        source_updated_at=f"2026-01-01T00:0{source_id}Z",
        source_created_at=f"2026-01-01T00:0{source_id}Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body=body,
        html_url=None,
    )


def seed(store, events):
    repo = RepositoryRef(123, "example/repo")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        events,
        since="now",
        etag=None,
        polled_at="now",
    )
    return repo


def test_exact_approval_is_deterministic():
    assert is_exact_approval("@agent approve")
    assert is_exact_approval("  @AGENT APPROVE  ")
    assert is_exact_approval("@agent     approve")
    assert not is_exact_approval("@agent approve please")
    assert not is_exact_approval("@agent yes")
    assert invocation_text("  @AGENT revise step 2") == "revise step 2"
    assert invocation_text("FYI @agent revise") is None
    assert issue_has_auto_label({"labels": [{"name": "auto"}]})
    assert not issue_has_auto_label({"labels": [{"name": "manual"}]})


def test_inline_source_context_contains_bounded_review_provenance():
    context = format_source_context(
        {
            "origin_surface": "PR_INLINE_REVIEW",
            "author_login": "alice",
            "subject_number": 42,
            "path": "src/Foo.java",
            "line": 15,
            "start_line": 10,
            "side": "RIGHT",
            "diff_hunk": "@@ -10,6 +10,11 @@",
            "commit_id": "newsha",
            "original_commit_id": "oldsha",
        },
        "fix this race",
    )
    assert "GitHub PR #42 inline review" in context
    assert "src/Foo.java" in context
    assert "10-15" in context
    assert "@@ -10,6 +10,11 @@" in context
    assert "newsha" in context and "oldsha" in context
    assert "outdated" in context


def test_submitted_review_source_context_separates_state_from_user_request():
    context = format_source_context(
        {
            "origin_surface": "PR_REVIEW",
            "author_login": "alice",
            "subject_number": 42,
            "review_state": "CHANGES_REQUESTED",
            "commit_id": "reviewsha",
        },
        "@agent preserve compatibility",
    )
    assert "GitHub PR #42 submitted review by alice" in context
    assert "Review state: CHANGES_REQUESTED" in context
    assert "Review anchor commit: reviewsha" in context
    assert context.endswith("@agent preserve compatibility")


def test_normal_external_task_remains_bounded():
    context = format_source_context(
        {"origin_surface": "ISSUE", "author_login": "alice", "subject_number": 7},
        "x" * 5_000,
    )
    assert len(context) == len("[GitHub issue #7 comment by alice]\n") + 4_000
    assert context.endswith("x" * 4_000)


def test_plan_feedback_approval_and_permit_bind_current_plan(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    feedback = make_event(repo, source_id="2", body="@agent preserve compatibility")
    approval = make_event(repo, source_id="3", body="@agent approve")
    seed(store, [root, feedback, approval])
    engine = WorkflowEngine(store=store, clock=lambda: "now")

    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    assert plan.status is PlanStatus.DRAFT
    store.update_plan(
        plan.plan_id, status=PlanStatus.POSTED, posted_at="2026-01-01T00:02Z"
    )
    state = store.workflow_state("github:123:issue:7")
    assert state and state.phase is WorkflowPhase.PLANNING
    # Simulate the crash-safe publication transition used by publish_plan.
    store.save_workflow_state(
        replace(state, phase=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    )
    revised = engine.revise(event_key=feedback.event_key, plan_text="v2")
    assert store.plan(plan.plan_id).status is PlanStatus.SUPERSEDED
    assert revised.version == 2
    store.update_plan(
        revised.plan_id, status=PlanStatus.POSTED, posted_at="2026-01-01T00:02Z"
    )
    state = store.workflow_state(state.thread_id)
    store.save_workflow_state(
        replace(state, phase=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    )
    permit = engine.approve(event_key=approval.event_key)
    assert permit.plan_id == revised.plan_id
    assert permit.plan_version == 2
    assert store.workflow_state(state.thread_id).phase is WorkflowPhase.EXECUTION_READY
    with pytest.raises(ValueError, match="unavailable"):
        engine.validate_permit("permit-does-not-exist")
    store.close()


class WorkflowGitHub:
    def __init__(self, labels=None, permissions=None, default_permission="write"):
        self.repo = RepositoryRef(123, "example/repo")
        self.labels = labels or []
        self.created = []
        self.review_numbers = []
        # Approval requires repository write access; these tests are about
        # other properties, so their commenters are writers by default.
        self.permissions = permissions or {}
        self.default_permission = default_permission

    def repository(self, full_name):
        return self.repo

    def collaborator_permission(self, repo, login):
        return self.permissions.get(login, self.default_permission)

    def issue(self, repo, number):
        return {"labels": [{"name": label} for label in self.labels]}

    def comments(self, repo, number):
        return self.created

    def create_comment(self, repo, number, body):
        item = {
            "id": len(self.created) + 1,
            "body": body,
            "created_at": "2026-01-01T00:10:00Z",
        }
        self.created.append(item)
        return item

    def review_comments_for_pull_request(self, repo, number):
        self.review_numbers.append(number)
        return self.created

    def create_review_comment_reply(self, repo, pull_number, comment_id, body):
        item = {
            "id": len(self.created) + 1,
            "body": body,
            "reply_to": comment_id,
            "pull_number": pull_number,
            "created_at": "2026-01-01T00:10:00Z",
        }
        self.created.append(item)
        return item


def test_auto_posts_plan_and_creates_application_permit(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [root])
    client = WorkflowGitHub(labels=["AUTO"])
    engine = WorkflowEngine(store=store, client=client, clock=lambda: "now")
    plan = engine.start_cycle(
        event_key=root.event_key, plan_text="v1", mode=WorkflowMode.AUTO
    )
    engine.publish_plan(plan.plan_id)
    engine.authorize_auto(thread_id="github:123:issue:7")
    state = store.workflow_state("github:123:issue:7")
    permit = store.permit_for_plan(plan.plan_id)
    assert state.phase is WorkflowPhase.EXECUTION_READY
    assert permit is not None
    assert permit.source.value == "AUTO"
    assert "AUTO" in client.created[0]["body"]
    store.close()


def test_pr_conversation_plan_and_summary_use_pr_surface(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent add a test")
    root = replace(
        root,
        subject_number=42,
        origin_surface=OriginSurface.PR_CONVERSATION,
        html_url="https://github.com/example/repo/pull/42#issuecomment-1",
    )
    seed(store, [root])
    client = WorkflowGitHub()
    engine = WorkflowEngine(store=store, client=client, clock=lambda: "now")
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    assert client.created[0]["body"].startswith("<!-- sweforge:plan:")
    assert client.created[0]["id"] == 1
    with pytest.raises(ValueError, match="review ACCEPT"):
        engine.complete_publication(
            thread_id="github:123:issue:42",
            publication_status="NO_CHANGES",
        )
    store.close()


def test_cycle_two_reuses_thread_and_workspace_metadata(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    later = make_event(repo, source_id="4", body="@agent also add metrics")
    seed(store, [root, later])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:10:00Z")
    first = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    state = store.workflow_state(first.thread_id)
    store.save_workflow_state(replace(state, phase=WorkflowPhase.IDLE))
    second = engine.start_cycle(event_key=later.event_key, plan_text="v1")
    assert second.cycle_id == 2
    assert second.version == 1
    assert second.thread_id == first.thread_id
    assert store.workflow_state(first.thread_id).cycle_id == 2
    store.close()


def test_early_approval_is_skipped_until_fresh_approval(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    early = make_event(repo, source_id="2", body="@agent approve")
    fresh = replace(
        make_event(repo, source_id="4", body="@agent approve"),
        source_updated_at="2026-01-01T00:20Z",
        source_created_at="2026-01-01T00:20Z",
    )
    seed(store, [root, early, fresh])
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:11:00Z"
    )
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    with pytest.raises(ValueError, match="predates"):
        engine.approve(event_key=early.event_key)
    assert (
        store.execution_for_event(early.event_key)["status"] == ExecutionStatus.SKIPPED
    )
    permit = engine.approve(event_key=fresh.event_key)
    assert permit.plan_id == plan.plan_id
    store.close()


def test_mark_posted_repairs_plan_posted_workflow_planning(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [root])
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:01:00Z")
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    store.update_plan(
        plan.plan_id,
        status=PlanStatus.POSTED,
        posted_at="2026-01-01T00:02:00Z",
        posted_comment_id=7,
    )
    repaired = store.mark_current_plan_posted(
        plan.plan_id, comment_id=7, posted_at="2026-01-01T00:02:00Z"
    )
    assert repaired.status is PlanStatus.POSTED
    assert (
        store.workflow_state(plan.thread_id).phase
        is WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
    )
    store.close()


def test_real_pr_mapping_preserves_issue_identity(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    issue = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [issue])
    store.register_pr_mapping(repo.repo_id, 42, "github:123:issue:7")
    pr = replace(
        issue,
        source_id="2",
        source_updated_at="2026-01-01T00:02Z",
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=42,
        origin_surface=OriginSurface.PR_CONVERSATION,
        body="@agent please review the PR",
    )
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [pr],
        since="now",
        etag=None,
        polled_at="now",
    )
    persisted = store.source_event(pr.event_key)
    assert persisted["thread_id"] == "github:123:issue:7"
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:03:00Z")
    plan = engine.start_cycle(event_key=pr.event_key, plan_text="v1")
    state = store.workflow_state(plan.thread_id)
    assert state.issue_number == 7
    assert state.response_subject_number == 42
    store.close()


def test_inline_plan_idempotency_uses_pr_number(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    issue = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [issue])
    store.register_pr_mapping(repo.repo_id, 42, "github:123:issue:7")
    review = replace(
        issue,
        source_id="2",
        source_updated_at="2026-01-01T00:02Z",
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=42,
        origin_surface=OriginSurface.PR_INLINE_REVIEW,
        review_thread_root_id="2",
        body="@agent fix this line",
    )
    store.record_batch(
        repo.repo_id,
        "review_comments",
        [review],
        since="now",
        etag=None,
        polled_at="now",
    )
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:03Z"
    )
    plan = engine.start_cycle(event_key=review.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    assert client.review_numbers == [42]
    store.close()


def test_bind_rejects_feedback_arriving_after_approval(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    approval = replace(
        make_event(repo, source_id="2", body="@agent approve"),
        source_updated_at="2026-01-01T00:20Z",
        source_created_at="2026-01-01T00:20Z",
    )
    feedback = replace(
        make_event(repo, source_id="3", body="@agent preserve compatibility"),
        source_updated_at="2026-01-01T00:21Z",
        source_created_at="2026-01-01T00:21Z",
    )
    seed(store, [root, approval, feedback])
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:22Z"
    )
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    permit = engine.approve(event_key=approval.event_key)
    with pytest.raises(PendingWorkflowInputError):
        store.bind_authorized_execution(
            permit.permit_id,
            expected_thread_id=permit.thread_id,
            now="2026-01-01T00:23Z",
        )
    assert store.execution_for_event(root.event_key) is None
    assert store.workflow_state(permit.thread_id).phase is WorkflowPhase.EXECUTION_READY
    store.close()


def test_existing_permit_root_is_backfilled_on_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteGitHubStore(path)
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    approval = replace(
        make_event(repo, source_id="2", body="@agent approve"),
        source_updated_at="2026-01-01T00:20Z",
        source_created_at="2026-01-01T00:20Z",
    )
    seed(store, [root, approval])
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:21Z"
    )
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    permit = engine.approve(event_key=approval.event_key)
    store.connection.execute(
        "ALTER TABLE execution_permits RENAME TO execution_permits_new"
    )
    store.connection.execute(
        """CREATE TABLE execution_permits(
           permit_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
           cycle_id INTEGER NOT NULL, plan_id TEXT NOT NULL,
           plan_version INTEGER NOT NULL, source TEXT NOT NULL,
           source_event_key TEXT, created_at TEXT NOT NULL,
           consumed_at TEXT, invalidated_at TEXT)"""
    )
    store.connection.execute(
        """INSERT INTO execution_permits
           SELECT permit_id, thread_id, cycle_id, plan_id, plan_version, source,
                  source_event_key, created_at, consumed_at, invalidated_at
           FROM execution_permits_new"""
    )
    store.connection.execute("DROP TABLE execution_permits_new")
    store.connection.commit()
    store.close()
    reopened = SQLiteGitHubStore(path)
    assert reopened.permit(permit.permit_id).root_event_key == root.event_key
    reopened.close()


@pytest.mark.parametrize(
    ("created", "updated"),
    [
        (None, "2026-01-01T00:20Z"),
        ("2026-01-01T00:01Z", "2026-01-01T00:20Z"),
        ("2026-01-01T00:10Z", "2026-01-01T00:10Z"),
        ("not-a-timestamp", "2026-01-01T00:20Z"),
    ],
)
def test_approval_timestamp_fail_closed(tmp_path, created, updated):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    approval = replace(
        make_event(repo, source_id="2", body="@agent approve"),
        source_created_at=created,
        source_updated_at=updated,
    )
    seed(store, [root, approval])
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:11Z"
    )
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    with pytest.raises(ValueError, match="predates"):
        engine.approve(event_key=approval.event_key)
    assert store.permit_for_plan(plan.plan_id) is None
    assert (
        store.execution_for_event(approval.event_key)["status"]
        == ExecutionStatus.SKIPPED
    )
    store.close()


def test_auto_authorization_recovers_on_advance(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    root = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [root])
    client = WorkflowGitHub(labels=["AUTO"])
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:11Z"
    )
    plan = engine.start_cycle(
        event_key=root.event_key, plan_text="v1", mode=WorkflowMode.AUTO
    )
    engine.publish_plan(plan.plan_id)
    result = engine.advance(
        thread_id=plan.thread_id,
        model="unused",
        repo_paths={},
        workspace_root=tmp_path,
    )
    assert result.phase is WorkflowPhase.EXECUTION_READY
    assert result.permit_id is not None
    assert len(client.created) == 1
    store.close()


def test_wrong_surface_approval_is_skipped_and_inline_target_can_approve(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    issue = make_event(repo, source_id="1", body="@agent implement X")
    seed(store, [issue])
    store.register_pr_mapping(repo.repo_id, 42, "github:123:issue:7")
    root = replace(
        issue,
        source_id="2",
        source_updated_at="2026-01-01T00:02Z",
        source_created_at="2026-01-01T00:02Z",
        subject_kind=SubjectKind.PULL_REQUEST,
        subject_number=42,
        origin_surface=OriginSurface.PR_INLINE_REVIEW,
        review_thread_root_id="500",
        body="@agent fix this line",
    )
    wrong = replace(
        root,
        source_id="3",
        source_updated_at="2026-01-01T00:05Z",
        source_created_at="2026-01-01T00:05Z",
        origin_surface=OriginSurface.PR_CONVERSATION,
        review_thread_root_id=None,
        body="@AGENT    APPROVE",
    )
    correct = replace(
        root,
        source_id="4",
        source_updated_at="2026-01-01T00:20Z",
        source_created_at="2026-01-01T00:20Z",
        body="@agent approve",
    )
    store.record_batch(
        repo.repo_id,
        "review_comments",
        [root, wrong, correct],
        since="now",
        etag=None,
        polled_at="now",
    )
    client = WorkflowGitHub()
    engine = WorkflowEngine(
        store=store, client=client, clock=lambda: "2026-01-01T00:21Z"
    )
    plan = engine.start_cycle(event_key=root.event_key, plan_text="v1")
    engine.publish_plan(plan.plan_id)
    result = engine.advance(
        thread_id=plan.thread_id,
        model="unused",
        repo_paths={},
        workspace_root=tmp_path,
    )
    assert result.phase is WorkflowPhase.EXECUTION_READY
    assert (
        store.input_consumption(wrong.event_key).purpose.value == "STALE_PLAN_APPROVAL"
    )
    store.close()
