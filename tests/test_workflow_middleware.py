from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from sweforge.agent import build_durable_workflow_agent, build_workflow_agent
from sweforge.workflow_middleware import (
    DelegatedWorkflowPolicyMiddleware,
    WorkflowPolicyMiddleware,
    WorkflowPolicySnapshot,
    WorkflowSkillsMiddleware,
)
from sweforge.workflow_runtime import TaskPhase


class Runtime:
    def __init__(self):
        self.reauthorized = []

    def assert_execution_authorized(self, task_run_id):
        self.reauthorized.append(task_run_id)


class Authority:
    def __init__(self, phase=TaskPhase.PLANNING, tools=("read_file", "glob")):
        self.runtime = Runtime()
        self.phase = phase
        self.tools = tools

    def snapshot(self):
        if self.phase == TaskPhase.WAITING_FOR_APPROVAL:
            raise PermissionError("not runnable")
        return WorkflowPolicySnapshot(
            workflow_id="flow",
            workflow_digest="digest",
            workflow_cycle_id="cycle",
            cycle_id=1,
            active_task_id="A",
            task_run_id="task-run-A",
            phase=self.phase,
            skill=f"A-{self.phase.value.lower()}",
            configured_tools=frozenset(self.tools),
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


def test_root_model_sees_only_phase_tools_gateway_and_bounded_task():
    policy = WorkflowPolicyMiddleware(Authority())
    request = ModelRequest(
        tools=[
            SimpleNamespace(name=name)
            for name in (
                "read_file",
                "glob",
                "write_file",
                "execute",
                "submit_plan",
                "finish_execution",
                "task",
            )
        ]
    )
    captured = policy.wrap_model_call(request, lambda item: item)
    assert {tool.name for tool in captured.tools} == {
        "read_file",
        "glob",
        "submit_plan",
        "task",
    }
    assert "active_task=A" in captured.system_message.content


def test_planning_cannot_mutate_even_with_stale_tool_call():
    policy = WorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="forbidden"):
        policy.wrap_tool_call(tool_request("edit_file"), lambda item: "ran")


def test_mutating_tool_rechecks_exact_permit_at_call_time():
    authority = Authority(
        TaskPhase.EXECUTING, ("read_file", "write_file", "edit_file", "execute")
    )
    policy = WorkflowPolicyMiddleware(authority)
    assert policy.wrap_tool_call(tool_request("edit_file"), lambda item: "ran") == "ran"
    assert authority.runtime.reauthorized == ["task-run-A"]


def test_inactive_task_skill_is_rejected():
    policy = WorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="inactive"):
        policy.wrap_tool_call(
            tool_request("read_file", {"file_path": "/skills/B-planning/SKILL.md"}),
            lambda item: "leaked",
        )


@pytest.mark.parametrize(
    "name",
    [
        "submit_plan",
        "finish_execution",
        "finish_validation",
        "edit_file",
        "execute",
        "task",
    ],
)
def test_investigator_cannot_call_lifecycle_mutation_or_delegate(name):
    delegated = DelegatedWorkflowPolicyMiddleware(Authority())
    with pytest.raises(PermissionError, match="forbidden"):
        delegated.wrap_tool_call(tool_request(name), lambda item: "ran")


def test_investigator_can_use_read_only_tool_in_active_phase():
    delegated = DelegatedWorkflowPolicyMiddleware(Authority())
    assert (
        delegated.wrap_tool_call(
            tool_request("read_file", {"file_path": "src/example.py"}),
            lambda item: "evidence",
        )
        == "evidence"
    )


def test_waiting_for_approval_runs_no_root_or_subagent_model():
    authority = Authority(TaskPhase.WAITING_FOR_APPROVAL)
    request = ModelRequest(tools=[])
    with pytest.raises(PermissionError, match="not runnable"):
        WorkflowPolicyMiddleware(authority).wrap_model_call(request, lambda item: item)
    with pytest.raises(PermissionError, match="not runnable"):
        DelegatedWorkflowPolicyMiddleware(authority).wrap_model_call(
            request, lambda item: item
        )


def test_skill_middleware_discloses_only_active_skill():
    seen = []

    def read_skill(cycle_id, skill):
        seen.append((cycle_id, skill))
        return "Only A planning instructions"

    request = ModelRequest(tools=[])
    result = WorkflowSkillsMiddleware(Authority(), read_skill).wrap_model_call(
        request, lambda item: item
    )
    assert seen == [(1, "A-planning")]
    assert "Only A planning" in result.system_message.content
    assert "B-" not in result.system_message.content


def test_canonical_builder_requires_explicit_bounded_default_override(monkeypatch):
    with pytest.raises(ValueError, match="explicit bounded"):
        build_workflow_agent(
            model="provider:model",
            tools=[],
            backend=object(),
            system_prompt="test",
            subagents=[],
        )

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "compiled"

    monkeypatch.setattr("sweforge.agent.create_deep_agent", fake_create)
    result = build_workflow_agent(
        model="provider:model",
        tools=[],
        backend=object(),
        system_prompt="test",
        subagents=[
            {
                "name": "general-purpose",
                "description": "read only",
                "system_prompt": "inspect",
                "tools": [],
            }
        ],
    )
    assert result == "compiled"
    assert captured["name"] == "sweforge-workflow-root"
    assert captured["subagents"][0]["name"] == "general-purpose"


def test_durable_builder_keeps_gateways_root_only(monkeypatch):
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return "root"

    monkeypatch.setattr("sweforge.agent.build_workflow_agent", fake_builder)
    monkeypatch.setattr(
        "sweforge.agent.WorkflowPolicyMiddleware",
        lambda authority, phase_models: SimpleNamespace(
            authority=authority, phase_models=phase_models
        ),
    )
    gateway = SimpleNamespace(name="submit_plan")
    result = build_durable_workflow_agent(
        planning_model="openai:test",
        execution_model="openai:test",
        validation_model="openai:test",
        backend=object(),
        authority=Authority(),
        lifecycle_tools=[gateway],
        read_skill=lambda cycle, skill: "instructions",
        checkpointer=object(),
    )
    assert result == "root"
    assert gateway in captured["tools"]
    investigator = captured["subagents"][0]
    assert gateway not in investigator["tools"]
    assert investigator["name"] == "general-purpose"
