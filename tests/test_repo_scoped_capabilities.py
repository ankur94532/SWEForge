import asyncio
from pathlib import Path
from types import SimpleNamespace

from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from sweforge.agent import _build_backend
from sweforge.capabilities import (
    MCPServerSpec,
    RepoCapabilityRegistry,
    load_repo_mcp_tools,
    repo_scope_interceptor,
)
from sweforge.context import RepoAgentContext
from sweforge.memory_learning import (
    MemoryLearningStatus,
    RepoMemoryCandidate,
    RepoMemoryEvidence,
    apply_memory_candidates,
)
from sweforge.repo_memory import (
    SQLiteMemoryStore,
    read_repo_memory,
    repo_memory_namespace,
    repo_skills_namespace,
)
from sweforge.skills import (
    list_repo_skills,
    put_repo_skill,
    show_repo_skill,
)


def test_context_is_immutable_and_namespace_is_runtime_derived(tmp_path: Path):
    context = RepoAgentContext(101, "owner/repo", "thread-a")
    memory = SQLiteMemoryStore(":memory:")
    backend = _build_backend(
        str(tmp_path),
        memory_store=memory.store,
        repo_context=context,
        skills_store=memory.store,
    )
    runtime = SimpleNamespace(context=context)
    assert backend.routes["/memories/"]._namespace(runtime) == repo_memory_namespace(
        101
    )
    assert backend.routes["/skills/"]._namespace(runtime) == repo_skills_namespace(101)
    try:
        context.repo_id = 202
    except Exception:
        pass
    assert context.repo_id == 101


def test_memory_and_skill_discovery_are_repo_scoped():
    memory = SQLiteMemoryStore(":memory:")
    put_repo_skill(memory.store, 101, "build/SKILL.md", "# A skill\nOnly A")
    put_repo_skill(memory.store, 202, "deploy/SKILL.md", "# B skill\nOnly B")
    assert list_repo_skills(memory.store, 101) == ["/build/SKILL.md"]
    assert show_repo_skill(memory.store, 101, "deploy/SKILL.md") is None
    assert list_repo_skills(memory.store, 101) != list_repo_skills(memory.store, 202)


def test_memory_learning_validates_provenance_and_deduplicates(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text("Run uv run pytest\n")
    excerpt = path.read_text().rstrip("\n")
    import hashlib

    candidate = RepoMemoryCandidate(
        candidate_id="build-1",
        category="BUILD",
        fact="Run uv run pytest",
        evidence=[
            RepoMemoryEvidence(
                path="README.md",
                start_line=1,
                end_line=1,
                content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                excerpt=excerpt,
            )
        ],
        durability_reason="The repository documents this as its canonical command.",
    )
    memory = SQLiteMemoryStore(":memory:")
    first = apply_memory_candidates(
        memory.store,
        repo_id=101,
        worktree=tmp_path,
        candidates=[candidate],
        lock_root=tmp_path / "locks",
    )
    second = apply_memory_candidates(
        memory.store,
        repo_id=101,
        worktree=tmp_path,
        candidates=[candidate],
        lock_root=tmp_path / "locks",
    )
    assert first.status is MemoryLearningStatus.UPDATED
    assert second.status is MemoryLearningStatus.NO_UPDATE
    assert "Run uv run pytest" in (
        read_repo_memory(memory.store, repo_memory_namespace(101)) or ""
    )


def test_memory_learning_rejects_other_repo_and_secret_evidence(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text("safe fact\n")
    candidate = RepoMemoryCandidate(
        candidate_id="bad",
        category="GOTCHA",
        fact="api_key=secret-value",
        evidence=[
            RepoMemoryEvidence(
                path="README.md",
                start_line=1,
                end_line=1,
                content_hash="0" * 64,
                excerpt="wrong",
            )
        ],
        durability_reason="not durable",
    )
    result = apply_memory_candidates(
        SQLiteMemoryStore(":memory:").store,
        repo_id=101,
        worktree=tmp_path,
        candidates=[candidate],
        lock_root=tmp_path / "locks",
    )
    assert result.status is MemoryLearningStatus.NO_UPDATE
    assert result.rejected_candidates == 1


def test_mcp_registry_filters_discovery_and_interceptor_rejects_cross_repo():
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec("repo-a", {"transport": "stdio", "command": "a"})
    )
    registry.register_server(
        MCPServerSpec("repo-b", {"transport": "stdio", "command": "b"})
    )
    registry.approve(101, "repo-a", {"read_a"})
    assert set(registry.approved_servers(101)) == {"repo-a"}
    assert set(registry.approved_servers(202)) == set()

    context = RepoAgentContext(101, "owner/repo", "thread-a")
    runtime = SimpleNamespace(context=context, tool_call_id="call-1")
    request = MCPToolCallRequest(
        name="read_b",
        args={"repo_id": 202, "repo_path": "/other"},
        server_name="repo-b",
        runtime=runtime,
    )

    async def handler(_request):
        raise AssertionError("unauthorized MCP handler was called")

    result = asyncio.run(repo_scope_interceptor(registry)(request, handler))
    assert "rejected" in result.content


def test_mcp_loader_uses_only_approved_connections(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, connections, **kwargs):
            calls["connections"] = connections
            calls["interceptors"] = kwargs["tool_interceptors"]

        async def get_tools(self):
            return [SimpleNamespace(name="read_a"), SimpleNamespace(name="read_b")]

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", FakeClient)
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec("repo-a", {"transport": "stdio", "command": "a"})
    )
    registry.approve(101, "repo-a", {"read_a"})
    tools, _ = asyncio.run(
        load_repo_mcp_tools(registry, RepoAgentContext(101, "owner/repo", "thread-a"))
    )
    assert [tool.name for tool in tools] == ["read_a"]
    assert set(calls["connections"]) == {"repo-a"}
    assert calls["interceptors"]
