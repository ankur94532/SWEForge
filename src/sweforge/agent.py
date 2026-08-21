"""Deep Agent construction and invocation."""

import os
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StateBackend


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
    input_state = {"messages": [{"role": "user", "content": task}]}
    if thread_id:
        result: dict[str, Any] = agent.invoke(
            input_state,
            config={"configurable": {"thread_id": thread_id}},
        )
    else:
        result = agent.invoke(input_state)
    messages = result.get("messages", [])
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    return str(content)
