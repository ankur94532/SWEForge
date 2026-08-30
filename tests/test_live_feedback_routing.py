from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from sweforge.agent_trace import AgentTracer
from sweforge.github_models import (
    InteractionMode,
    OriginSurface,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_comment_delivery import deliver_pending_workflow_comments
from sweforge.workflow_controller import DeclarativeWorkflowController
from sweforge.workflow_driver import DeepAgentWorkflowDriver
from sweforge.workflow_messages import (
    PLAN_DEFERRED_FEEDBACK_MESSAGE,
    PLAN_FOOTER,
    RESULT_DEFERRED_FEEDBACK_MESSAGE,
    RESULT_FOOTER,
    UNSOLICITED_ACK_MESSAGE,
)
from sweforge.workflow_middleware import (
    DelegatedWorkflowPolicyMiddleware,
    WorkflowAuthority,
    WorkflowPolicyMiddleware,
)
from sweforge.workflow_runtime import TaskPhase, ValidationVerdict, WorkflowRuntime
from sweforge.workflow_spec import parse_workflow_spec
from sweforge.workflow_tools import build_lifecycle_tools

NOW = "2026-01-01T00:20:00Z"


def workflow_spec():
    return parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "live-feedback-test",
            "tasks": [
                {
                    "id": "A",
                    "depends_on": [],
                    "planning": {
                        "skill": "plan-A",
                        "tools": ["read_file", "grep"],
                    },
                    "execution": {"skill": "execute-A", "tools": ["edit_file"]},
                    "validation": {
                        "skill": "validate-A",
                        "tools": ["read_file", "run_validation"],
                    },
                }
            ],
        }
    )


def source(
    repo: RepositoryRef,
    source_id: str,
    *,
    body: str,
    created_at: str,
    surface: OriginSurface = OriginSurface.ISSUE,
    subject_number: int = 7,
    review_root: str | None = None,
) -> SourceEvent:
    is_issue = surface == OriginSurface.ISSUE
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=(
            SourceKind.ISSUE
            if source_id == "root"
            else (
                SourceKind.REVIEW_COMMENT
                if surface == OriginSurface.PR_INLINE_REVIEW
                else SourceKind.PULL_REQUEST_REVIEW
                if surface == OriginSurface.PR_REVIEW
                else SourceKind.ISSUE_COMMENT
            )
        ),
        source_id=source_id,
        source_updated_at=created_at,
        source_created_at=created_at,
        subject_kind=SubjectKind.ISSUE if is_issue else SubjectKind.PULL_REQUEST,
        subject_number=subject_number,
        author_login="owner",
        body=body,
        html_url=None,
        origin_surface=surface,
        review_thread_root_id=review_root,
    )


def record(store: SQLiteGitHubStore, event: SourceEvent) -> None:
    store.upsert_repository(
        event.repo_id, event.repo_full_name, event.source_updated_at
    )
    store.record_batch(
        event.repo_id,
        event.source_kind.value,
        [event],
        since=event.source_updated_at,
        etag=None,
        polled_at=event.source_updated_at,
    )


def setup_runtime(tmp_path, *, mode: InteractionMode = InteractionMode.MANUAL):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(77, "example/repo")
    root = source(
        repo,
        "root",
        body="@agent implement parser compatibility",
        created_at="2026-01-01T00:00:00Z",
    )
    if mode == InteractionMode.AUTO:
        root = replace(root, issue_labels=("AUTO",))
    record(store, root)
    thread_id = f"github:{repo.repo_id}:issue:7"
    runtime = WorkflowRuntime(store, clock=lambda: NOW)
    cycle = runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=root.event_key,
        spec=workflow_spec(),
    )
    task = runtime.select_active_task(cycle.workflow_cycle_id)
    return store, repo, runtime, cycle, task


def submit_plan(runtime: WorkflowRuntime, task):
    return runtime.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="preserve compatibility and add tests",
        posted_comment_id=101,
        posted_at="2026-01-01T00:10:00Z",
    )


def approve_plan(runtime: WorkflowRuntime, task, plan, *, event_key="approval"):
    return runtime.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key=event_key,
        approved_by="owner",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:11:00Z",
    )


def publish_result(runtime: WorkflowRuntime, task):
    runtime.finish_execution(
        task.task_run_id,
        summary="implemented",
        evidence={"reported": {"tests": "passed"}, "tool_observations": []},
    )
    runtime.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.ACCEPT,
        summary="validated",
        findings=[],
        repair_instructions=[],
        evidence={"validation_runs": [{"pytest": "passed"}], "reported": {}},
    )
    return runtime.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=102,
        posted_at="2026-01-01T00:12:00Z",
    )


class SurfaceClient:
    def __init__(self):
        self.conversation: dict[int, list[dict]] = {}
        self.inline: dict[int, list[dict]] = {}
        self.calls: list[tuple] = []
        self.next_id = 1000

    def repository(self, full_name):
        return RepositoryRef(77, full_name)

    def comments(self, _repo, number):
        return list(self.conversation.get(number, []))

    def review_comments(self, _repo, number):
        return list(self.inline.get(number, []))

    def create_comment(self, _repo, number, body):
        self.next_id += 1
        item = {"id": self.next_id, "body": body, "created_at": NOW}
        self.conversation.setdefault(number, []).append(item)
        self.calls.append(("conversation", number, body))
        return item

    def create_review_comment_reply(self, _repo, number, reply_to, body):
        self.next_id += 1
        item = {"id": self.next_id, "body": body, "created_at": NOW}
        self.inline.setdefault(number, []).append(item)
        self.calls.append(("inline", number, reply_to, body))
        return item


class CaptureSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


@pytest.mark.parametrize(
    "phase",
    [TaskPhase.PLANNING, TaskPhase.EXECUTING, TaskPhase.VALIDATING],
)
def test_unsolicited_input_is_queued_and_acknowledged_without_phase_change(
    tmp_path, phase
):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    if phase != TaskPhase.PLANNING:
        plan = submit_plan(runtime, task)
        approve_plan(runtime, task, plan)
    if phase == TaskPhase.VALIDATING:
        runtime.finish_execution(
            task.task_run_id,
            summary="implemented",
            evidence={"reported": {"tests": "passed"}, "tool_observations": []},
        )
    before = runtime.task(task.task_run_id)
    steering = source(
        repo,
        f"steering-{phase.value}",
        body="@agent Also preserve old config behavior.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, steering)

    queued = store.pending_revision_inputs(cycle.thread_id)
    assert [item["source_event_key"] for item in queued] == [steering.event_key]
    assert runtime.task(task.task_run_id).phase == before.phase
    pending = store.pending_workflow_comments(cycle.thread_id)
    assert len(pending) == 1
    assert UNSOLICITED_ACK_MESSAGE in pending[0].body
    assert "sweforge:revision-input-ack:" in pending[0].stable_marker

    client = SurfaceClient()
    sink = CaptureSink()
    delivered = deliver_pending_workflow_comments(
        store=store,
        client=client,
        thread_id=cycle.thread_id,
        now=NOW,
        tracer=AgentTracer(sink),
    )
    assert delivered == 1
    assert len(client.calls) == 1
    assert runtime.task(task.task_run_id).phase == before.phase
    assert [item.category for item in sink.events] == [
        "REVISION INPUT ACK REQUIRED",
        "REVISION INPUT ACK POSTED",
    ]
    store.close()


def test_ack_reconciliation_survives_repoll_restart_and_post_before_commit(tmp_path):
    store, repo, _runtime, cycle, _task = setup_runtime(tmp_path)
    steering = source(
        repo,
        "steering-once",
        body="@agent Handle empty arrays too.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, steering)
    record(store, steering)
    client = SurfaceClient()
    assert (
        deliver_pending_workflow_comments(
            store=store, client=client, thread_id=cycle.thread_id, now=NOW
        )
        == 1
    )
    outbox = store.connection.execute(
        "SELECT outbox_id FROM workflow_comment_outbox_v1"
    ).fetchone()
    store.update_workflow_comment(outbox["outbox_id"], status="PENDING", now=NOW)
    store.close()

    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    assert (
        deliver_pending_workflow_comments(
            store=reopened, client=client, thread_id=cycle.thread_id, now=NOW
        )
        == 1
    )
    assert len(client.calls) == 1
    assert len(reopened.pending_revision_inputs(cycle.thread_id)) == 1
    reopened.close()


@pytest.mark.parametrize(
    ("surface", "subject", "review_root", "expected_call"),
    [
        (OriginSurface.PR_CONVERSATION, 9, None, "conversation"),
        (OriginSurface.PR_INLINE_REVIEW, 9, "700", "inline"),
        (OriginSurface.PR_REVIEW, 9, None, "conversation"),
    ],
)
def test_wrong_surface_input_is_acknowledged_on_its_own_pr_surface(
    tmp_path, surface, subject, review_root, expected_call
):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    store.register_pr_mapping(repo.repo_id, subject, cycle.thread_id)
    steering = source(
        repo,
        f"pr-{surface.value}",
        body="@agent Also add tests.",
        created_at="2026-01-01T00:15:00Z",
        surface=surface,
        subject_number=subject,
        review_root=review_root,
    )
    record(store, steering)
    client = SurfaceClient()
    deliver_pending_workflow_comments(
        store=store, client=client, thread_id=cycle.thread_id, now=NOW
    )

    assert client.calls[0][0] == expected_call
    if expected_call == "inline":
        assert client.calls[0][2] == 700
    retained = runtime.plan(plan.plan_id)
    assert retained.plan_id == plan.plan_id
    assert retained.approval_occurrence_key == plan.approval_occurrence_key
    assert runtime.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL
    store.close()


def test_plan_feedback_defer_retains_exact_plan_and_queues_original_once(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "plan-feedback",
        body="@agent Also migrate authentication to OAuth.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    assert store.pending_revision_inputs(cycle.thread_id) == []
    assert store.pending_workflow_comments(cycle.thread_id) == []
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="PLAN",
        occurrence_key=plan.approval_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )

    store.defer_feedback_to_revision(review.feedback_review_id, now=NOW)
    store.defer_feedback_to_revision(review.feedback_review_id, now=NOW)
    retained = runtime.plan(plan.plan_id)
    waiting = runtime.task(task.task_run_id)
    assert waiting.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL
    assert waiting.current_plan_id == plan.plan_id
    assert retained.plan_digest == plan.plan_digest
    assert retained.approval_occurrence_key == plan.approval_occurrence_key
    queued = store.pending_revision_inputs(cycle.thread_id)
    assert len(queued) == 1
    assert queued[0]["source_event_key"] == feedback.event_key
    pushback = store.pending_workflow_comments(cycle.thread_id)
    assert len(pushback) == 1
    assert PLAN_DEFERRED_FEEDBACK_MESSAGE in pushback[0].body

    approve_plan(runtime, task, retained, event_key="retained-plan-approval")
    runtime.close_deferred_feedback(review.feedback_review_id)
    assert runtime.task(task.task_run_id).phase == TaskPhase.EXECUTING
    store.close()


def test_result_feedback_defer_retains_exact_result_and_validation(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    approve_plan(runtime, task, plan)
    result = publish_result(runtime, task)
    feedback = source(
        repo,
        "result-feedback",
        body="@agent Also redesign authentication.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="RESULT",
        occurrence_key=result.result_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )
    store.defer_feedback_to_revision(review.feedback_review_id, now=NOW)

    retained = runtime.current_result(task.task_run_id)
    assert retained == result
    assert retained.validation_id == result.validation_id
    assert retained.result_occurrence_key == result.result_occurrence_key
    assert runtime.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL
    assert (
        RESULT_DEFERRED_FEEDBACK_MESSAGE
        in store.pending_workflow_comments(cycle.thread_id)[0].body
    )
    runtime.approve_result(
        task_run_id=task.task_run_id,
        occurrence_key=retained.result_occurrence_key,
        approval_event_key="retained-result-approval",
        approved_by="owner",
        approval_is_authorized=True,
        approval_occurred_at=NOW,
    )
    runtime.close_deferred_feedback(review.feedback_review_id)
    assert runtime.task(task.task_run_id).phase == TaskPhase.DONE
    store.close()


@pytest.mark.parametrize("kind", ["PLAN", "RESULT"])
def test_relevant_feedback_uses_authoritative_cumulative_replan(tmp_path, kind):
    store, repo, runtime, _cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    if kind == "RESULT":
        approve_plan(runtime, task, plan)
        result = publish_result(runtime, task)
        occurrence = result.result_occurrence_key
    else:
        occurrence = plan.approval_occurrence_key
    feedback = source(
        repo,
        f"relevant-{kind}",
        body="@agent Keep the JSON response backward compatible.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind=kind,
        occurrence_key=occurrence,
        feedback_text=feedback.body,
        now=NOW,
    )
    replanning = runtime.resolve_current_feedback_replan(review.feedback_review_id)

    assert replanning.phase == TaskPhase.PLANNING
    assert runtime.plan(plan.plan_id).status == "SUPERSEDED"
    assert store.feedback_review(review.feedback_review_id).status == "REPLAN"
    if kind == "RESULT":
        assert feedback.body in str(replanning.repair_feedback)
    store.close()


def test_feedback_gateways_are_zero_argument_root_only_and_phase_scoped(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    tools = {
        item.name: item
        for item in build_lifecycle_tools(
            runtime=runtime,
            workflow_cycle_id=cycle.workflow_cycle_id,
            publish_plan=lambda **_kwargs: (1, NOW),
            publish_result=lambda **_kwargs: (2, NOW),
        )
    }
    for name in ("replan_current_feedback", "defer_current_feedback_to_revision"):
        assert tools[name].args_schema.model_json_schema()["properties"] == {}
    authority = WorkflowAuthority(runtime, cycle.workflow_cycle_id, workflow_spec())
    assert (
        "defer_current_feedback_to_revision"
        not in WorkflowPolicyMiddleware.allowed_tools(authority.snapshot())
    )
    with pytest.raises(PermissionError, match="no exact active feedback review"):
        tools["defer_current_feedback_to_revision"].invoke({})

    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "security-feedback",
        body="@agent Separate request.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="PLAN",
        occurrence_key=plan.approval_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )
    allowed = WorkflowPolicyMiddleware.allowed_tools(authority.snapshot())
    assert {"replan_current_feedback", "defer_current_feedback_to_revision"} <= allowed
    delegated = DelegatedWorkflowPolicyMiddleware(authority)
    delegated_request = SimpleNamespace(
        tool_call={"name": "defer_current_feedback_to_revision", "args": {}}
    )
    with pytest.raises(PermissionError, match="delegated tool"):
        delegated.wrap_tool_call(delegated_request, lambda request: request)
    with pytest.raises(Exception):
        tools["defer_current_feedback_to_revision"].invoke(
            {"event_key": "chosen-by-model"}
        )
    deferred = authority.snapshot()
    assert deferred.feedback_review_status == "DEFERRED_WAITING"
    assert WorkflowPolicyMiddleware.allowed_tools(deferred) == {
        "defer_current_feedback_to_revision"
    }
    store.close()


@pytest.mark.parametrize("kind", ["PLAN", "RESULT"])
def test_feedback_review_restart_reconstructs_exact_resume_payload(tmp_path, kind):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    if kind == "RESULT":
        approve_plan(runtime, task, plan)
        result = publish_result(runtime, task)
        occurrence = result.result_occurrence_key
    else:
        occurrence = plan.approval_occurrence_key
    feedback = source(
        repo,
        f"restart-{kind}",
        body="@agent Preserve the current wire format.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind=kind,
        occurrence_key=occurrence,
        feedback_text=feedback.body,
        now=NOW,
    )

    class PendingDriver:
        def has_pending_interrupt(self, **kwargs):
            return (
                kwargs["kind"] == f"{kind}_APPROVAL"
                and kwargs["occurrence_key"] == occurrence
            )

    controller = object.__new__(DeclarativeWorkflowController)
    controller.store = store
    controller.runtime = runtime
    controller.client = None
    controller.clock = lambda: NOW
    controller.tracer = None
    method = (
        controller._plan_wait_resume
        if kind == "PLAN"
        else controller._result_wait_resume
    )
    payload = method(cycle, runtime.task(task.task_run_id), PendingDriver())

    assert payload == {
        "kind": f"{kind}_FEEDBACK",
        "occurrence_key": occurrence,
        "event_key": feedback.event_key,
        "feedback_review_id": review.feedback_review_id,
        "feedback": feedback.body,
    }
    store.close()


def make_driver(store, runtime, cycle, client, tmp_path):
    return DeepAgentWorkflowDriver(
        runtime=runtime,
        workflow_cycle_id=cycle.workflow_cycle_id,
        spec=workflow_spec(),
        store=store,
        client=client,
        worktree=tmp_path,
        planning_model="unused",
        execution_model="unused",
        validation_model="unused",
        checkpointer=None,
        memory_store=None,
        capability_registry=None,
        sandbox_backend_provider=None,
        secure_execution=False,
        unsafe_local_shell=True,
    )


@pytest.mark.parametrize("mode", [InteractionMode.MANUAL, InteractionMode.AUTO])
def test_plan_and_result_footers_are_application_owned_and_mode_appropriate(
    tmp_path, mode
):
    store, _repo, runtime, cycle, task = setup_runtime(tmp_path, mode=mode)
    client = SurfaceClient()
    driver = make_driver(store, runtime, cycle, client, tmp_path)
    driver._publish_plan(
        cycle,
        task_run_id=task.task_run_id,
        task_id=task.task_id,
        plan_text="model-authored plan body",
    )
    plan_body = client.calls[-1][-1]
    if mode == InteractionMode.MANUAL:
        assert PLAN_FOOTER in plan_body
        assert "feedback specifically related to this plan" in plan_body
        assert "follow-up revision loop" in plan_body
    else:
        assert PLAN_FOOTER not in plan_body
        assert "@agent approve" not in plan_body

    plan = submit_plan(runtime, task)
    if mode == InteractionMode.AUTO:
        runtime.auto_authorize_plan(task.task_run_id)
    else:
        approve_plan(runtime, task, plan)
    publish_result(runtime, task)
    driver._publish_result(cycle, task_run_id=task.task_run_id, task_id=task.task_id)
    result_body = client.calls[-1][-1]
    if mode == InteractionMode.MANUAL:
        assert RESULT_FOOTER in result_body
        assert "feedback specifically related to this implementation" in result_body
        assert "follow-up revision loop" in result_body
    else:
        assert RESULT_FOOTER not in result_body
        assert "@agent approve" not in result_body
    store.close()


def test_pushback_reconciliation_is_exactly_once_after_post_before_commit(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "defer-crash",
        body="@agent Separate OAuth migration.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="PLAN",
        occurrence_key=plan.approval_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )
    store.defer_feedback_to_revision(review.feedback_review_id, now=NOW)
    client = SurfaceClient()
    assert (
        deliver_pending_workflow_comments(
            store=store, client=client, thread_id=cycle.thread_id, now=NOW
        )
        == 1
    )
    outbox = store.connection.execute(
        "SELECT outbox_id FROM workflow_comment_outbox_v1"
    ).fetchone()
    store.update_workflow_comment(outbox["outbox_id"], status="PENDING", now=NOW)
    assert (
        deliver_pending_workflow_comments(
            store=store, client=client, thread_id=cycle.thread_id, now=NOW
        )
        == 1
    )
    assert len(client.calls) == 1
    assert runtime.task(task.task_run_id).current_plan_id == plan.plan_id
    store.close()


def test_ambiguous_marker_delivery_fails_closed_without_losing_revision(tmp_path):
    store, repo, _runtime, cycle, _task = setup_runtime(tmp_path)
    feedback = source(
        repo,
        "ambiguous-ack",
        body="@agent Add regression coverage.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    pending = store.pending_workflow_comments(cycle.thread_id)[0]
    client = SurfaceClient()
    client.conversation[7] = [
        {"id": 1, "body": pending.body},
        {"id": 2, "body": pending.body},
    ]
    assert (
        deliver_pending_workflow_comments(
            store=store, client=client, thread_id=cycle.thread_id, now=NOW
        )
        == 0
    )
    status = store.connection.execute(
        "SELECT status FROM workflow_comment_outbox_v1"
    ).fetchone()["status"]
    assert status == "AMBIGUOUS"
    assert len(store.pending_revision_inputs(cycle.thread_id)) == 1
    store.close()


def test_transient_ack_failure_is_durably_retried_without_rolling_back_input(
    tmp_path,
):
    store, repo, _runtime, cycle, _task = setup_runtime(tmp_path)
    feedback = source(
        repo,
        "retry-ack",
        body="@agent Preserve older config files.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)

    class FailingClient(SurfaceClient):
        def create_comment(self, _repo, _number, _body):
            raise OSError("temporary GitHub outage")

    assert (
        deliver_pending_workflow_comments(
            store=store,
            client=FailingClient(),
            thread_id=cycle.thread_id,
            now=NOW,
        )
        == 0
    )
    assert len(store.pending_revision_inputs(cycle.thread_id)) == 1
    assert store.pending_workflow_comments(cycle.thread_id, due_at=NOW) == []
    due = "2026-01-01T00:21:00Z"
    assert len(store.pending_workflow_comments(cycle.thread_id, due_at=due)) == 1
    client = SurfaceClient()
    assert (
        deliver_pending_workflow_comments(
            store=store, client=client, thread_id=cycle.thread_id, now=due
        )
        == 1
    )
    assert len(client.calls) == 1
    store.close()


def test_feedback_outcome_and_pushback_tracing_is_metadata_only(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "trace-defer",
        body="@agent Secret unrelated request text.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="PLAN",
        occurrence_key=plan.approval_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )
    sink = CaptureSink()
    tracer = AgentTracer(sink)
    client = SurfaceClient()
    tools = {
        item.name: item
        for item in build_lifecycle_tools(
            runtime=runtime,
            workflow_cycle_id=cycle.workflow_cycle_id,
            publish_plan=lambda **_kwargs: (1, NOW),
            publish_result=lambda **_kwargs: (2, NOW),
            deliver_comments=lambda: deliver_pending_workflow_comments(
                store=store,
                client=client,
                thread_id=cycle.thread_id,
                now=NOW,
                tracer=tracer,
            ),
            tracer=tracer,
        )
    }
    with pytest.raises(Exception):
        tools["defer_current_feedback_to_revision"].invoke({})

    categories = [item.category for item in sink.events]
    assert "FEEDBACK DEFERRED" in categories
    assert "FEEDBACK PUSHBACK REQUIRED" in categories
    assert "FEEDBACK PUSHBACK POSTED" in categories
    rendered = "\n".join(item.message for item in sink.events)
    assert "Secret unrelated request text" not in rendered
    store.close()


def test_relevant_feedback_gateway_emits_bounded_replan_trace(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "trace-replan",
        body="@agent Keep the current JSON response compatible.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)
    review = store.begin_feedback_review(
        event_key=feedback.event_key,
        task_run_id=task.task_run_id,
        feedback_kind="PLAN",
        occurrence_key=plan.approval_occurrence_key,
        feedback_text=feedback.body,
        now=NOW,
    )
    sink = CaptureSink()
    tools = {
        item.name: item
        for item in build_lifecycle_tools(
            runtime=runtime,
            workflow_cycle_id=cycle.workflow_cycle_id,
            publish_plan=lambda **_kwargs: (1, NOW),
            publish_result=lambda **_kwargs: (2, NOW),
            tracer=AgentTracer(sink),
        )
    }
    result = tools["replan_current_feedback"].invoke({})

    assert "cumulative replacement plan" in result
    assert runtime.task(task.task_run_id).phase == TaskPhase.PLANNING
    event = next(item for item in sink.events if item.category == "FEEDBACK REPLAN")
    assert review.feedback_review_id in event.message
    assert feedback.body not in event.message
    store.close()


def test_controller_traces_exact_feedback_review_start(tmp_path):
    store, repo, runtime, cycle, task = setup_runtime(tmp_path)
    plan = submit_plan(runtime, task)
    feedback = source(
        repo,
        "trace-review",
        body="@agent Keep existing JSON fields.",
        created_at="2026-01-01T00:15:00Z",
    )
    record(store, feedback)

    class PendingDriver:
        def has_pending_interrupt(self, **_kwargs):
            return True

    sink = CaptureSink()
    controller = object.__new__(DeclarativeWorkflowController)
    controller.store = store
    controller.runtime = runtime
    controller.client = None
    controller.clock = lambda: NOW
    controller.tracer = AgentTracer(sink)
    payload = controller._plan_wait_resume(
        cycle, runtime.task(task.task_run_id), PendingDriver()
    )

    assert payload["kind"] == "PLAN_FEEDBACK"
    trace = next(
        item for item in sink.events if item.category == "FEEDBACK REVIEW START"
    )
    assert payload["feedback_review_id"] in trace.message
    assert feedback.body not in trace.message
    assert trace.context.origin_surface == OriginSurface.ISSUE.value
    assert payload["occurrence_key"] == plan.approval_occurrence_key
    store.close()
