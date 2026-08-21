from dataclasses import replace

import pytest

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
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
    assert not is_exact_approval("@agent approve please")
    assert not is_exact_approval("@agent yes")
    assert invocation_text("  @AGENT revise step 2") == "revise step 2"
    assert invocation_text("FYI @agent revise") is None
    assert issue_has_auto_label({"labels": [{"name": "auto"}]})
    assert not issue_has_auto_label({"labels": [{"name": "manual"}]})


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
    store.update_plan(plan.plan_id, status=PlanStatus.POSTED, posted_at="now")
    state = store.workflow_state("github:123:issue:7")
    assert state and state.phase is WorkflowPhase.PLANNING
    # Simulate the crash-safe publication transition used by publish_plan.
    store.save_workflow_state(
        replace(state, phase=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    )
    revised = engine.revise(event_key=feedback.event_key, plan_text="v2")
    assert store.plan(plan.plan_id).status is PlanStatus.SUPERSEDED
    assert revised.version == 2
    store.update_plan(revised.plan_id, status=PlanStatus.POSTED, posted_at="now")
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
    def __init__(self, labels=None):
        self.repo = RepositoryRef(123, "example/repo")
        self.labels = labels or []
        self.created = []

    def repository(self, full_name):
        return self.repo

    def issue(self, repo, number):
        return {"labels": [{"name": label} for label in self.labels]}

    def comments(self, repo, number):
        return self.created

    def create_comment(self, repo, number, body):
        item = {"id": len(self.created) + 1, "body": body}
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
    state = store.workflow_state("github:123:issue:7")
    permit = store.permit_for_plan(plan.plan_id)
    assert state.phase is WorkflowPhase.EXECUTION_READY
    assert permit is not None
    assert permit.source.value == "AUTO"
    assert "AUTO" in client.created[0]["body"]
    store.close()
