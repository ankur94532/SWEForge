"""Trusted repo-scoped MCP capability registry and interceptor."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from .agent_trace import AgentTracer, TraceContext
from .context import RepoAgentContext
from .repo_secrets import RepoSecretStore, SecretValue, redact_secret_values

REMOTE_MCP_TRANSPORTS = frozenset({"http", "streamable_http", "streamable-http", "sse"})


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """Operator-owned MCP connection; never loaded from a target repository."""

    server_id: str
    connection: dict[str, Any]
    secret_env: dict[str, str] | None = None
    secret_headers: dict[str, str] | None = None


def _no_redirect_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Keep credential-bearing MCP headers on their configured origin."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
    )


def _trace_context(context: RepoAgentContext) -> TraceContext:
    return TraceContext(thread_id=context.thread_id, repo=context.repo_full_name)


def _resolve_mcp_secrets(
    *,
    key: str,
    references: dict[str, str] | None,
    context: RepoAgentContext,
    secret_store: RepoSecretStore | None,
    tracer: AgentTracer | None,
) -> dict[str, SecretValue]:
    if not references:
        return {}
    try:
        if secret_store is None:
            raise PermissionError("required repository credential store is unavailable")
        return secret_store.resolve_env(
            context.repo_id,
            references,
            subject=f"mcp:{key}",
        )
    except PermissionError:
        if tracer is not None:
            tracer.emit(
                "SECRET RESOLUTION",
                (
                    f"server={key} required={len(references)} "
                    f"resolved=0 required_missing={len(references)}"
                ),
                _trace_context(context),
            )
        raise


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

    def known_tool_names(self) -> frozenset[str]:
        """Names a trusted workflow specification may reference."""
        return frozenset(
            f"{server_id}_{tool_name}"
            for servers in self._approved.values()
            for server_id, tool_names in servers.items()
            for tool_name in tool_names
        )


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
        args["repo_id"] = context.repo_id
        args["repo_full_name"] = context.repo_full_name
        for field in ("workspace_root", "repo_path", "tenant"):
            args.pop(field, None)
        return await handler(request.override(args=args))

    return authorize


async def load_repo_mcp_tools(
    registry: RepoCapabilityRegistry,
    context: RepoAgentContext,
    *,
    secret_store: RepoSecretStore | None = None,
    tracer: AgentTracer | None = None,
):
    """Load only approved server tools; callers still pass the interceptor."""
    connections: dict[str, dict[str, Any]] = {}
    secret_values: list[str] = []
    for key, spec in registry.approved_servers(context.repo_id).items():
        connection = dict(spec.connection)
        references: dict[str, str] | None = None
        if connection.get("transport") == "stdio":
            fixed_env = dict(connection.get("env", {}))
            references = spec.secret_env
            resolved = _resolve_mcp_secrets(
                key=key,
                references=references,
                context=context,
                secret_store=secret_store,
                tracer=tracer,
            )
            connection["env"] = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                **fixed_env,
                **{name: value.reveal() for name, value in resolved.items()},
            }
        elif connection.get("transport") in REMOTE_MCP_TRANSPORTS:
            references = spec.secret_headers
            resolved = _resolve_mcp_secrets(
                key=key,
                references=references,
                context=context,
                secret_store=secret_store,
                tracer=tracer,
            )
            connection["headers"] = {
                **dict(connection.get("headers", {})),
                **{name: value.reveal() for name, value in resolved.items()},
            }
            if resolved:
                connection["httpx_client_factory"] = _no_redirect_http_client
        else:
            resolved = {}
        secret_values.extend(value.reveal() for value in resolved.values())
        if tracer is not None:
            tracer.emit(
                "MCP SERVER START",
                (
                    f"server={key} transport={connection.get('transport')} "
                    f"required={len(references or {})} resolved={len(resolved)}"
                ),
                _trace_context(context),
            )
        connections[key] = connection
    try:
        client = MultiServerMCPClient(
            connections,
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
    except Exception as exc:
        safe = redact_secret_values(exc, secret_values)
        raise RuntimeError(f"repository MCP client failed: {safe}") from None
    return tools, client
