"""Deep Agent construction and invocation."""

import asyncio
import hashlib
import os
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from deepagents import create_deep_agent
from deepagents._models import resolve_model
from deepagents.backends import (
    CompositeBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import SandboxBackendProtocol
from deepagents.graph import DeepAgentState
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.store.base import BaseStore
from langgraph.types import Command, interrupt

from .agent_trace import AgentTracer, observable_message_text
from .capabilities import RepoCapabilityRegistry, load_repo_mcp_tools
from .context import RepoAgentContext
from .execution_evidence import RecordingSandboxBackend
from .execution_security import (
    SandboxBackendProvider,
    require_secure_backend,
)
from .repo_memory import (
    MEMORY_VIRTUAL_PATH,
    repo_memory_namespace,
    repo_skills_namespace,
)
from .skills import SKILLS_VIRTUAL_PATH
from .workflow_middleware import (
    RESEARCH_TOOLS,
    DelegatedWorkflowPolicyMiddleware,
    ReadOnlyInvestigatorMiddleware,
    WorkflowAuthority,
    WorkflowPolicyMiddleware,
    WorkflowSkillsMiddleware,
)
from .workflow_runtime import TaskPhase


def build_workflow_agent(
    *,
    model: str | BaseChatModel,
    tools: list[Any],
    backend: CompositeBackend,
    system_prompt: str,
    memory: list[str] | None = None,
    skills: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    store: BaseStore | None = None,
    context_schema: type | None = None,
    checkpointer: object | None = None,
    middleware: list[AgentMiddleware] | None = None,
    response_format: Any = None,
    subagents: list[dict[str, Any]] | None = None,
):
    """Canonical constructor for the one workflow-owning Deep Agent.

    Callers must pass an explicit bounded ``general-purpose`` worker. Its name
    overrides Deep Agents' automatically added unrestricted worker, so ``task``
    cannot become an authority expansion.
    """
    if not subagents or not any(
        item.get("name") == "general-purpose" for item in subagents
    ):
        raise ValueError(
            "workflow agent requires an explicit bounded general-purpose subagent"
        )
    return create_deep_agent(
        model=model,
        tools=tools,
        subagents=subagents,
        backend=backend,
        system_prompt=system_prompt,
        memory=memory,
        skills=skills,
        permissions=permissions,
        store=store,
        context_schema=context_schema,
        checkpointer=checkpointer,
        middleware=middleware or [],
        response_format=response_format,
        name="sweforge-workflow-root",
    )


def build_durable_workflow_agent(
    *,
    planning_model: str | BaseChatModel,
    execution_model: str | BaseChatModel,
    validation_model: str | BaseChatModel,
    backend: CompositeBackend,
    authority: WorkflowAuthority,
    lifecycle_tools: list[Any],
    capability_tools: list[Any] | None = None,
    read_skill: Callable[[int, str], str],
    checkpointer: object,
    store: BaseStore | None = None,
    context_schema: type | None = None,
    memory: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    middleware: list[AgentMiddleware] | None = None,
    tracer: AgentTracer | None = None,
):
    """Build the one durable root agent with a bounded investigator below it."""
    extra_tools = capability_tools or []
    research = [
        tool for tool in extra_tools if getattr(tool, "name", "") in RESEARCH_TOOLS
    ]
    delegated_policy = DelegatedWorkflowPolicyMiddleware(authority, tracer=tracer)
    delegated_skills = WorkflowSkillsMiddleware(authority, read_skill, tracer=tracer)
    subagents = [
        {
            # This explicit bounded override prevents Deep Agents from adding
            # its unrestricted default general-purpose worker.
            "name": "general-purpose",
            "description": (
                "Read-only repository investigator for the current active task "
                "and phase. Returns a concise evidence report."
            ),
            "system_prompt": (
                "Investigate only. Lifecycle changes, delegation, writes and "
                "command execution are structurally unavailable."
            ),
            "tools": research,
            "middleware": [delegated_policy, delegated_skills],
        }
    ]
    policy = WorkflowPolicyMiddleware(
        authority,
        phase_models={
            TaskPhase.PLANNING: planning_model,
            TaskPhase.EXECUTING: execution_model,
            TaskPhase.VALIDATING: validation_model,
        },
        tracer=tracer,
    )
    return build_workflow_agent(
        model=planning_model,
        tools=[*extra_tools, *lifecycle_tools],
        backend=backend,
        system_prompt=(
            "You are the sole SWEForge workflow-owning root agent. Reason freely "
            "within the authoritative current phase, and use its lifecycle gateway "
            "when complete. Never infer or perform a workflow transition yourself."
        ),
        memory=memory,
        # Dynamic skill middleware eagerly discloses one exact phase skill or a
        # bounded catalog. Passing the broad /skills source here would advertise
        # inactive operational authority.
        skills=None,
        permissions=permissions,
        store=store,
        context_schema=context_schema,
        checkpointer=checkpointer,
        middleware=[
            policy,
            WorkflowSkillsMiddleware(authority, read_skill, tracer=tracer),
            *(middleware or []),
        ],
        subagents=subagents,
    )


def _live_message_id(event_key: str) -> str:
    return f"sweforge:event:{hashlib.sha256(event_key.encode()).hexdigest()}"


class LiveInputMiddleware(AgentMiddleware):
    """Inject durable actionable inputs before each model call.

    The provider returns persisted events and their stable IDs. The middleware
    deliberately does not acknowledge before the checkpointed message update;
    retries are therefore at-least-once physically and deduplicated logically
    by LangGraph message IDs.
    """

    def __init__(
        self,
        pending: Callable[[], list[tuple[str, str]]],
        delivered_event_keys: set[str] | None = None,
    ) -> None:
        self.pending = pending
        self.delivered_event_keys = delivered_event_keys

    def before_model(self, state, runtime):
        existing = {
            getattr(message, "id", None)
            for message in state.get("messages", [])
            if getattr(message, "id", None)
        }
        messages = []
        for event_key, body in self.pending():
            if (
                self.delivered_event_keys is not None
                and event_key in self.delivered_event_keys
            ):
                continue
            message_id = _live_message_id(event_key)
            if message_id in existing:
                if self.delivered_event_keys is not None:
                    self.delivered_event_keys.add(event_key)
                continue
            messages.append(HumanMessage(content=body, id=message_id))
            if self.delivered_event_keys is not None:
                self.delivered_event_keys.add(event_key)
        return {"messages": messages} if messages else None


def _normalize_response_text(message: Any) -> str:
    """Return user-facing text without serializing structured message content."""
    return observable_message_text(message)


def pending_interrupt_values(agent, config) -> tuple[dict[str, Any], ...]:
    """Read the checkpoint's live pending interrupt payloads, if any.

    This is the only structural link between a persisted clarification and the
    interrupt that is actually waiting, so resume selection can be driven by
    occurrence identity rather than by ordering heuristics.
    """
    reader = getattr(agent, "get_state", None)
    if not callable(reader):
        return ()
    snapshot = reader(config)
    values: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, Mapping):
                values.append(dict(value))
    return tuple(values)


def _invoke_agent(agent, state, *, config=None, durability=None, context=None):
    kwargs = {"config": config} if config is not None else {}
    if durability is not None:
        kwargs["durability"] = durability
    if context is not None:
        kwargs["context"] = context
    try:
        return agent.invoke(state, **kwargs)
    except TypeError as exc:
        # Keep small offline doubles written for older LangGraph APIs usable;
        # real LangGraph agents receive sync durability above.
        if durability is None or "durability" not in str(exc):
            raise
        kwargs.pop("durability", None)
        return agent.invoke(state, **kwargs)


def _build_backend(
    worktree: str,
    *,
    memory_store: BaseStore | None = None,
    repo_context: RepoAgentContext | None = None,
    memory_namespace: tuple[str, ...] | None = None,
    skills_store: BaseStore | None = None,
    sandbox_backend: SandboxBackendProtocol | None = None,
    execution_evidence_sink: Callable[..., Any] | None = None,
) -> CompositeBackend:
    local = sandbox_backend or LocalShellBackend(
        root_dir=worktree,
        virtual_mode=True,
        env={"PATH": os.environ.get("PATH", "")},
        inherit_env=False,
    )
    if execution_evidence_sink is not None:
        local = RecordingSandboxBackend(local, execution_evidence_sink)
    routes = {"/sweforge_internal/": StateBackend()}
    if repo_context is None and (memory_store is None) != (memory_namespace is None):
        raise ValueError("memory_store and memory_namespace must be supplied together")
    if repo_context is not None and memory_store is not None:
        routes["/memories/"] = StoreBackend(
            namespace=lambda runtime: repo_memory_namespace(runtime.context.repo_id),
            store=memory_store,
        )
    elif memory_store is not None and memory_namespace is not None:
        # Compatibility for the standalone V0 CLI; GitHub workflows always
        # provide RepoAgentContext and cannot select a namespace themselves.
        routes["/memories/"] = StoreBackend(
            namespace=lambda _runtime: memory_namespace,
            store=memory_store,
        )
    if repo_context is not None and skills_store is not None:
        routes["/skills/"] = StoreBackend(
            namespace=lambda runtime: repo_skills_namespace(runtime.context.repo_id),
            store=skills_store,
        )
    return CompositeBackend(
        default=local, routes=routes, artifacts_root="/sweforge_internal/"
    )


def _create_repair_agent(
    *,
    model: str | BaseChatModel,
    tools: list[Any],
    backend: CompositeBackend,
    system_prompt: str,
    memory: list[str] | None,
    skills: list[str] | None,
    permissions: list[FilesystemPermission] | None,
    store: BaseStore | None,
    context_schema: type | None,
    checkpointer: object | None,
    middleware: list[AgentMiddleware],
):
    """Build the repair harness without synchronous subagent middleware.

    Deep Agents 0.7.8 auto-adds its general-purpose subagent when
    ``create_deep_agent(..., subagents=[])`` is used. Repair therefore assembles
    the same core filesystem, skills, summarization, memory and custom
    middleware directly on LangChain's agent builder. With no
    ``SubAgentMiddleware``, the compiled graph has no ``task`` tool or
    synchronous subagent dispatch path.
    """
    resolved_model = resolve_model(model)
    repair_middleware: list[AgentMiddleware] = []
    if skills is not None:
        repair_middleware.append(SkillsMiddleware(backend=backend, sources=skills))
    repair_middleware.extend(
        [
            FilesystemMiddleware(backend=backend, _permissions=permissions),
            create_summarization_middleware(resolved_model, backend),
            PatchToolCallsMiddleware(),
            *middleware,
        ]
    )
    if memory is not None:
        repair_middleware.append(
            MemoryMiddleware(
                backend=backend,
                sources=memory,
                add_cache_control=True,
            )
        )
    return create_agent(
        resolved_model,
        system_prompt=system_prompt,
        tools=tools,
        middleware=repair_middleware,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        state_schema=DeepAgentState,
    ).with_config(
        {
            "recursion_limit": 9_999,
            "metadata": {"ls_integration": "sweforge-repair"},
        }
    )


def run_task(
    *,
    model: str | BaseChatModel,
    worktree: str,
    task: str,
    thread_id: str | None = None,
    checkpointer: object | None = None,
    message_id: str | None = None,
    resume_if_present: bool = False,
    memory_store: BaseStore | None = None,
    repo_context: RepoAgentContext | None = None,
    memory_namespace: tuple[str, ...] | None = None,
    skills_store: BaseStore | None = None,
    capability_registry: RepoCapabilityRegistry | None = None,
    sandbox_backend_provider: SandboxBackendProvider | None = None,
    secure_execution: bool = False,
    unsafe_local_shell: bool = False,
    live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
    live_delivered_event_keys: set[str] | None = None,
    clarification_request_sink: Callable[[dict[str, Any]], None] | None = None,
    interrupt_result_sink: Callable[[dict[str, Any]], None] | None = None,
    resume_value: Any | None = None,
    resume_resolver: Callable[[tuple[dict[str, Any], ...]], Any] | None = None,
    repo_memory_proposal_sink: Callable[..., str] | None = None,
    issue_memory_search: Callable[[str, int], str] | None = None,
    execution_evidence_sink: Callable[..., Any] | None = None,
    repair_mode: bool = False,
) -> str:
    """Run one task using Deep Agents' native harness and return its final text."""
    if checkpointer is not None and not thread_id:
        raise ValueError("thread_id is required when a checkpointer is supplied")
    if memory_store is not None and repo_context is None and memory_namespace is None:
        raise ValueError("repository memory requires authoritative context")
    if repo_context is not None and memory_namespace is not None:
        raise ValueError("callers cannot override the context-derived memory namespace")
    if repo_context is not None and not secure_execution and not unsafe_local_shell:
        raise ValueError(
            "repository-scoped execution must select strict sandbox or explicit "
            "unsafe local-shell mode"
        )
    if repo_context is not None and secure_execution:
        isolated_backend = require_secure_backend(
            context=repo_context,
            worktree=worktree,
            provider=sandbox_backend_provider,
            unsafe_local_shell=unsafe_local_shell,
        )
    else:
        isolated_backend = None
    if interrupt_result_sink is None:
        interrupt_result_sink = clarification_request_sink
    effective_skills_store = skills_store or memory_store
    backend = _build_backend(
        worktree,
        memory_store=memory_store,
        repo_context=repo_context,
        memory_namespace=memory_namespace,
        skills_store=effective_skills_store,
        sandbox_backend=isolated_backend,
        execution_evidence_sink=execution_evidence_sink,
    )
    memory = [MEMORY_VIRTUAL_PATH] if memory_store is not None else None
    permissions = (
        [
            FilesystemPermission(
                operations=["write"], paths=["/memories/**"], mode="deny"
            ),
            FilesystemPermission(
                operations=["write"], paths=["/skills/**"], mode="deny"
            ),
        ]
        if memory_store is not None or skills_store is not None
        else None
    )
    middleware = (
        [LiveInputMiddleware(live_input_provider, live_delivered_event_keys)]
        if live_input_provider is not None
        else []
    )
    mcp_tools = []
    clarification_tools = []
    if interrupt_result_sink is not None:

        @tool
        def request_clarification(
            question: str,
            reason: str,
            answer_type: str = "TEXT",
            choices: list[str] | None = None,
            tool_call_id: Annotated[str, InjectedToolCallId] = "",
        ) -> str:
            """Request specific missing information before safely continuing."""
            normalized_type = answer_type.upper()
            if normalized_type not in {"CHOICE", "BOOLEAN", "TEXT", "VALUE"}:
                raise ValueError("answer_type must be CHOICE, BOOLEAN, TEXT, or VALUE")
            normalized_choices = tuple(str(item) for item in (choices or ()))
            if normalized_type == "CHOICE" and not normalized_choices:
                raise ValueError("CHOICE clarification requires choices")
            answer = interrupt(
                {
                    "question": question[:2_000],
                    "reason": reason[:2_000],
                    "answer_type": normalized_type,
                    "choices": normalized_choices,
                    "occurrence_key": tool_call_id or message_id or "clarification",
                }
            )
            return f"Clarification answer received: {answer}"

        clarification_tools.append(request_clarification)
    memory_tools = []
    if repo_memory_proposal_sink is not None:

        @tool
        def propose_repo_memory(
            category: str,
            fact: str,
            durability_reason: str,
            path: str,
            start_line: int,
            end_line: int,
        ) -> str:
            """Nominate durable repository knowledge for later validation.

            Use this for facts a future task in THIS repository would need --
            build/test commands, conventions, architectural boundaries,
            configuration locations, stable dependency relationships -- even
            when you found them in files this task did not change.

            This does not write repository memory. Point at the lines that
            prove the fact; SWEForge reads them itself, verifies them after
            publication and decides whether to record the fact.
            """
            return repo_memory_proposal_sink(
                category=category,
                fact=fact,
                durability_reason=durability_reason,
                path=path,
                start_line=int(start_line),
                end_line=int(end_line),
            )

        memory_tools.append(propose_repo_memory)
    research_tools = []
    if issue_memory_search is not None:

        @tool
        def search_issue_memory(query: str, limit: int = 3) -> str:
            """Search past resolved issues in THIS repository for similar cases.

            Returns concise historical records: symptom, root cause, fix and
            affected components. They are clues from when they were fixed, not
            current repository truth -- verify against the code before relying
            on one.
            """
            return issue_memory_search(query, int(limit))

        research_tools.append(search_issue_memory)
    if capability_registry is not None:
        if repo_context is None:
            raise ValueError("MCP capabilities require authoritative context")
        mcp_tools, _ = asyncio.run(
            load_repo_mcp_tools(capability_registry, repo_context)
        )
    # INITIAL keeps a read-only research subagent. Repair uses a dedicated
    # no-subagent assembly below because Deep Agents 0.7.8 auto-adds its default
    # general-purpose subagent when an empty list is passed.
    subagents = [
        {
            "name": "general-purpose",
            "description": (
                "Read-only investigator for researching questions, searching "
                "code and gathering evidence. Returns a written report; it "
                "cannot propose repository memory or ask the human anything."
            ),
            "system_prompt": (
                "Investigate the request within this repository worktree and "
                "report concise, concrete findings with file paths and line "
                "numbers. Do not modify application state."
            ),
            "tools": [*mcp_tools, *research_tools],
            "middleware": [ReadOnlyInvestigatorMiddleware()],
        }
    ]
    system_prompt = (
        "Work only within the provided repository worktree. Inspect the code, "
        "make the requested changes, and run relevant tests or validation. "
        "Filesystem tool paths are virtual paths rooted at the repository. "
        "Shell commands already execute with the repository root as their "
        "working directory, so use relative repository paths in shell commands "
        "rather than virtual absolute paths. "
        "Summarize what you changed and any validation results."
        + (
            " This is a repair execution: perform the authorized repair directly; "
            "do not delegate work to another agent."
            if repair_mode
            else ""
        )
    )
    agent_tools = [
        *mcp_tools,
        *clarification_tools,
        *memory_tools,
        *research_tools,
    ]
    skills = (
        [SKILLS_VIRTUAL_PATH]
        if effective_skills_store is not None and repo_context is not None
        else None
    )
    agent = (
        _create_repair_agent(
            model=model,
            tools=agent_tools,
            backend=backend,
            system_prompt=system_prompt,
            memory=memory,
            skills=skills,
            permissions=permissions,
            store=memory_store,
            context_schema=RepoAgentContext if repo_context is not None else None,
            checkpointer=checkpointer,
            middleware=middleware,
        )
        if repair_mode
        else build_workflow_agent(
            model=model,
            tools=agent_tools,
            subagents=subagents,
            backend=backend,
            system_prompt=system_prompt,
            memory=memory,
            skills=skills,
            permissions=permissions,
            store=memory_store,
            context_schema=RepoAgentContext if repo_context is not None else None,
            checkpointer=checkpointer,
            middleware=middleware,
        )
    )
    input_state: dict[str, Any] | None = {
        "messages": [{"role": "user", "content": task}]
    }
    if thread_id:
        config = {"configurable": {"thread_id": thread_id}}
        if resume_resolver is not None and checkpointer is not None:
            # Resolve the answer against the interrupt that is actually pending
            # so one occurrence can never consume another's answer.
            pending = pending_interrupt_values(agent, config)
            resume_value = resume_resolver(pending) if pending else None
            if pending and resume_value is None:
                # Fail closed without touching the checkpoint.  Invoking a
                # graph that has a pending interrupt does NOT re-raise it:
                # LangGraph reuses the previous Command(resume=...) value, so
                # running here would hand this interrupt a stale answer.
                if interrupt_result_sink is not None:
                    for payload in pending:
                        interrupt_result_sink(dict(payload))
                return ""
        if resume_value is not None:
            result: dict[str, Any] = _invoke_agent(
                agent,
                Command(resume=resume_value),
                config=config,
                durability="sync",
                context=repo_context,
            )
            if interrupt_result_sink is not None:
                for item in result.get("__interrupt__", ()):
                    if isinstance(getattr(item, "value", None), Mapping):
                        interrupt_result_sink(dict(item.value))
            messages = result.get("messages", [])
            return _normalize_response_text(messages[-1]) if messages else ""
        if message_id:
            snapshot = agent.get_state(config)
            has_message = any(
                getattr(message, "id", None) == message_id
                for message in snapshot.values.get("messages", [])
            )
            if has_message and resume_if_present:
                input_state = None
            else:
                input_state = {
                    "messages": [
                        HumanMessage(content=task, id=message_id),
                    ]
                }
        if message_id:
            result: dict[str, Any] = _invoke_agent(
                agent,
                input_state,
                config=config,
                durability="sync",
                context=repo_context,
            )
        else:
            result = _invoke_agent(
                agent,
                input_state,
                config=config,
                durability="sync",
                context=repo_context,
            )
    else:
        result = _invoke_agent(agent, input_state, context=repo_context)
    messages = result.get("messages", [])
    if interrupt_result_sink is not None:
        for item in result.get("__interrupt__", ()):
            if isinstance(getattr(item, "value", None), Mapping):
                interrupt_result_sink(dict(item.value))
    if not messages:
        return ""
    return _normalize_response_text(messages[-1])
