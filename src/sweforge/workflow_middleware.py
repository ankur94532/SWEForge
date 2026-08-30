"""Authoritative phase policy for the single workflow-owning Deep Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from deepagents._models import resolve_model
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from .workflow_runtime import TaskPhase, WorkflowRuntime
from .workflow_spec import WorkflowSpec

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "execute"})
RESEARCH_TOOLS = frozenset({"ls", "read_file", "glob", "grep", "search_issue_memory"})


@dataclass(frozen=True, slots=True)
class WorkflowPolicySnapshot:
    workflow_id: str
    workflow_digest: str
    workflow_cycle_id: str
    cycle_id: int
    active_task_id: str
    task_run_id: str
    phase: TaskPhase
    skill: str
    configured_tools: frozenset[str]
    skills: tuple[str, ...] = ()


class WorkflowAuthority:
    """Re-read authoritative runtime state for every policy decision."""

    def __init__(
        self, runtime: WorkflowRuntime, workflow_cycle_id: str, spec: WorkflowSpec
    ) -> None:
        self.runtime = runtime
        self.workflow_cycle_id = workflow_cycle_id
        self.spec = spec

    def snapshot(self) -> WorkflowPolicySnapshot:
        return self._snapshot_for_replay(None)

    def snapshot_for_tool(self, tool_name: str) -> WorkflowPolicySnapshot:
        return self._snapshot_for_replay(tool_name)

    def resume_snapshot(self, kind: str) -> WorkflowPolicySnapshot:
        replay_tool = {
            "PLAN_APPROVAL": "submit_plan",
            "PLAN_FEEDBACK": "submit_plan",
            "RESULT_APPROVAL": "finish_validation",
            "RESULT_FEEDBACK": "finish_validation",
            "CLARIFICATION_RESPONSE": "request_clarification",
        }.get(kind)
        if replay_tool is None:
            raise PermissionError("workflow resume kind is invalid")
        return self._snapshot_for_replay(replay_tool)

    def _snapshot_for_replay(self, replay_tool: str | None) -> WorkflowPolicySnapshot:
        cycle = self.runtime.cycle(self.workflow_cycle_id)
        task = self.runtime.active_task(self.workflow_cycle_id)
        if task is None or cycle.active_task_id != task.task_id:
            raise PermissionError("workflow has no active task")
        if (
            task.phase == TaskPhase.WAITING_FOR_INPUT
            and replay_tool == "request_clarification"
            and task.waiting_from_phase is not None
        ):
            phase = task.waiting_from_phase
        elif (
            task.phase == TaskPhase.WAITING_FOR_PLAN_APPROVAL
            and replay_tool == "submit_plan"
        ):
            phase = TaskPhase.PLANNING
        elif (
            task.phase == TaskPhase.WAITING_FOR_RESULT_APPROVAL
            and replay_tool == "finish_validation"
        ):
            phase = TaskPhase.VALIDATING
        else:
            phase = task.phase
        if phase not in (
            TaskPhase.PLANNING,
            TaskPhase.EXECUTING,
            TaskPhase.VALIDATING,
        ):
            raise PermissionError(f"workflow phase {task.phase} is not runnable")
        phase_spec = self.runtime.phase_spec(self.spec, task)
        return WorkflowPolicySnapshot(
            workflow_id=cycle.workflow_id,
            workflow_digest=cycle.workflow_digest,
            workflow_cycle_id=cycle.workflow_cycle_id,
            cycle_id=cycle.cycle_id,
            active_task_id=task.task_id,
            task_run_id=task.task_run_id,
            phase=phase,
            skill=phase_spec.skill,
            skills=phase_spec.skills,
            configured_tools=frozenset(phase_spec.tools),
        )


def _tool_name(tool: Any) -> str:
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", ""))


def _call_name(request: Any) -> str:
    call = request.tool_call
    value = call.get("name") if isinstance(call, Mapping) else getattr(call, "name", "")
    return str(value)


def _call_args(request: Any) -> Mapping[str, Any]:
    call = request.tool_call
    args = (
        call.get("args", {}) if isinstance(call, Mapping) else getattr(call, "args", {})
    )
    return args if isinstance(args, Mapping) else {}


def _phase_gateway(phase: TaskPhase) -> str:
    return {
        TaskPhase.PLANNING: "submit_plan",
        TaskPhase.EXECUTING: "finish_execution",
        TaskPhase.VALIDATING: "finish_validation",
    }[phase]


class WorkflowPolicyMiddleware(AgentMiddleware):
    """Filter model tools and reject stale calls immediately before execution."""

    def __init__(
        self,
        authority: WorkflowAuthority,
        *,
        phase_models: Mapping[TaskPhase, Any] | None = None,
    ) -> None:
        self.authority = authority
        self.phase_models = {
            phase: resolve_model(model) for phase, model in (phase_models or {}).items()
        }

    @staticmethod
    def allowed_tools(snapshot: WorkflowPolicySnapshot) -> frozenset[str]:
        # ``task`` delegates only to the explicitly bounded investigator.
        configured = snapshot.configured_tools
        if snapshot.phase in (TaskPhase.PLANNING, TaskPhase.VALIDATING):
            # A malformed or stale trusted spec cannot turn a read-only phase
            # into an execution phase. Custom MCP tools remain operator-owned;
            # built-in mutation is denied here and again at call time.
            configured = configured - MUTATING_TOOLS
        validation_service = (
            {"run_validation"} if snapshot.phase == TaskPhase.VALIDATING else set()
        )
        return (
            configured
            | validation_service
            | {
                _phase_gateway(snapshot.phase),
                "task",
            }
        )

    def wrap_model_call(self, request, handler):
        snapshot = self.authority.snapshot()
        allowed = self.allowed_tools(snapshot)
        tools = [tool for tool in request.tools if _tool_name(tool) in allowed]
        prompt = (
            f"Authoritative SWEForge context: workflow={snapshot.workflow_id} "
            f"cycle={snapshot.cycle_id} active_task={snapshot.active_task_id} "
            f"task_run={snapshot.task_run_id} phase={snapshot.phase.value}. "
            f"Only the application lifecycle gateway may finish this phase. "
            f"Active procedural skill: {snapshot.skill}."
        )
        existing = request.system_message
        content = (
            f"{getattr(existing, 'content', '')}\n\n{prompt}" if existing else prompt
        )
        overrides: dict[str, Any] = {
            "tools": tools,
            "system_message": SystemMessage(content=content),
        }
        if snapshot.phase in self.phase_models:
            overrides["model"] = self.phase_models[snapshot.phase]
        return handler(request.override(**overrides))

    def wrap_tool_call(self, request, handler):
        name = _call_name(request)
        snapshot_for_tool = getattr(self.authority, "snapshot_for_tool", None)
        snapshot = (
            snapshot_for_tool(name)
            if callable(snapshot_for_tool)
            else self.authority.snapshot()
        )
        if name not in self.allowed_tools(snapshot):
            raise PermissionError(
                f"tool {name!r} is forbidden for {snapshot.phase.value}"
            )
        if snapshot.phase == TaskPhase.EXECUTING and name != "task":
            # Re-authorize every root execution-phase call, including custom
            # MCP tools whose mutability cannot be inferred from their names.
            self.authority.runtime.assert_execution_authorized(snapshot.task_run_id)
        self._reject_inactive_skill_path(request, snapshot)
        return handler(request)

    @staticmethod
    def _reject_inactive_skill_path(
        request: Any, snapshot: WorkflowPolicySnapshot
    ) -> None:
        if _call_name(request) != "read_file":
            return
        args = _call_args(request)
        path = str(args.get("file_path") or args.get("path") or "")
        if not path.startswith("/skills/"):
            return
        skills = snapshot.skills or (snapshot.skill,)
        allowed_prefixes = tuple(f"/skills/{skill}/" for skill in skills)
        if not any(
            path == prefix + "SKILL.md" or path.startswith(prefix)
            for prefix in allowed_prefixes
        ):
            raise PermissionError("inactive task/phase skill access is forbidden")


class DelegatedWorkflowPolicyMiddleware(AgentMiddleware):
    """A strict subset policy for the explicit read-only investigator."""

    def __init__(self, authority: WorkflowAuthority) -> None:
        self.authority = authority

    def wrap_model_call(self, request, handler):
        snapshot = self.authority.snapshot()
        allowed = snapshot.configured_tools & RESEARCH_TOOLS
        tools = [tool for tool in request.tools if _tool_name(tool) in allowed]
        prompt = (
            "You are a bounded read-only investigator below the root workflow "
            f"agent. Inspect only active task {snapshot.active_task_id} in "
            f"{snapshot.phase.value}; return a concise evidence report. You cannot "
            "advance lifecycle state or delegate further."
        )
        return handler(
            request.override(
                tools=tools,
                system_message=SystemMessage(content=prompt),
            )
        )

    def wrap_tool_call(self, request, handler):
        snapshot = self.authority.snapshot()
        name = _call_name(request)
        if name not in (snapshot.configured_tools & RESEARCH_TOOLS):
            raise PermissionError(f"delegated tool {name!r} is forbidden")
        WorkflowPolicyMiddleware._reject_inactive_skill_path(request, snapshot)
        return handler(request)


class ReadOnlyInvestigatorMiddleware(AgentMiddleware):
    """Static non-workflow fallback that structurally bounds legacy delegation."""

    allowed = RESEARCH_TOOLS

    def wrap_model_call(self, request, handler):
        tools = [tool for tool in request.tools if _tool_name(tool) in self.allowed]
        return handler(request.override(tools=tools))

    def wrap_tool_call(self, request, handler):
        name = _call_name(request)
        if name not in self.allowed:
            raise PermissionError(f"investigator tool {name!r} is forbidden")
        return handler(request)


class WorkflowSkillsMiddleware(AgentMiddleware):
    """Progressively disclose exactly one operator-controlled phase skill."""

    def __init__(
        self,
        authority: WorkflowAuthority,
        read_skill: Callable[[int, str], str],
    ) -> None:
        self.authority = authority
        self.read_skill = read_skill

    def wrap_model_call(self, request, handler):
        snapshot = self.authority.snapshot()
        rendered = []
        for skill in snapshot.skills or (snapshot.skill,):
            content = self.read_skill(snapshot.cycle_id, skill)
            if not isinstance(content, str) or not content.strip():
                raise PermissionError(f"required workflow skill is missing: {skill}")
            rendered.append(f"[Trusted skill: {skill}]\n{content}")
        prefix = getattr(request.system_message, "content", "")
        prompt = f"{prefix}\n\n" + "\n\n".join(rendered)
        return handler(request.override(system_message=SystemMessage(content=prompt)))
