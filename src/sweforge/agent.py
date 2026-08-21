"""Deep Agent construction and invocation."""

import hashlib
import os
from collections.abc import Callable, Mapping
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import (
    CompositeBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.store.base import BaseStore

from .repo_memory import MEMORY_VIRTUAL_PATH


def _live_message_id(event_key: str) -> str:
    return f"sweforge:event:{hashlib.sha256(event_key.encode()).hexdigest()}"


class LiveInputMiddleware(AgentMiddleware):
    """Inject durable actionable inputs before each model call.

    The provider returns persisted events and their stable IDs. The middleware
    deliberately does not acknowledge before the checkpointed message update;
    retries are therefore at-least-once physically and deduplicated logically
    by LangGraph message IDs.
    """

    def __init__(self, pending: Callable[[], list[tuple[str, str]]]) -> None:
        self.pending = pending

    def before_model(self, state, runtime):
        existing = {
            getattr(message, "id", None)
            for message in state.get("messages", [])
            if getattr(message, "id", None)
        }
        messages = [
            HumanMessage(content=body, id=_live_message_id(event_key))
            for event_key, body in self.pending()
            if _live_message_id(event_key) not in existing
        ]
        return {"messages": messages} if messages else None


def _normalize_response_text(message: Any) -> str:
    """Return user-facing text without serializing structured message content."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    # LangChain exposes normalized blocks through this stable accessor.  Fall
    # back to raw content for lightweight test doubles and other message types.
    blocks = getattr(message, "content_blocks", content)
    if not isinstance(blocks, (list, tuple)):
        return ""

    text_blocks: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_blocks.append(block["text"])
        elif getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_blocks.append(text)
    return "\n".join(text_blocks)


def _build_backend(
    worktree: str,
    *,
    memory_store: BaseStore | None = None,
    memory_namespace: tuple[str, ...] | None = None,
) -> CompositeBackend:
    local = LocalShellBackend(
        root_dir=worktree,
        virtual_mode=True,
        env={"PATH": os.environ.get("PATH", "")},
        inherit_env=False,
    )
    routes = {"/sweforge_internal/": StateBackend()}
    if (memory_store is None) != (memory_namespace is None):
        raise ValueError("memory_store and memory_namespace must be supplied together")
    if memory_store is not None and memory_namespace is not None:
        routes["/memories/"] = StoreBackend(
            namespace=lambda _runtime: memory_namespace,
            store=memory_store,
        )
    return CompositeBackend(
        default=local, routes=routes, artifacts_root="/sweforge_internal/"
    )


def run_task(
    *,
    model: str,
    worktree: str,
    task: str,
    thread_id: str | None = None,
    checkpointer: object | None = None,
    message_id: str | None = None,
    resume_if_present: bool = False,
    memory_store: BaseStore | None = None,
    memory_namespace: tuple[str, ...] | None = None,
    live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
) -> str:
    """Run one task using Deep Agents' native harness and return its final text."""
    if checkpointer is not None and not thread_id:
        raise ValueError("thread_id is required when a checkpointer is supplied")
    backend = _build_backend(
        worktree, memory_store=memory_store, memory_namespace=memory_namespace
    )
    memory = [MEMORY_VIRTUAL_PATH] if memory_store is not None else None
    permissions = (
        [
            FilesystemPermission(
                operations=["write"], paths=["/memories/**"], mode="deny"
            )
        ]
        if memory_store is not None
        else None
    )
    middleware = (
        [LiveInputMiddleware(live_input_provider)]
        if live_input_provider is not None
        else []
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=(
            "Work only within the provided repository worktree. Inspect the code, "
            "make the requested changes, and run relevant tests or validation. "
            "Filesystem tool paths are virtual paths rooted at the repository. "
            "Shell commands already execute with the repository root as their "
            "working directory, so use relative repository paths in shell commands "
            "rather than virtual absolute paths. "
            "Summarize what you changed and any validation results."
        ),
        memory=memory,
        permissions=permissions,
        store=memory_store,
        checkpointer=checkpointer,
        middleware=middleware,
    )
    input_state: dict[str, Any] | None = {
        "messages": [{"role": "user", "content": task}]
    }
    if thread_id:
        config = {"configurable": {"thread_id": thread_id}}
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
            result: dict[str, Any] = agent.invoke(
                input_state, config=config, durability="sync"
            )
        else:
            result = agent.invoke(input_state, config=config)
    else:
        result = agent.invoke(input_state)
    messages = result.get("messages", [])
    if not messages:
        return ""
    return _normalize_response_text(messages[-1])
