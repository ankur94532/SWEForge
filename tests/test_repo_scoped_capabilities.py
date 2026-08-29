import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from sweforge.agent import _build_backend, run_task
from sweforge.capabilities import (
    MCPServerSpec,
    RepoCapabilityRegistry,
    load_repo_mcp_tools,
    repo_scope_interceptor,
)
from sweforge.context import RepoAgentContext
from sweforge.execution_security import (
    SecureExecutionUnavailable,
    require_secure_backend,
)
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    RepoMemoryLearningRecord,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
    WorkflowPhase,
    WorkflowStateRecord,
    memory_learning_id_for,
)
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
from sweforge.workflow import WorkflowEngine


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


def test_skill_discovery_is_not_limited_to_store_default_page_size():
    memory = SQLiteMemoryStore(":memory:")
    expected = [f"/skill-{index:02d}/SKILL.md" for index in range(12)]
    for relative_path in expected:
        put_repo_skill(memory.store, 101, relative_path, f"# {relative_path}\n")

    assert list_repo_skills(memory.store, 101) == expected


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

        async def get_tools(self, *, server_name=None):
            assert server_name == "repo-a"
            return [
                SimpleNamespace(name="repo-a_read_a"),
                SimpleNamespace(name="repo-a_read_b"),
            ]

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", FakeClient)
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec("repo-a", {"transport": "stdio", "command": "a"})
    )
    registry.approve(101, "repo-a", {"read_a"})
    tools, _ = asyncio.run(
        load_repo_mcp_tools(registry, RepoAgentContext(101, "owner/repo", "thread-a"))
    )
    assert [tool.name for tool in tools] == ["repo-a_read_a"]
    assert set(calls["connections"]) == {"repo-a"}
    assert calls["interceptors"]


def test_mcp_same_named_tools_are_server_prefixed_and_pair_filtered(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, connections, **kwargs):
            self.connections = connections
            self.kwargs = kwargs

        async def get_tools(self, *, server_name=None):
            calls.append(server_name)
            return [
                SimpleNamespace(name=f"{server_name}_search"),
                SimpleNamespace(name=f"{server_name}_deploy"),
            ]

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", FakeClient)
    registry = RepoCapabilityRegistry()
    registry.register_server(MCPServerSpec("a", {"transport": "stdio", "command": "a"}))
    registry.register_server(MCPServerSpec("b", {"transport": "stdio", "command": "b"}))
    registry.approve(101, "a", {"search"})
    registry.approve(101, "b", {"deploy"})
    tools, _ = asyncio.run(
        load_repo_mcp_tools(registry, RepoAgentContext(101, "owner/repo", "thread-a"))
    )
    assert calls == ["a", "b"]
    assert [tool.name for tool in tools] == ["a_search", "b_deploy"]


def test_real_stdio_mcp_adapter_reaches_interceptor_and_overwrites_authority(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "m = FastMCP('fixture')\n"
        "@m.tool()\n"
        "def read_repo(repo_id: int = 0, repo_full_name: str = '', "
        "repo_path: str = '') -> str:\n"
        "    return f'{repo_id}:{repo_full_name}:{repo_path}'\n"
        "m.run()\n"
    )
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec(
            "fixture",
            {"transport": "stdio", "command": sys.executable, "args": [str(server)]},
        )
    )
    registry.approve(101, "fixture", {"read_repo"})
    context = RepoAgentContext(101, "owner/repo", "thread-a")

    async def invoke():
        tools, _client = await load_repo_mcp_tools(registry, context)
        runtime = SimpleNamespace(context=context, tool_call_id="mcp-1")
        return await tools[0].coroutine(
            runtime=runtime, repo_id=202, repo_path="/other/repo"
        )

    result = asyncio.run(invoke())
    assert "101:owner/repo:" in str(result)
    assert "/other/repo" not in str(result)


def test_strict_repo_execution_fails_closed_without_sandbox(tmp_path):
    context = RepoAgentContext(101, "owner/repo", "thread-a")
    with pytest.raises((SecureExecutionUnavailable, ValueError), match="sandbox"):
        run_task(
            model="provider:model",
            worktree=str(tmp_path),
            task="inspect",
            repo_context=context,
            secure_execution=True,
        )


def test_configured_sandbox_provider_is_wired_and_shell_capable(tmp_path):
    context = RepoAgentContext(101, "owner/repo", "thread-a")
    backend = SimpleNamespace(execute=lambda *_args, **_kwargs: None)

    def provider(*, context, worktree):
        assert context.repo_id == 101
        assert worktree == str(tmp_path)
        return backend

    assert (
        require_secure_backend(
            context=context,
            worktree=str(tmp_path),
            provider=provider,
            unsafe_local_shell=False,
        )
        is backend
    )
    with pytest.raises(SecureExecutionUnavailable):
        require_secure_backend(
            context=context,
            worktree=str(tmp_path),
            provider=lambda **_: SimpleNamespace(),
            unsafe_local_shell=False,
        )


def test_workflow_learning_pending_retry_updates_memory_and_is_idempotent(tmp_path):
    repo = RepositoryRef(101, "owner/repo")
    event = SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id="1",
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="operator",
        body="@agent do work",
        html_url=None,
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issues",
        [event],
        since="now",
        etag=None,
        polled_at="now",
    )
    thread_id = store.source_event(event.event_key)["thread_id"]
    plan_engine = WorkflowEngine(store=store)
    plan = plan_engine.start_cycle(event_key=event.event_key, plan_text="plan")
    worktree = tmp_path / "workspace"
    worktree.mkdir()
    evidence_file = worktree / "README.md"
    evidence_file.write_text("Use the repository command\n")
    workspace = ThreadWorkspaceRecord(
        thread_id=thread_id,
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        issue_number=7,
        source_repository_path=str(tmp_path),
        workspace_path=str(worktree),
        branch_name="sweforge/issue-7",
        base_commit="base",
        created_at="now",
        updated_at="now",
    )
    store.save_thread_workspace(workspace)
    state = store.workflow_state(thread_id)
    assert state is not None
    idle = WorkflowStateRecord(**{**state.__dict__, "phase": WorkflowPhase.IDLE})
    store.save_workflow_state(idle)
    store.save_repo_memory_learning(
        RepoMemoryLearningRecord(
            learning_id=memory_learning_id_for(
                thread_id=thread_id,
                cycle_id=plan.cycle_id,
                root_input_id=event.event_key,
            ),
            source_event_key=event.event_key,
            thread_id=thread_id,
            cycle_id=plan.cycle_id,
            root_input_id=event.event_key,
            repo_id=repo.repo_id,
            status="PENDING",
            accepted_candidates=0,
            rejected_candidates=0,
            error_message=None,
            created_at="now",
            updated_at="now",
        )
    )
    import hashlib

    excerpt = evidence_file.read_text().rstrip()
    candidate = RepoMemoryCandidate(
        candidate_id="command",
        category="TOOLING",
        fact="Use the repository command",
        evidence=[
            RepoMemoryEvidence(
                path="README.md",
                start_line=1,
                end_line=1,
                content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                excerpt=excerpt,
            )
        ],
        durability_reason="The repository documents a stable command.",
    )
    memory = SQLiteMemoryStore(":memory:")
    engine = WorkflowEngine(
        store=store,
        memory_learner=lambda **_: [candidate],
    )
    result = engine.advance(
        thread_id=thread_id,
        model="unused",
        repo_paths={},
        workspace_root=tmp_path,
        memory_store=memory.store,
        execute_kwargs={"memory_lock_root": tmp_path / "locks"},
    )
    assert result.phase is WorkflowPhase.IDLE
    assert store.repo_memory_learning(event.event_key).status == "UPDATED"
    assert (read_repo_memory(memory.store, repo_memory_namespace(101)) or "").count(
        "Use the repository command"
    ) == 1
    memory.close()
    store.close()
