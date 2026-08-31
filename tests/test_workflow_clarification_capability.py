"""`request_clarification` as an automatic root lifecycle service.

The durable clarification machinery already existed; what was missing was
capability exposure. These tests hold both halves: the gateway is reachable
from every normal runnable phase without being named in a workflow, and it
still cannot become workflow authority.
"""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from sweforge.github_models import (
    InteractionMode,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
)
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_middleware import (
    ROOT_LIFECYCLE_SERVICES,
    DelegatedWorkflowPolicyMiddleware,
    WorkflowAuthority,
    WorkflowPolicyMiddleware,
    WorkflowPolicySnapshot,
)
from sweforge.workflow_runtime import (
    TaskPhase,
    ValidationVerdict,
    WorkflowCycleKind,
    WorkflowRuntime,
)
from sweforge.workflow_spec import (
    BUILTIN_WORKFLOW_TOOLS,
    derive_revision_spec,
    parse_workflow_spec,
)

RUNNABLE = (TaskPhase.PLANNING, TaskPhase.EXECUTING, TaskPhase.VALIDATING)


class Runtime:
    def __init__(self):
        self.reauthorized = []

    def assert_execution_authorized(self, task_run_id):
        self.reauthorized.append(task_run_id)


class Authority:
    def __init__(self, phase=TaskPhase.PLANNING, tools=("read_file",), skills=()):
        self.runtime = Runtime()
        self.phase = phase
        self.tools = tools
        self.skills = tuple(skills)

    def snapshot(self):
        return WorkflowPolicySnapshot(
            workflow_id="flow",
            workflow_digest="digest",
            workflow_cycle_id="cycle",
            cycle_id=1,
            active_task_id="A",
            task_run_id="task-run-A",
            phase=self.phase,
            skill=(self.skills[0] if self.skills else f"A-{self.phase.value.lower()}"),
            skills=self.skills,
            configured_tools=frozenset(self.tools),
            current_plan_id="plan-A",
            execution_attempt=1,
        )


@dataclass(frozen=True)
class ModelRequest:
    tools: list
    system_message: object | None = None
    model: object | None = None

    def override(self, **values):
        return replace(self, **values)


def tool_request(name, args=None):
    return SimpleNamespace(tool_call={"name": name, "args": args or {}})


# --- capability exposure --------------------------------------------------


@pytest.mark.parametrize("phase", RUNNABLE)
def test_clarification_is_offered_even_when_the_phase_spec_omits_it(phase):
    """The workflow never names it; the model still sees it."""
    authority = Authority(phase, tools=("read_file",))
    assert "request_clarification" not in authority.snapshot().configured_tools
    policy = WorkflowPolicyMiddleware(authority)
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name)
            for name in ("read_file", "request_clarification")
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert "request_clarification" in {tool.name for tool in captured.tools}
    assert "request_clarification" in captured.system_message.content


@pytest.mark.parametrize("phase", RUNNABLE)
def test_explicitly_listing_clarification_stays_backward_compatible(phase):
    """An installed spec that already names it keeps identical semantics."""
    listed = WorkflowPolicyMiddleware.allowed_tools(
        Authority(phase, tools=("read_file", "request_clarification")).snapshot()
    )
    omitted = WorkflowPolicyMiddleware.allowed_tools(
        Authority(phase, tools=("read_file",)).snapshot()
    )
    assert listed == omitted
    assert "request_clarification" in listed


def test_clarification_remains_workflow_vocabulary_for_installed_specs():
    assert "request_clarification" in BUILTIN_WORKFLOW_TOOLS
    spec = parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "legacy",
            "tasks": [
                {
                    "id": "only",
                    "depends_on": [],
                    "planning": {
                        "skill": "plan",
                        "tools": ["read_file", "request_clarification"],
                    },
                    "execution": {
                        "skill": "execute",
                        "tools": ["read_file", "request_clarification"],
                    },
                    "validation": {
                        "skill": "validate",
                        "tools": ["read_file", "run_validation"],
                    },
                }
            ],
        }
    )
    assert "request_clarification" in spec.tasks[0].planning.tools


@pytest.mark.parametrize("status", ["REVIEWING", "DEFERRED_WAITING"])
def test_feedback_review_states_cannot_ask_a_clarification(status):
    snapshot = replace(
        Authority(TaskPhase.PLANNING).snapshot(), feedback_review_status=status
    )
    assert "request_clarification" not in WorkflowPolicyMiddleware.allowed_tools(
        snapshot
    )


@pytest.mark.parametrize("phase", RUNNABLE)
def test_investigator_never_receives_clarification(phase):
    authority = Authority(phase, tools=("read_file", "request_clarification"))
    policy = DelegatedWorkflowPolicyMiddleware(authority)
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name)
            for name in ("read_file", "request_clarification")
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert [tool.name for tool in captured.tools] == ["read_file"]
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("request_clarification"), lambda _i: "ran")


def test_root_lifecycle_services_is_exactly_clarification():
    assert ROOT_LIFECYCLE_SERVICES == frozenset({"request_clarification"})


def test_executing_clarification_still_revalidates_the_permit():
    authority = Authority(TaskPhase.EXECUTING)
    policy = WorkflowPolicyMiddleware(authority)
    assert (
        policy.wrap_tool_call(tool_request("request_clarification"), lambda _i: "ok")
        == "ok"
    )
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_executing_clarification_is_rejected_when_execution_is_deauthorized():
    class Revoked(Runtime):
        def assert_execution_authorized(self, task_run_id):
            raise PermissionError("execution permit is invalidated")

    authority = Authority(TaskPhase.EXECUTING)
    authority.runtime = Revoked()
    policy = WorkflowPolicyMiddleware(authority)
    with pytest.raises(PermissionError, match="permit is invalidated"):
        policy.wrap_tool_call(tool_request("request_clarification"), lambda _i: "ok")


@pytest.mark.parametrize("phase", [TaskPhase.PLANNING, TaskPhase.VALIDATING])
def test_planning_and_validation_clarification_need_no_execution_permit(phase):
    authority = Authority(phase)
    policy = WorkflowPolicyMiddleware(authority)
    assert (
        policy.wrap_tool_call(tool_request("request_clarification"), lambda _i: "ok")
        == "ok"
    )
    assert authority.runtime.reauthorized == []


def test_other_phase_capabilities_are_unchanged():
    """write_todos stays execution-only; run_validation stays validation-only."""
    for phase in RUNNABLE:
        allowed = WorkflowPolicyMiddleware.allowed_tools(Authority(phase).snapshot())
        assert ("write_todos" in allowed) == (phase == TaskPhase.EXECUTING)
        assert ("run_validation" in allowed) == (phase == TaskPhase.VALIDATING)


# --- real runtime ---------------------------------------------------------


def _spec(order=("A", "B"), clarification=False):
    """Two tasks; no phase names request_clarification unless asked."""
    extra = ["request_clarification"] if clarification else []
    deps = {"A": [], "B": ["A"]}
    return parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "clarify-flow",
            "tasks": [
                {
                    "id": name,
                    "depends_on": deps[name],
                    "planning": {
                        "skill": f"{name}-planning",
                        "tools": ["read_file", "glob", *extra],
                    },
                    "execution": {
                        "skill": f"{name}-execution",
                        "tools": ["read_file", "edit_file", "execute", *extra],
                    },
                    "validation": {
                        "skill": f"{name}-validation",
                        "tools": ["read_file", "run_validation"],
                    },
                }
                for name in order
            ],
        }
    )


def _store(tmp_path, *, labels=()):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(77, "example/repo")
    event = SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id="1",
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=5,
        author_login="owner",
        body="@agent implement",
        html_url=None,
        issue_labels=labels,
    )
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id, "issues", [event], since="now", etag=None, polled_at="now"
    )
    return store, store.source_event(event.event_key)["thread_id"], event


@pytest.fixture
def runtime(tmp_path):
    store, thread_id, event = _store(tmp_path)
    engine = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    yield store, engine, thread_id, event
    store.close()


def _cycle(engine, thread_id, event, spec, **kwargs):
    return engine.initialize_cycle(
        thread_id=thread_id,
        cycle_id=kwargs.pop("cycle_id", 1),
        root_input_id=event.event_key,
        spec=spec,
        spec_ref="operator",
        **kwargs,
    )


def _approve(engine, task):
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text=f"Plan {task.task_id}",
        posted_comment_id=100 + task.declaration_index,
        posted_at="2026-01-01T00:11:00Z",
    )
    engine.approve_plan(
        task_run_id=task.task_run_id,
        occurrence_key=plan.approval_occurrence_key,
        approval_event_key=f"approval-{task.task_id}",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:12:00Z",
    )


def _execute(engine, task):
    engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"ok": True},
            "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
        },
    )


@pytest.mark.parametrize(
    ("advance", "phase"),
    [
        (None, TaskPhase.PLANNING),
        (_approve, TaskPhase.EXECUTING),
        ("validating", TaskPhase.VALIDATING),
    ],
)
def test_clarification_pauses_and_resumes_the_exact_originating_phase(
    runtime, advance, phase
):
    _store_, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    if advance == "validating":
        _approve(engine, task)
        _execute(engine, task)
    elif advance is not None:
        advance(engine, task)
    assert engine.task(task.task_run_id).phase == phase

    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    assert "request_clarification" in WorkflowPolicyMiddleware.allowed_tools(
        authority.snapshot()
    )

    occurrence = f"clarification:{task.task_run_id}:one"
    paused = engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    assert paused.phase == TaskPhase.WAITING_FOR_INPUT
    assert paused.waiting_from_phase == phase
    # Ownership is retained and no peer is selected.
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == task.task_id
    assert engine.select_active_task(cycle.workflow_cycle_id).task_id == task.task_id
    assert engine.task(task.task_run_id).status != TaskPhase.DONE
    assert not engine.publication_is_eligible(cycle.workflow_cycle_id)
    # A waiting task is not runnable, but the replay path restores its phase.
    with pytest.raises(PermissionError, match="not runnable"):
        authority.snapshot()
    assert authority.snapshot_for_tool("request_clarification").phase == phase

    resumed = engine.resume_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    assert resumed.phase == phase
    assert resumed.waiting_from_phase is None
    assert "request_clarification" in WorkflowPolicyMiddleware.allowed_tools(
        authority.snapshot()
    )


def test_waiting_task_keeps_ownership_against_a_ready_peer(runtime):
    _store_, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
    )
    for _ in range(3):
        assert (
            engine.select_active_task(cycle.workflow_cycle_id).task_id == task.task_id
        )
    statuses = {
        item.task_id: item.status for item in engine.task_runs(cycle.workflow_cycle_id)
    }
    assert statuses["B"] == TaskPhase.PENDING


def test_a_stale_occurrence_cannot_resume_a_clarification(runtime):
    _store_, engine, thread_id, event = runtime
    cycle = _cycle(engine, thread_id, event, _spec())
    task = engine.select_active_task(cycle.workflow_cycle_id)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
    )
    with pytest.raises(ValueError, match="stale"):
        engine.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key="clarification:A:other"
        )
    assert engine.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_INPUT


def test_clarification_does_not_authorize_execution_or_finish_anything(runtime):
    _store_, engine, thread_id, event = runtime
    cycle = _cycle(engine, thread_id, event, _spec())
    task = engine.select_active_task(cycle.workflow_cycle_id)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
    )
    engine.resume_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:one"
    )
    # Still PLANNING: no permit was created and execution is not authorized.
    assert engine.task(task.task_run_id).phase == TaskPhase.PLANNING
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)
    assert not engine.publication_is_eligible(cycle.workflow_cycle_id)


def test_executing_clarification_replay_keeps_its_permit_authorized(runtime):
    """LangGraph replays the gateway before resume_clarification runs.

    At that moment the durable phase is still WAITING_FOR_INPUT, so the
    execution reauthorization every root call performs must recognize a task
    paused *from* EXECUTING instead of refusing its own pause.
    """
    _store_, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    _approve(engine, task)
    occurrence = f"clarification:{task.task_run_id}:exec"
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    paused = engine.task(task.task_run_id)
    assert paused.phase == TaskPhase.WAITING_FOR_INPUT
    assert paused.waiting_from_phase == TaskPhase.EXECUTING

    permit = engine.assert_execution_authorized(task.task_run_id)
    assert permit.invalidated_at is None
    assert permit.approval_mode == "HUMAN"

    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    policy = WorkflowPolicyMiddleware(authority)
    assert (
        policy.wrap_tool_call(tool_request("request_clarification"), lambda _i: "ok")
        == "ok"
    )
    assert (
        engine.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key=occurrence
        ).phase
        == TaskPhase.EXECUTING
    )


def test_pausing_from_planning_never_authorizes_execution(runtime):
    """The relaxation is exact: only a pause that came from EXECUTING counts."""
    _store_, engine, thread_id, event = runtime
    cycle = _cycle(engine, thread_id, event, _spec())
    task = engine.select_active_task(cycle.workflow_cycle_id)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:plan"
    )
    assert engine.task(task.task_run_id).waiting_from_phase == TaskPhase.PLANNING
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)


def test_paused_execution_still_refuses_an_invalidated_permit(runtime):
    """Pausing does not grandfather a permit that later became stale."""
    _store_, engine, thread_id, event = runtime
    cycle = _cycle(engine, thread_id, event, _spec())
    task = engine.select_active_task(cycle.workflow_cycle_id)
    _approve(engine, task)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:exec"
    )
    engine.db.execute(
        "UPDATE workflow_task_permits_v1 SET invalidated_at=? WHERE task_run_id=?",
        ("2026-01-01T00:20:00Z", task.task_run_id),
    )
    engine.db.commit()
    with pytest.raises(PermissionError, match="permit is missing or stale"):
        engine.assert_execution_authorized(task.task_run_id)


def test_no_other_tool_can_reach_the_paused_execution_path(runtime):
    """Only the clarification gateway replays while WAITING_FOR_INPUT."""
    _store_, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    _approve(engine, task)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:A:exec"
    )
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    for name in ("edit_file", "execute", "write_todos", "finish_execution"):
        with pytest.raises(PermissionError, match="not runnable"):
            authority.snapshot_for_tool(name)


# --- AUTO -----------------------------------------------------------------


@pytest.fixture
def auto_runtime(tmp_path):
    store, thread_id, event = _store(tmp_path, labels=("AUTO",))
    engine = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    assert store.interaction_mode(thread_id) == InteractionMode.AUTO
    yield store, engine, thread_id, event
    store.close()


@pytest.mark.parametrize("phase", RUNNABLE)
def test_auto_clarification_genuinely_waits_for_a_human(auto_runtime, phase):
    """AUTO skips routine approvals; it must not auto-answer a question."""
    store, engine, thread_id, event = auto_runtime
    spec = _spec(order=("A",))
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    if phase is not TaskPhase.PLANNING:
        engine.submit_posted_plan(
            task_run_id=task.task_run_id,
            plan_text="Plan A",
            posted_comment_id=1,
            posted_at="2026-01-01T00:11:00Z",
        )
        engine.auto_authorize_plan(task.task_run_id)
    if phase is TaskPhase.VALIDATING:
        _execute(engine, task)
    assert engine.task(task.task_run_id).phase == phase

    occurrence = f"clarification:{task.task_run_id}:auto"
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    waiting = engine.task(task.task_run_id)
    assert waiting.phase == TaskPhase.WAITING_FOR_INPUT
    assert waiting.waiting_from_phase == phase

    # Nothing in AUTO resolves it: it stays waiting until a human answers.
    for _ in range(3):
        assert (
            engine.select_active_task(cycle.workflow_cycle_id).phase
            == TaskPhase.WAITING_FOR_INPUT
        )
    assert engine.task(task.task_run_id).clarification_occurrence_key == occurrence
    assert not engine.publication_is_eligible(cycle.workflow_cycle_id)

    assert (
        engine.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key=occurrence
        ).phase
        == phase
    )


def test_auto_resumes_and_still_performs_its_own_authorization(auto_runtime):
    store, engine, thread_id, event = auto_runtime
    spec = _spec(order=("A",))
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    occurrence = f"clarification:{task.task_run_id}:auto"
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    engine.resume_clarification(task_run_id=task.task_run_id, occurrence_key=occurrence)
    # The answer authorized nothing; AUTO still records its own exact permit.
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)
    engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="Plan A",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    permit = engine.auto_authorize_plan(task.task_run_id)
    assert permit.approval_mode == "AUTO"
    assert engine.task(task.task_run_id).phase == TaskPhase.EXECUTING
    _execute(engine, task)
    engine.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.ACCEPT,
        summary="ok",
        findings=[],
        repair_instructions=[],
        evidence={
            "reported": {"ok": True},
            "validation_runs": [{"diff": "", "executions": []}],
        },
    )
    engine.publish_validated_result(
        task_run_id=task.task_run_id, posted_comment_id=2, posted_at="t"
    )
    assert engine.auto_accept_result(task.task_run_id).mode == "AUTO"
    assert engine.task(task.task_run_id).status == TaskPhase.DONE


def test_manual_clarification_is_not_a_plan_approval(runtime):
    """A clarification answer must never stand in for `@agent approve`."""
    store, engine, thread_id, event = runtime
    assert store.interaction_mode(thread_id) == InteractionMode.MANUAL
    spec = _spec(order=("A",))
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    occurrence = f"clarification:{task.task_run_id}:manual"
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    engine.resume_clarification(task_run_id=task.task_run_id, occurrence_key=occurrence)
    engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text="Plan A",
        posted_comment_id=1,
        posted_at="2026-01-01T00:11:00Z",
    )
    # The clarification did not approve the plan; MANUAL still waits.
    assert engine.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL
    with pytest.raises(PermissionError, match="not executing"):
        engine.assert_execution_authorized(task.task_run_id)


# --- revision cycles ------------------------------------------------------


@pytest.mark.parametrize(
    ("advance", "phase"),
    [
        (None, TaskPhase.PLANNING),
        (_approve, TaskPhase.EXECUTING),
        ("validating", TaskPhase.VALIDATING),
    ],
)
def test_revision_clarification_works_and_keeps_phase_wise_skills(
    runtime, advance, phase
):
    _store_, engine, thread_id, event = runtime
    revision_spec = derive_revision_spec(_spec())
    cycle = _cycle(
        engine,
        thread_id,
        event,
        revision_spec,
        cycle_kind=WorkflowCycleKind.REVISION,
        revision_sequence=1,
    )
    task = engine.select_active_task(cycle.workflow_cycle_id)
    if advance == "validating":
        _approve(engine, task)
        _execute(engine, task)
    elif advance is not None:
        advance(engine, task)

    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, revision_spec)
    expected_skills = authority.snapshot().skills
    assert "request_clarification" in WorkflowPolicyMiddleware.allowed_tools(
        authority.snapshot()
    )

    occurrence = f"clarification:{task.task_run_id}:revision"
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key=occurrence
    )
    assert engine.task(task.task_run_id).waiting_from_phase == phase
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == "revision"
    # Skills stay exactly phase-specific across the pause.
    assert (
        authority.snapshot_for_tool("request_clarification").skills == expected_skills
    )

    assert (
        engine.resume_clarification(
            task_run_id=task.task_run_id, occurrence_key=occurrence
        ).phase
        == phase
    )
    assert authority.snapshot().skills == expected_skills
    # One revision, not a second one created because input was solicited.
    assert engine.next_revision_sequence(thread_id) == 2


def test_revision_clarification_keeps_phases_free_of_cross_phase_skills(runtime):
    _store_, engine, thread_id, event = runtime
    revision_spec = derive_revision_spec(_spec())
    cycle = _cycle(
        engine,
        thread_id,
        event,
        revision_spec,
        cycle_kind=WorkflowCycleKind.REVISION,
        revision_sequence=1,
    )
    task = engine.select_active_task(cycle.workflow_cycle_id)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, revision_spec)
    engine.pause_for_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:r:1"
    )
    engine.resume_clarification(
        task_run_id=task.task_run_id, occurrence_key="clarification:r:1"
    )
    assert authority.snapshot().skills == ("A-planning", "B-planning")
    _approve(engine, task)
    assert authority.snapshot().skills == ("A-execution", "B-execution")
    _execute(engine, task)
    assert authority.snapshot().skills == ("A-validation", "B-validation")
