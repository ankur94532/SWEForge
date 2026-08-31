"""Native Deep Agents ``write_todos`` working memory inside EXECUTING.

The tool itself comes from LangChain's ``TodoListMiddleware``; SWEForge only
authorizes it per phase. These tests hold both halves of that split: the tool is
genuinely the native one, and it never becomes workflow authority.
"""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from sweforge.agent import _build_backend, build_durable_workflow_agent
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow_middleware import (
    HARNESS_EXECUTION_TOOLS,
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
from tests.harness.models import ScriptedChatModel

EXECUTION_TOOLS = ("read_file", "write_file", "edit_file", "execute")


class Runtime:
    def __init__(self):
        self.reauthorized = []

    def assert_execution_authorized(self, task_run_id):
        self.reauthorized.append(task_run_id)


class Authority:
    """Mirrors the fake used by ``tests/test_workflow_middleware.py``."""

    def __init__(self, phase=TaskPhase.EXECUTING, tools=EXECUTION_TOOLS, skills=()):
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


def _done(content):
    return {"content": content, "status": "completed"}


def _todo_call(call_id, todos):
    return {"name": "write_todos", "id": call_id, "args": {"todos": todos}}


def offered(phase, tools=EXECUTION_TOOLS):
    """Model-visible tool names for one phase, including the native todo tool."""
    policy = WorkflowPolicyMiddleware(Authority(phase, tools))
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name)
            for name in (
                *tools,
                "write_todos",
                "submit_plan",
                "finish_execution",
                "finish_validation",
                "run_validation",
                "task",
            )
        ]
    )
    return policy.wrap_model_call(request, lambda item: item)


# --- phase exposure -------------------------------------------------------


def test_executing_root_model_sees_native_write_todos():
    captured = offered(TaskPhase.EXECUTING)
    assert "write_todos" in {tool.name for tool in captured.tools}


@pytest.mark.parametrize("phase", [TaskPhase.PLANNING, TaskPhase.VALIDATING])
def test_write_todos_is_absent_outside_execution(phase):
    captured = offered(phase)
    assert "write_todos" not in {tool.name for tool in captured.tools}


def test_execution_prompt_scopes_todos_to_the_authorized_plan_identity():
    captured = offered(TaskPhase.EXECUTING)
    content = captured.system_message.content
    assert "plan=plan-A" in content and "attempt=1" in content
    assert "finish_execution, which remains the only way to leave EXECUTING" in content
    assert "can never broaden WHAT that plan authorizes" in content


@pytest.mark.parametrize("phase", [TaskPhase.PLANNING, TaskPhase.VALIDATING])
def test_todo_guidance_is_not_offered_outside_execution(phase):
    assert "write_todos" not in offered(phase).system_message.content


def test_feedback_review_state_never_offers_todos():
    authority = Authority(TaskPhase.EXECUTING)
    snapshot = replace(authority.snapshot(), feedback_review_status="REVIEWING")
    assert "write_todos" not in WorkflowPolicyMiddleware.allowed_tools(snapshot)
    deferred = replace(authority.snapshot(), feedback_review_status="DEFERRED_WAITING")
    assert "write_todos" not in WorkflowPolicyMiddleware.allowed_tools(deferred)


# --- delegation -----------------------------------------------------------


def test_investigator_never_receives_write_todos():
    authority = Authority(TaskPhase.EXECUTING, (*EXECUTION_TOOLS, "write_todos"))
    policy = DelegatedWorkflowPolicyMiddleware(authority)
    request = ModelRequest(
        tools=[SimpleNamespace(name=name) for name in ("read_file", "write_todos")]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert [tool.name for tool in captured.tools] == ["read_file"]
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("write_todos"), lambda _item: "ran")


# --- call-time authorization ---------------------------------------------


@pytest.mark.parametrize("phase", [TaskPhase.PLANNING, TaskPhase.VALIDATING])
def test_stale_write_todos_call_outside_execution_fails_closed(phase):
    policy = WorkflowPolicyMiddleware(Authority(phase))
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("write_todos"), lambda _item: "ran")


def test_write_todos_still_revalidates_the_execution_permit():
    authority = Authority(TaskPhase.EXECUTING)
    policy = WorkflowPolicyMiddleware(authority)
    assert (
        policy.wrap_tool_call(tool_request("write_todos"), lambda _item: "ran") == "ran"
    )
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_write_todos_cannot_run_when_execution_is_no_longer_authorized():
    class Revoked(Runtime):
        def assert_execution_authorized(self, task_run_id):
            raise PermissionError("execution permit is invalidated")

    authority = Authority(TaskPhase.EXECUTING)
    authority.runtime = Revoked()
    policy = WorkflowPolicyMiddleware(authority)
    with pytest.raises(PermissionError, match="permit is invalidated"):
        policy.wrap_tool_call(tool_request("write_todos"), lambda _item: "ran")


# --- workflow specification vocabulary ------------------------------------


def _spec(order=("A", "B")):
    deps = {"A": [], "B": ["A"]}
    return parse_workflow_spec(
        {
            "version": 1,
            "workflow_id": "todo-flow",
            "tasks": [
                {
                    "id": name,
                    "depends_on": deps[name],
                    "planning": {
                        "skill": f"{name}-planning",
                        "tools": ["read_file", "glob", "grep"],
                    },
                    "execution": {
                        "skill": f"{name}-execution",
                        "tools": list(EXECUTION_TOOLS),
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


def test_write_todos_is_not_operator_vocabulary():
    assert "write_todos" not in BUILTIN_WORKFLOW_TOOLS
    assert HARNESS_EXECUTION_TOOLS == frozenset({"write_todos"})


def test_existing_specs_parse_and_keep_their_exact_tool_grants():
    spec = _spec()
    assert [task.id for task in spec.tasks] == ["A", "B"]
    for task in spec.tasks:
        for phase in (task.planning, task.execution, task.validation):
            assert "write_todos" not in phase.tools
    assert spec.digest == _spec().digest


def test_a_workflow_naming_write_todos_is_still_rejected():
    document = _spec().canonical_document()
    document["tasks"][0]["execution"]["tools"].append("write_todos")
    with pytest.raises(ValueError, match="unknown tools"):
        parse_workflow_spec(document)


def test_revision_derivation_is_unchanged_by_todo_support():
    revision = derive_revision_spec(_spec())
    for phase in (
        revision.tasks[0].planning,
        revision.tasks[0].execution,
        revision.tasks[0].validation,
    ):
        assert "write_todos" not in phase.tools


# --- real runtime ---------------------------------------------------------


def _event(repo, number=7):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=str(number),
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=number,
        author_login="owner",
        body="@agent implement the workflow",
        html_url=None,
    )


@pytest.fixture
def runtime(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(123, "example/repo")
    event = _event(repo)
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [event],
        since="now",
        etag=None,
        polled_at="now",
    )
    thread_id = store.source_event(event.event_key)["thread_id"]
    engine = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    yield store, engine, thread_id, event
    store.close()


def _cycle(engine, thread_id, event, spec, **kwargs):
    return engine.initialize_cycle(
        thread_id=thread_id,
        cycle_id=kwargs.pop("cycle_id", 1),
        root_input_id=event.event_key,
        spec=spec,
        spec_ref="/etc/sweforge/workflow.yaml",
        **kwargs,
    )


def _approve(engine, task):
    plan = engine.submit_posted_plan(
        task_run_id=task.task_run_id,
        plan_text=f"Plan for {task.task_id}",
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
    return engine.task(task.task_run_id)


def test_todo_activity_never_moves_the_authoritative_task(runtime):
    store, engine, thread_id, event = runtime
    cycle = _cycle(engine, thread_id, event, _spec())
    task = engine.select_active_task(cycle.workflow_cycle_id)
    executing = _approve(engine, task)
    assert executing.phase == TaskPhase.EXECUTING

    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, _spec())
    policy = WorkflowPolicyMiddleware(authority)
    assert "write_todos" in policy.allowed_tools(authority.snapshot())

    # Write, update, then complete every todo through the authorized path.
    for _ in range(3):
        policy.wrap_tool_call(
            tool_request(
                "write_todos", {"todos": [{"content": "step", "status": "completed"}]}
            ),
            lambda _item: "Updated todo list",
        )
    after = engine.task(task.task_run_id)
    assert after.phase == TaskPhase.EXECUTING
    assert after.status == TaskPhase.EXECUTING
    assert after.execution_attempt == executing.execution_attempt
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == task.task_id

    # Completing all todos did not finish anything; the gateway still must run.
    validating = engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"tests": "passed"},
            "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
        },
    )
    assert validating.phase == TaskPhase.VALIDATING
    assert engine.task(task.task_run_id).status != TaskPhase.DONE


def test_validation_gateway_behaviour_is_unchanged(runtime):
    store, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    _approve(engine, task)
    engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"tests": "passed"},
            "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
        },
    )
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    allowed = WorkflowPolicyMiddleware.allowed_tools(authority.snapshot())
    assert "run_validation" in allowed and "write_todos" not in allowed

    engine.finish_validation(
        task_run_id=task.task_run_id,
        verdict=ValidationVerdict.ACCEPT,
        summary="validated",
        findings=[],
        repair_instructions=[],
        evidence={
            "reported": {"tests": "passed"},
            "validation_runs": [{"diff": "", "executions": []}],
        },
    )
    result = engine.publish_validated_result(
        task_run_id=task.task_run_id,
        posted_comment_id=200,
        posted_at="2026-01-01T00:13:00Z",
    )
    assert engine.task(task.task_run_id).phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL
    engine.approve_result(
        task_run_id=task.task_run_id,
        occurrence_key=result.result_occurrence_key,
        approval_event_key="result-approval",
        approved_by="maintainer",
        approval_is_authorized=True,
        approval_occurred_at="2026-01-01T00:14:00Z",
    )
    assert engine.task(task.task_run_id).status == TaskPhase.DONE


def test_revision_execution_gets_todos_while_its_other_phases_do_not(runtime):
    store, engine, thread_id, event = runtime
    revision_spec = derive_revision_spec(_spec())
    cycle = _cycle(
        engine,
        thread_id,
        event,
        revision_spec,
        cycle_kind=WorkflowCycleKind.REVISION,
        revision_sequence=1,
    )
    assert cycle.cycle_kind == WorkflowCycleKind.REVISION
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, revision_spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)

    assert task.phase == TaskPhase.PLANNING
    assert "write_todos" not in WorkflowPolicyMiddleware.allowed_tools(
        authority.snapshot()
    )

    _approve(engine, task)
    assert "write_todos" in WorkflowPolicyMiddleware.allowed_tools(authority.snapshot())

    engine.finish_execution(
        task.task_run_id,
        evidence={
            "reported": {"done": True},
            "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
        },
    )
    assert engine.task(task.task_run_id).phase == TaskPhase.VALIDATING
    assert "write_todos" not in WorkflowPolicyMiddleware.allowed_tools(
        authority.snapshot()
    )


# --- integration with the real Deep Agents harness ------------------------


def _real_agent(tmp_path, authority, model, checkpointer=None):
    return build_durable_workflow_agent(
        planning_model=model,
        execution_model=model,
        validation_model=model,
        backend=_build_backend(str(tmp_path)),
        authority=authority,
        lifecycle_tools=[],
        capability_tools=[],
        read_skill=lambda _cycle_id, skill: f"---\nname: {skill}\n---\nbody",
        checkpointer=checkpointer,
    )


def _tool_names(agent):
    node = agent.nodes["tools"]
    return set(getattr(getattr(node, "bound", node), "tools_by_name", {}))


def _executing_cycle(engine, thread_id, event, spec):
    cycle = _cycle(engine, thread_id, event, spec)
    task = engine.select_active_task(cycle.workflow_cycle_id)
    _approve(engine, task)
    return cycle, task


def test_real_deep_agent_harness_supplies_the_native_todo_tool(tmp_path, runtime):
    """Prove the tool is Deep Agents' own, not a SWEForge allowlist entry."""
    _store, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    engine.select_active_task(cycle.workflow_cycle_id)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    agent = _real_agent(tmp_path, authority, ScriptedChatModel([]))

    assert "write_todos" in _tool_names(agent)
    node = agent.nodes["tools"]
    tool = getattr(getattr(node, "bound", node), "tools_by_name")["write_todos"]
    native = TodoListMiddleware().tools[0]
    assert tool.name == native.name
    assert tool.args_schema is native.args_schema
    # The middleware's own parallel-call guard node is wired into the graph, and
    # its todo state is a real channel, so todos ride the checkpointer.
    assert "TodoListMiddleware.after_model" in agent.nodes
    assert "todos" in set(agent.stream_channels_list)


def test_native_todo_tool_really_runs_and_persists_without_moving_the_workflow(
    tmp_path, runtime
):
    """End-to-end: the real harness offers, executes and checkpoints todos."""
    _store, engine, thread_id, event = runtime
    spec = _spec()
    cycle, task = _executing_cycle(engine, thread_id, event, spec)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)

    todos = [
        {"content": "inspect implementation", "status": "in_progress"},
        {"content": "make the smallest fix", "status": "pending"},
    ]
    completed = [
        {"content": "inspect implementation", "status": "completed"},
        {"content": "make the smallest fix", "status": "completed"},
    ]
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write_todos", "id": "c1", "args": {"todos": todos}}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[_todo_call("c2", completed)],
            ),
            AIMessage(content="execution work is done"),
        ]
    )
    saver = InMemorySaver()
    agent = _real_agent(tmp_path, authority, model, checkpointer=saver)
    config = {"configurable": {"thread_id": task.thread_id}}
    result = agent.invoke({"messages": [{"role": "user", "content": "go"}]}, config)

    # The real native tool ran, twice, through SWEForge's policy middleware.
    assert result["todos"] == completed
    assert agent.get_state(config).values["todos"] == completed
    assert any("write_todos" in surface for surface in model.surfaces)

    # Every todo is complete, and the workflow has not moved at all.
    after = engine.task(task.task_run_id)
    assert after.phase == TaskPhase.EXECUTING
    assert after.status == TaskPhase.EXECUTING
    assert engine.cycle(cycle.workflow_cycle_id).active_task_id == task.task_id
    assert not engine.publication_is_eligible(cycle.workflow_cycle_id)

    # Only the lifecycle gateway advances it.
    assert (
        engine.finish_execution(
            task.task_run_id,
            evidence={
                "reported": {"tests": "passed"},
                "tool_observations": [{"command": "t", "exit_code": 0, "output": "ok"}],
            },
        ).phase
        == TaskPhase.VALIDATING
    )


def test_native_todos_survive_a_restart_and_stay_non_authoritative(tmp_path, runtime):
    """Todo state is durable working memory, never recovered authority."""
    _store, engine, thread_id, event = runtime
    spec = _spec()
    cycle, task = _executing_cycle(engine, thread_id, event, spec)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    todos = [{"content": "reproduce issue", "status": "in_progress"}]
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": task.thread_id}}

    first = _real_agent(
        tmp_path,
        authority,
        ScriptedChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[_todo_call("c1", todos)],
                ),
                AIMessage(content="paused"),
            ]
        ),
        checkpointer=saver,
    )
    first.invoke({"messages": [{"role": "user", "content": "go"}]}, config)

    # A freshly constructed agent over the same checkpoint sees the same todos,
    # while authoritative phase/ownership still come only from SQLite.
    rebuilt = _real_agent(
        tmp_path, authority, ScriptedChatModel([]), checkpointer=saver
    )
    assert rebuilt.get_state(config).values["todos"] == todos
    assert engine.task(task.task_run_id).phase == TaskPhase.EXECUTING
    assert engine.task(task.task_run_id).status != TaskPhase.DONE


def test_native_todo_state_is_omitted_from_the_graph_input_schema(tmp_path, runtime):
    """Deep Agents 0.7.8 marks `todos` OmitFromInput.

    That is why stale todos are handled by execution instructions rather than by
    an external deterministic reset: `update_state` cannot legally write the
    channel, and SWEForge does not invent its own todo persistence to work
    around it.
    """
    _store, engine, thread_id, event = runtime
    spec = _spec()
    cycle = _cycle(engine, thread_id, event, spec)
    engine.select_active_task(cycle.workflow_cycle_id)
    authority = WorkflowAuthority(engine, cycle.workflow_cycle_id, spec)
    agent = _real_agent(tmp_path, authority, ScriptedChatModel([]), InMemorySaver())

    assert "todos" not in set(agent.get_input_jsonschema()["properties"])
    config = {"configurable": {"thread_id": "github:123:issue:7"}}
    agent.update_state(config, {"todos": [{"content": "x", "status": "pending"}]})
    assert "todos" not in agent.get_state(config).values
