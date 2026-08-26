"""S29 and S30: the two security fail-closed paths.

Both are refusals, and a refusal that silently becomes a permission is the
worst failure this system can have. S29 proves strict execution will not run
at all without a configured sandbox. S30 proves the MCP interceptor
re-authorizes every call against the repository allowlist -- distinct from S9,
which proves identity cannot be spoofed; this proves an unapproved capability
cannot be reached even with honest identity.
"""

import asyncio

import pytest
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.agent import RepoAgentContext
from sweforge.capabilities import (
    MCPServerSpec,
    RepoCapabilityRegistry,
    repo_scope_interceptor,
)
from sweforge.execution_security import (
    SecureExecutionUnavailable,
    require_secure_backend,
    resolve_sandbox_provider,
)

CONTEXT = RepoAgentContext(repo_id=1, repo_full_name="example/repo", thread_id="t1")


class _Request:
    """The shape repo_scope_interceptor consumes."""

    def __init__(self, server_name, name, args, context=CONTEXT):
        self.server_name = server_name
        self.name = name
        self.args = args
        self.runtime = type("R", (), {"context": context, "tool_call_id": "c1"})()

    def override(self, *, args):
        return _Request(self.server_name, self.name, args, self.runtime.context)


async def _call(registry, request, handler):
    return await repo_scope_interceptor(registry)(request, handler)


def _registry():
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec(server_id="docs", connection={"url": "http://localhost"})
    )
    registry.approve(1, "docs", {"search"})
    return registry


@scenario(
    "S29",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="Strict execution refuses to run without a sandbox backend.",
)
def s29_no_sandbox_provider(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))

        # No provider and no explicit unsafe opt-in: execution must refuse.
        with pytest.raises(SecureExecutionUnavailable, match="sandbox backend"):
            require_secure_backend(
                context=CONTEXT,
                worktree=str(world.root),
                provider=None,
                unsafe_local_shell=False,
            )

        # An unknown provider name refuses rather than falling back.
        with pytest.raises(SecureExecutionUnavailable, match="unknown sandbox"):
            resolve_sandbox_provider("no-such-provider")

        # The unsafe path is reachable only by asking for it explicitly.
        assert (
            require_secure_backend(
                context=CONTEXT,
                worktree=str(world.root),
                provider=None,
                unsafe_local_shell=True,
            )
            is None
        ), "the explicit development opt-in did not disable the sandbox"
    return world.observation()


@scenario(
    "S30",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="An unapproved MCP capability is rejected for this repository.",
)
def s30_unapproved_mcp_tool(root_dir) -> Observation:
    world = World.build(root_dir)
    registry = _registry()
    reached: list[str] = []

    async def handler(request):
        reached.append(request.name)
        return "ok"

    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))

        # Approved tool on the approved server, for this repository.
        allowed = asyncio.run(_call(registry, _Request("docs", "search", {}), handler))
        assert reached == ["search"], "an approved capability was rejected"

        # Unapproved tool on an approved server.
        rejected = asyncio.run(
            _call(registry, _Request("docs", "delete_everything", {}), handler)
        )
        assert reached == ["search"], "an unapproved tool reached the handler"
        assert "not approved" in rejected.content

        # Approved tool, but a repository that approved nothing.
        other = RepoAgentContext(
            repo_id=2, repo_full_name="example/other", thread_id="t"
        )
        cross = asyncio.run(
            _call(registry, _Request("docs", "search", {}, other), handler)
        )
        assert reached == ["search"], "a capability leaked across repositories"
        assert "not approved" in cross.content

        # Missing authoritative context is refused, not defaulted.
        missing = asyncio.run(
            _call(registry, _Request("docs", "search", {}, None), handler)
        )
        assert reached == ["search"], "a call without repository context was served"
        assert "context missing" in missing.content
        del allowed
    return world.observation()


def test_model_supplied_identity_is_stripped_before_the_handler():
    """Positive control on the rewrite: the interceptor must overwrite
    identity rather than trust what the caller supplied."""
    registry = _registry()
    seen: dict = {}

    async def handler(request):
        seen.update(request.args)
        return "ok"

    asyncio.run(
        _call(
            registry,
            _Request(
                "docs",
                "search",
                {"repo_id": 99, "workspace_root": "/etc", "tenant": "other"},
            ),
            handler,
        )
    )
    assert seen["repo_id"] == 1, "the model's repo_id survived"
    assert seen["repo_full_name"] == "example/repo"
    for field in ("workspace_root", "repo_path", "tenant"):
        assert field not in seen, f"{field} was not stripped"


@pytest.mark.parametrize("scenario_id", ["S29", "S30"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
