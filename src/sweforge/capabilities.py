"""Trusted repo-scoped MCP capability registry and interceptor."""

from dataclasses import dataclass
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
        handle_tool_errors=False,
    )
    tools = await client.get_tools()
    allowed = registry._approved.get(context.repo_id, {})
    return [
        tool
        for tool in tools
        if any(tool.name == name for names in allowed.values() for name in names)
    ], client
