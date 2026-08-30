"""Authoritative phase policy for the single workflow-owning Deep Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from deepagents._models import resolve_model
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from .agent_trace import AgentTracer, TraceContext
from .skills import canonical_skill_path, parse_skill_metadata
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
    feedback_review_id: str | None = None
    feedback_review_kind: str | None = None
    feedback_review_status: str | None = None


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
        review = self.runtime.store.feedback_review_for_task(task.task_run_id)
        if review is None:
            review = self.runtime.store.feedback_review_for_task(
                task.task_run_id, statuses=("DEFERRED_WAITING",)
            )
        if (
            task.phase == TaskPhase.WAITING_FOR_INPUT
            and replay_tool == "request_clarification"
            and task.waiting_from_phase is not None
        ):
            phase = task.waiting_from_phase
        elif review is not None and review.status in {"REVIEWING", "DEFERRED_WAITING"}:
            phase = (
                TaskPhase.PLANNING
                if review.feedback_kind == "PLAN"
                else TaskPhase.VALIDATING
            )
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
            feedback_review_id=(review.feedback_review_id if review else None),
            feedback_review_kind=(review.feedback_kind if review else None),
            feedback_review_status=(review.status if review else None),
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
        tracer: AgentTracer | None = None,
        tool_effects: Mapping[str, str] | None = None,
    ) -> None:
        self.authority = authority
        self.tracer = tracer
        self.tool_effects = dict(tool_effects or {})
        self.phase_models = {
            phase: resolve_model(model) for phase, model in (phase_models or {}).items()
        }

    @staticmethod
    def allowed_tools(
        snapshot: WorkflowPolicySnapshot,
        tool_effects: Mapping[str, str] | None = None,
    ) -> frozenset[str]:
        # ``task`` delegates only to the explicitly bounded investigator.
        configured = snapshot.configured_tools
        if snapshot.feedback_review_status == "REVIEWING":
            research = RESEARCH_TOOLS | frozenset(
                name
                for name, effect in (tool_effects or {}).items()
                if effect == "read"
            )
            return (configured & research) | {
                "task",
                "replan_current_feedback",
                "defer_current_feedback_to_revision",
            }
        if snapshot.feedback_review_status == "DEFERRED_WAITING":
            return frozenset({"defer_current_feedback_to_revision"})
        if snapshot.phase in (TaskPhase.PLANNING, TaskPhase.VALIDATING):
            # A malformed or stale trusted spec cannot turn a read-only phase
            # into an execution phase. Custom MCP tools remain operator-owned;
            # built-in mutation is denied here and again at call time.
            configured = configured - MUTATING_TOOLS
            configured = configured - {
                name
                for name, effect in (tool_effects or {}).items()
                if effect == "mutate"
            }
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
        allowed = self.allowed_tools(snapshot, self.tool_effects)
        tools = [tool for tool in request.tools if _tool_name(tool) in allowed]
        prompt = (
            f"Authoritative SWEForge context: workflow={snapshot.workflow_id} "
            f"cycle={snapshot.cycle_id} active_task={snapshot.active_task_id} "
            f"task_run={snapshot.task_run_id} phase={snapshot.phase.value}. "
            f"Only the application lifecycle gateway may finish this phase. "
            "Authorized procedural skills: "
            f"{', '.join(snapshot.skills or (snapshot.skill,))}."
        )
        if snapshot.feedback_review_status == "REVIEWING":
            prompt += (
                " You are reviewing exact solicited user feedback for the current "
                f"{snapshot.feedback_review_kind.lower()} occurrence. Decide only "
                "whether it materially concerns the current task and approval "
                "scope. If relevant, call replan_current_feedback with no "
                "arguments. If clearly separate, call "
                "defer_current_feedback_to_revision with no arguments. Do not "
                "compose workflow pushback, expose reasoning, approve work, or "
                "select future ownership."
            )
        elif snapshot.feedback_review_status == "DEFERRED_WAITING":
            prompt += (
                " Recover the already-durable deferred-feedback checkpoint by "
                "calling defer_current_feedback_to_revision with no arguments. "
                "Do not reconsider or describe the semantic outcome."
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
        if name not in self.allowed_tools(snapshot, self.tool_effects):
            raise PermissionError(
                f"tool {name!r} is forbidden for {snapshot.phase.value}"
            )
        if snapshot.phase == TaskPhase.EXECUTING and name != "task":
            # Re-authorize every root execution-phase call, including custom
            # MCP tools whose mutability cannot be inferred from their names.
            self.authority.runtime.assert_execution_authorized(snapshot.task_run_id)
        skill_read = self._reject_inactive_skill_path(request, snapshot)
        result = handler(request)
        if self.tracer is not None and skill_read is not None:
            self.tracer.emit(
                "SKILL READ",
                f"skill={skill_read}",
                self._trace_context(snapshot),
            )
        return result

    @staticmethod
    def _reject_inactive_skill_path(
        request: Any, snapshot: WorkflowPolicySnapshot
    ) -> str | None:
        if _call_name(request) != "read_file":
            return None
        args = _call_args(request)
        path = str(args.get("file_path") or args.get("path") or "")
        if not path.startswith("/skills/"):
            return None
        raw_parts = path.split("/")
        if (
            "\\" in path
            or "%" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in raw_parts[1:])
            or str(PurePosixPath(path)) != path
        ):
            raise PermissionError("non-canonical skill path is forbidden")
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[1] != "skills":
            raise PermissionError("non-canonical skill path is forbidden")
        requested_skill = parts[2]
        skills = snapshot.skills or (snapshot.skill,)
        if requested_skill not in skills:
            raise PermissionError("inactive task/phase skill access is forbidden")
        return (
            requested_skill if path == canonical_skill_path(requested_skill) else None
        )

    @staticmethod
    def _trace_context(snapshot: WorkflowPolicySnapshot) -> TraceContext:
        return TraceContext(
            workflow_cycle_id=snapshot.workflow_cycle_id,
            cycle_id=snapshot.cycle_id,
            task_id=snapshot.active_task_id,
            task_run_id=snapshot.task_run_id,
            phase=snapshot.phase.value,
        )


class DelegatedWorkflowPolicyMiddleware(AgentMiddleware):
    """A strict subset policy for the explicit read-only investigator."""

    def __init__(
        self,
        authority: WorkflowAuthority,
        *,
        tracer: AgentTracer | None = None,
        tool_effects: Mapping[str, str] | None = None,
    ) -> None:
        self.authority = authority
        self.tracer = tracer
        self.tool_effects = dict(tool_effects or {})

    @property
    def research_tools(self) -> frozenset[str]:
        return RESEARCH_TOOLS | frozenset(
            name for name, effect in self.tool_effects.items() if effect == "read"
        )

    def wrap_model_call(self, request, handler):
        snapshot = self.authority.snapshot()
        allowed = snapshot.configured_tools & self.research_tools
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
        if name not in (snapshot.configured_tools & self.research_tools):
            raise PermissionError(f"delegated tool {name!r} is forbidden")
        skill_read = WorkflowPolicyMiddleware._reject_inactive_skill_path(
            request, snapshot
        )
        result = handler(request)
        if self.tracer is not None and skill_read is not None:
            self.tracer.emit(
                "SKILL READ",
                f"skill={skill_read}",
                WorkflowPolicyMiddleware._trace_context(snapshot),
            )
        return result


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
    """Eagerly load one exact skill or catalog multiple authorized skills."""

    def __init__(
        self,
        authority: WorkflowAuthority,
        read_skill: Callable[[int, str], str],
        *,
        tracer: AgentTracer | None = None,
    ) -> None:
        self.authority = authority
        self.read_skill = read_skill
        self.tracer = tracer

    def wrap_model_call(self, request, handler):
        snapshot = self.authority.snapshot()
        skills = snapshot.skills or (snapshot.skill,)
        if len(skills) == 1:
            skill = skills[0]
            content = self.read_skill(snapshot.cycle_id, skill)
            if not isinstance(content, str) or not content.strip():
                raise PermissionError(f"required workflow skill is missing: {skill}")
            rendered = f"[Trusted skill: {skill}]\n{content}"
        else:
            if "read_file" not in snapshot.configured_tools:
                raise PermissionError(
                    "multi-skill discovery requires phase-authorized read_file"
                )
            metadata = []
            seen: set[str] = set()
            for skill in skills:
                try:
                    content = self.read_skill(snapshot.cycle_id, skill)
                    item = parse_skill_metadata(skill, content)
                except PermissionError:
                    raise
                except (TypeError, ValueError) as exc:
                    raise PermissionError(
                        f"required workflow skill metadata is invalid: {skill}"
                    ) from exc
                if item.name in seen:
                    raise PermissionError("duplicate active skill metadata")
                seen.add(item.name)
                metadata.append(item)
            catalog = ["Trusted skills available for this task/phase:"]
            for item in metadata:
                catalog.extend(
                    [
                        f"- {item.name}: {item.description}",
                        f"  Load: {item.path}",
                    ]
                )
            catalog.extend(
                [
                    "Read the SKILL.md for skills relevant to the current work "
                    "before using their procedural guidance. Do not load unrelated "
                    "skills merely because they are available."
                ]
            )
            rendered = "\n".join(catalog)
            if self.tracer is not None:
                self.tracer.emit(
                    "SKILL CATALOG",
                    f"count={len(metadata)}",
                    WorkflowPolicyMiddleware._trace_context(snapshot),
                )
        prefix = getattr(request.system_message, "content", "")
        prompt = f"{prefix}\n\n{rendered}"
        return handler(request.override(system_message=SystemMessage(content=prompt)))
