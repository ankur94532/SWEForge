"""Trusted repo-scoped MCP capability registry and interceptor."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from .context import RepoAgentContext


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """Operator-owned MCP connection; never loaded from a target repository."""

    server_id: str
    connection: dict[str, Any]


class RepoCapabilityRegistry:
    """In-process trusted allowlist for server IDs and tool names."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerSpec] = {}
        self._approved: dict[int, dict[str, frozenset[str]]] = {}

    def register_server(self, spec: MCPServerSpec) -> None:
        if not spec.server_id or not spec.connection:
            raise ValueError("MCP server configuration is incomplete")
        self._servers[spec.server_id] = spec

    def approve(self, repo_id: int, server_id: str, tool_names: set[str]) -> None:
        if server_id not in self._servers:
            raise ValueError("MCP server is not registered")
        if not tool_names or any(not name for name in tool_names):
            raise ValueError("approved MCP tools must be named")
        self._approved.setdefault(repo_id, {})[server_id] = frozenset(tool_names)

    def approved_servers(self, repo_id: int) -> dict[str, MCPServerSpec]:
        return {
            server_id: self._servers[server_id]
            for server_id in self._approved.get(repo_id, {})
        }

    def is_allowed(self, repo_id: int, server_id: str, tool_name: str) -> bool:
        return tool_name in self._approved.get(repo_id, {}).get(server_id, ())

    def approved_tools(self, repo_id: int, server_id: str) -> frozenset[str]:
        return self._approved.get(repo_id, {}).get(server_id, frozenset())


def load_capability_registry(path: str | Path) -> RepoCapabilityRegistry:
    """Load trusted operator config; target repositories never provide this file."""
    document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    registry = RepoCapabilityRegistry()
    for server_id, connection in document.get("servers", {}).items():
        if not isinstance(connection, dict):
            raise ValueError("MCP server connection must be an object")
        registry.register_server(MCPServerSpec(server_id, connection))
    for repo_id, servers in document.get("repositories", {}).items():
        for server_id, tool_names in servers.items():
            registry.approve(int(repo_id), server_id, set(tool_names))
    return registry


def repo_scope_interceptor(registry: RepoCapabilityRegistry):
    """Return an adapter interceptor that re-authorizes every MCP invocation."""

    async def authorize(request: MCPToolCallRequest, handler):
        runtime = request.runtime
        context = getattr(runtime, "context", None)
        if not isinstance(context, RepoAgentContext):
            return ToolMessage(
                content="MCP call rejected: authoritative repository context missing.",
                tool_call_id=getattr(runtime, "tool_call_id", "unknown"),
            )
        if not registry.is_allowed(context.repo_id, request.server_name, request.name):
            return ToolMessage(
                content=(
                    "MCP call rejected: capability is not approved for this repository."
                ),
                tool_call_id=getattr(runtime, "tool_call_id", "unknown"),
            )
        args = dict(request.args)
        for field in (
            "repo_id",
            "repo_full_name",
            "workspace_root",
            "repo_path",
            "tenant",
        ):
            args.pop(field, None)
        return await handler(request.override(args=args))

    return authorize


async def load_repo_mcp_tools(
    registry: RepoCapabilityRegistry,
    context: RepoAgentContext,
):
    """Load only approved server tools; callers still pass the interceptor."""
    client = MultiServerMCPClient(
        {
            key: spec.connection
            for key, spec in registry.approved_servers(context.repo_id).items()
        },
        tool_interceptors=[repo_scope_interceptor(registry)],
        tool_name_prefix=True,
        handle_tool_errors=False,
    )
    tools = []
    for server_id in registry.approved_servers(context.repo_id):
        server_tools = await client.get_tools(server_name=server_id)
        allowed = registry.approved_tools(context.repo_id, server_id)
        prefix = f"{server_id}_"
        tools.extend(
            tool
            for tool in server_tools
            if tool.name.startswith(prefix) and tool.name[len(prefix) :] in allowed
        )
    return tools, client
