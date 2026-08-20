"""Deep Agent construction and invocation."""

from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend


def run_task(*, model: str, worktree: str, task: str) -> str:
    """Run one task using Deep Agents' native harness and return its final text."""
    agent = create_deep_agent(
        model=model,
        backend=LocalShellBackend(
            root_dir=worktree, virtual_mode=True, inherit_env=False
        ),
        system_prompt=(
            "Work only within the provided repository worktree. Inspect the code, "
            "make the requested changes, and run relevant tests or validation. "
            "Summarize what you changed and any validation results."
        ),
    )
    result: dict[str, Any] = agent.invoke(
        {"messages": [{"role": "user", "content": task}]}
    )
    messages = result.get("messages", [])
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    return str(content)
