"""Deep Agent construction and invocation."""

import os
from collections.abc import Mapping
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StateBackend
from langchain_core.messages import HumanMessage


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


def _build_backend(worktree: str) -> CompositeBackend:
    local = LocalShellBackend(
        root_dir=worktree,
        virtual_mode=True,
        env={"PATH": os.environ.get("PATH", "")},
        inherit_env=False,
    )
    return CompositeBackend(
        default=local,
        routes={"/sweforge_internal/": StateBackend()},
        artifacts_root="/sweforge_internal/",
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
) -> str:
    """Run one task using Deep Agents' native harness and return its final text."""
    if checkpointer is not None and not thread_id:
        raise ValueError("thread_id is required when a checkpointer is supplied")
    backend = _build_backend(worktree)
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
        checkpointer=checkpointer,
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
