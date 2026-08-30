import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from langchain_core.tools import StructuredTool

from sweforge.agent import _build_backend
from sweforge.agent_trace import AgentTracer
from sweforge.capabilities import (
    MCPServerSpec,
    RepoCapabilityRegistry,
    load_repo_mcp_tools,
)
from sweforge.context import RepoAgentContext
from sweforge.github_models import SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config import RepoConfigRegistry, validate_repo_bundle
from sweforge.repo_secrets import RepoSecretStore
from sweforge.script_tools import ScriptToolExecutor, build_script_tools

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"


class CaptureSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def configured(tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    state = SQLiteGitHubStore(tmp_path / "state.db")
    state.upsert_repository(1, "a/repo", "now")
    state.upsert_repository(2, "b/repo", "now")
    registry = RepoConfigRegistry(state)
    generation = registry.install(1, bundle, now="now")
    secrets = RepoSecretStore(state, Fernet.generate_key())
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return bundle, state, registry, generation, secrets, worktree


def executor_for(registry, generation, secrets, worktree, **kwargs):
    executor = ScriptToolExecutor(
        worktree=worktree,
        files_for=lambda spec: registry.script_files(1, generation.generation_id, spec),
        secret_resolver=lambda spec: secrets.resolve_env(
            1, spec.secret_env, subject=f"script:{spec.name}"
        ),
        unsafe_local_shell=kwargs.pop("unsafe_local_shell", True),
        **kwargs,
    )
    tools, _effects = build_script_tools(
        registry.script_specs(1, generation.generation_id), executor
    )
    return executor, tools[0]


def ingest_thread(state, repo_id, repo, number):
    result = state.record_batch(
        repo_id,
        "issues",
        [
            SourceEvent(
                repo_id=repo_id,
                repo_full_name=repo,
                source_kind=SourceKind.ISSUE,
                source_id=str(number),
                source_updated_at=f"2026-01-01T00:00:{number:02d}Z",
                subject_kind=SubjectKind.ISSUE,
                subject_number=number,
                author_login="operator",
                body="@agent test generation-bound credentials",
                html_url=None,
            )
        ],
        since="2026-01-01T00:00:00Z",
        etag=None,
        polled_at=f"2026-01-01T00:01:{number:02d}Z",
    )
    return result.created_thread_ids[0]


def test_script_receives_minimal_declared_env_and_schema_has_no_secret(tmp_path):
    _bundle, _state, registry, generation, secrets, worktree = configured(tmp_path)
    secrets.set(1, "RELEASE_POLICY_TOKEN", "repo-a-secret-value")
    captures = []

    class Runner:
        def execute_tool(self, command, *, stdin, env, timeout):
            captures.append((command, stdin, dict(env), timeout))
            return subprocess.CompletedProcess(command, 0, "ok", "")

    executor, tool = executor_for(
        registry,
        generation,
        secrets,
        worktree,
        sandbox_backend=Runner(),
        unsafe_local_shell=False,
    )
    assert tool.invoke({"config_path": "policy.json"}) == "ok"

    environment = captures[0][2]
    assert environment["RELEASE_POLICY_TOKEN"] == "repo-a-secret-value"
    assert environment["RELEASE_REGION"] == "example-region"
    assert set(environment) == {
        "PATH",
        "LANG",
        "RELEASE_REGION",
        "RELEASE_POLICY_TOKEN",
    }
    assert "RELEASE_POLICY_TOKEN" not in tool.args
    assert "secret" not in json.dumps(tool.args).casefold()
    assert json.loads(captures[0][1]) == {"config_path": "policy.json"}
    with pytest.raises((TypeError, ValueError)):
        tool.invoke(
            {
                "config_path": "policy.json",
                "RELEASE_POLICY_TOKEN": "model-override-attempt",
            }
        )

    unrelated = replace(
        registry.script_specs(1, generation.generation_id)[0],
        name="unrelated_read_tool",
        secret_env={},
    )
    assert executor.invoke(unrelated, {"config_path": "other.json"}) == "ok"
    assert "RELEASE_POLICY_TOKEN" not in captures[-1][2]


def test_output_stderr_exception_and_trace_redact_injected_values(tmp_path):
    _bundle, _state, registry, generation, secrets, worktree = configured(tmp_path)
    secret = "printable-secret-value"
    secrets.set(1, "RELEASE_POLICY_TOKEN", secret)
    sink = CaptureSink()

    class PrintingRunner:
        def execute_tool(self, command, *, stdin, env, timeout):
            value = env["RELEASE_POLICY_TOKEN"]
            return subprocess.CompletedProcess(
                command, 3, f"stdout={value}", f"stderr={value}"
            )

    _executor, tool = executor_for(
        registry,
        generation,
        secrets,
        worktree,
        sandbox_backend=PrintingRunner(),
        unsafe_local_shell=False,
        tracer=AgentTracer(sink),
    )
    result = tool.invoke({"config_path": "policy.json"})
    assert result.count("[REDACTED]") == 2
    assert secret not in result
    assert secret not in "\n".join(event.message for event in sink.events)
    assert {event.category for event in sink.events} == {
        "SECRET RESOLUTION",
        "SCRIPT TOOL CALL",
        "SCRIPT TOOL RESULT",
    }

    class FailingRunner:
        def execute_tool(self, command, *, stdin, env, timeout):
            raise RuntimeError(f"transport exposed {env['RELEASE_POLICY_TOKEN']}")

    _executor, failing_tool = executor_for(
        registry,
        generation,
        secrets,
        worktree,
        sandbox_backend=FailingRunner(),
        unsafe_local_shell=False,
    )
    with pytest.raises(RuntimeError, match="REDACTED") as caught:
        failing_tool.invoke({"config_path": "policy.json"})
    assert secret not in str(caught.value)


def test_rotation_deletion_and_readd_affect_same_generation_without_digest_change(
    tmp_path,
):
    _bundle, _state, registry, generation, secrets, worktree = configured(tmp_path)
    captures = []

    class Runner:
        def execute_tool(self, command, *, stdin, env, timeout):
            captures.append(env["RELEASE_POLICY_TOKEN"])
            return subprocess.CompletedProcess(command, 0, "ok", "")

    _executor, tool = executor_for(
        registry,
        generation,
        secrets,
        worktree,
        sandbox_backend=Runner(),
        unsafe_local_shell=False,
    )
    secrets.set(1, "RELEASE_POLICY_TOKEN", "rotation-value-one")
    tool.invoke({"config_path": "one"})
    secrets.set(1, "RELEASE_POLICY_TOKEN", "rotation-value-two")
    tool.invoke({"config_path": "two"})
    assert captures == ["rotation-value-one", "rotation-value-two"]
    assert registry.current_generation(1).digest == generation.digest

    secrets.delete(1, "RELEASE_POLICY_TOKEN")
    with pytest.raises(PermissionError, match="not configured"):
        tool.invoke({"config_path": "missing"})
    assert captures == ["rotation-value-one", "rotation-value-two"]
    secrets.set(1, "RELEASE_POLICY_TOKEN", "rotation-value-three")
    tool.invoke({"config_path": "three"})
    assert captures[-1] == "rotation-value-three"


def test_repo_a_tool_never_resolves_repo_b_value(tmp_path):
    _bundle, _state, registry, generation, secrets, worktree = configured(tmp_path)
    secrets.set(1, "RELEASE_POLICY_TOKEN", "repository-a-value")
    secrets.set(2, "RELEASE_POLICY_TOKEN", "repository-b-value")
    captures = []

    class Runner:
        def execute_tool(self, command, *, stdin, env, timeout):
            captures.append(env["RELEASE_POLICY_TOKEN"])
            return subprocess.CompletedProcess(command, 0, "ok", "")

    _executor, tool = executor_for(
        registry,
        generation,
        secrets,
        worktree,
        sandbox_backend=Runner(),
        unsafe_local_shell=False,
    )
    tool.invoke({"config_path": "policy"})
    assert captures == ["repository-a-value"]


def test_generic_execute_never_inherits_repository_tool_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RELEASE_POLICY_TOKEN", "must-not-reach-generic-execute")
    backend = _build_backend(str(tmp_path))
    result = backend.execute("env")
    assert "RELEASE_POLICY_TOKEN" not in result.output
    assert "must-not-reach-generic-execute" not in result.output


def test_secret_reference_changes_digest_but_values_do_not(tmp_path):
    bundle, _state, registry, generation, secrets, _worktree = configured(tmp_path)
    secrets.set(1, "RELEASE_POLICY_TOKEN", "first-configured-value")
    assert registry.current_generation(1).digest == generation.digest
    secrets.set(1, "RELEASE_POLICY_TOKEN", "second-configured-value")
    assert registry.current_generation(1).digest == generation.digest

    metadata = bundle / "tools" / "scripts" / "validate-release" / "tool.yaml"
    changed = metadata.read_text().replace(
        "RELEASE_POLICY_TOKEN: RELEASE_POLICY_TOKEN",
        "RELEASE_POLICY_TOKEN: DIFFERENT_TOKEN",
    )
    metadata.write_text(changed)
    assert validate_repo_bundle(bundle).digest != generation.digest


def test_issue_generations_freeze_reference_sets_but_resolve_current_values(tmp_path):
    bundle, state, registry, first, secrets, worktree = configured(tmp_path)
    first_thread = ingest_thread(state, 1, "a/repo", 1)
    metadata = bundle / "tools" / "scripts" / "validate-release" / "tool.yaml"
    metadata.write_text(
        metadata.read_text().replace(
            "RELEASE_POLICY_TOKEN: RELEASE_POLICY_TOKEN",
            "RELEASE_POLICY_TOKEN: NEXT_RELEASE_POLICY_TOKEN",
        )
    )
    second = registry.install(1, bundle, now="generation-two")
    second_thread = ingest_thread(state, 1, "a/repo", 2)

    assert registry.thread_generation(first_thread) == first
    assert registry.thread_generation(second_thread) == second
    assert registry.script_specs(1, first.generation_id)[0].secret_env == {
        "RELEASE_POLICY_TOKEN": "RELEASE_POLICY_TOKEN"
    }
    assert registry.script_specs(1, second.generation_id)[0].secret_env == {
        "RELEASE_POLICY_TOKEN": "NEXT_RELEASE_POLICY_TOKEN"
    }

    captures = []

    class Runner:
        def execute_tool(self, command, *, stdin, env, timeout):
            captures.append(env["RELEASE_POLICY_TOKEN"])
            return subprocess.CompletedProcess(command, 0, "ok", "")

    secrets.set(1, "RELEASE_POLICY_TOKEN", "old-reference-current-one")
    secrets.set(1, "NEXT_RELEASE_POLICY_TOKEN", "new-reference-current-one")
    _old_executor, old_tool = executor_for(
        registry,
        first,
        secrets,
        worktree,
        sandbox_backend=Runner(),
        unsafe_local_shell=False,
    )
    _new_executor, new_tool = executor_for(
        registry,
        second,
        secrets,
        worktree,
        sandbox_backend=Runner(),
        unsafe_local_shell=False,
    )
    old_tool.invoke({"config_path": "old"})
    new_tool.invoke({"config_path": "new"})
    secrets.set(1, "RELEASE_POLICY_TOKEN", "old-reference-current-two")
    secrets.set(1, "NEXT_RELEASE_POLICY_TOKEN", "new-reference-current-two")
    old_tool.invoke({"config_path": "old-again"})
    new_tool.invoke({"config_path": "new-again"})
    assert captures == [
        "old-reference-current-one",
        "new-reference-current-one",
        "old-reference-current-two",
        "new-reference-current-two",
    ]


def test_local_mcp_receives_only_declared_repo_secret_env(monkeypatch, tmp_path):
    _bundle, state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    secrets.set(1, "MCP_TOKEN", "repo-a-mcp-secret")
    secrets.set(2, "MCP_TOKEN", "repo-b-mcp-secret")
    calls = []

    class FakeClient:
        def __init__(self, connections, **kwargs):
            calls.append(connections)

        async def get_tools(self, *, server_name=None):
            return [SimpleNamespace(name=f"{server_name}_lookup")]

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", FakeClient)
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec(
            "internal",
            {"transport": "stdio", "command": "server", "env": {"FIXED": "yes"}},
            {"INTERNAL_TOKEN": "MCP_TOKEN"},
        )
    )
    registry.approve(1, "internal", {"lookup"})
    registry.approve(2, "internal", {"lookup"})
    sink = CaptureSink()
    tools, _client = asyncio.run(
        load_repo_mcp_tools(
            registry,
            RepoAgentContext(1, "a/repo", "thread"),
            secret_store=secrets,
            tracer=AgentTracer(sink),
        )
    )

    assert [tool.name for tool in tools] == ["internal_lookup"]
    environment = calls[0]["internal"]["env"]
    assert environment["INTERNAL_TOKEN"] == "repo-a-mcp-secret"
    assert environment["FIXED"] == "yes"
    assert "repo-b-mcp-secret" not in json.dumps(calls[0])
    assert "repo-a-mcp-secret" not in "\n".join(event.message for event in sink.events)
    assert sink.events[0].category == "MCP SERVER START"

    second_tools, _second_client = asyncio.run(
        load_repo_mcp_tools(
            registry,
            RepoAgentContext(2, "b/repo", "thread-b"),
            secret_store=secrets,
        )
    )
    assert [tool.name for tool in second_tools] == ["internal_lookup"]
    assert calls[1]["internal"]["env"]["INTERNAL_TOKEN"] == "repo-b-mcp-secret"
    assert calls[0] is not calls[1]


def test_missing_mcp_secret_fails_before_client_start(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("MCP client must not start")

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", ForbiddenClient)
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec(
            "internal",
            {"transport": "stdio", "command": "server"},
            {"TOKEN": "MISSING"},
        )
    )
    registry.approve(1, "internal", {"lookup"})
    sink = CaptureSink()
    with pytest.raises(PermissionError, match="not configured"):
        asyncio.run(
            load_repo_mcp_tools(
                registry,
                RepoAgentContext(1, "a/repo", "thread"),
                secret_store=secrets,
                tracer=AgentTracer(sink),
            )
        )
    assert sink.events[0].category == "SECRET RESOLUTION"
    assert "required_missing=1" in sink.events[0].message


REMOTE_MCP_YAML = """  release-service:
    connection:
      transport: streamable_http
      url: https://mcp.example.invalid/mcp
    headers:
      X-Client-Version: sweforge
    secret_headers:
      Authorization: RELEASE_MCP_AUTH
    tools: [lookup_release]
"""


def remote_registry(secret_headers=None, repo_ids=(1,)):
    registry = RepoCapabilityRegistry()
    registry.register_server(
        MCPServerSpec(
            "release-service",
            {
                "transport": "streamable_http",
                "url": "https://mcp.example.invalid/mcp",
                "headers": {"X-Client-Version": "sweforge"},
            },
            None,
            secret_headers,
        )
    )
    for repo_id in repo_ids:
        registry.approve(repo_id, "release-service", {"lookup_release"})
    return registry


def recording_client(calls, *, fail_with=None):
    class FakeClient:
        def __init__(self, connections, **kwargs):
            calls.append(connections)

        async def get_tools(self, *, server_name=None):
            if fail_with is not None:
                raise RuntimeError(fail_with)
            return [
                StructuredTool.from_function(
                    func=lambda release_id: release_id,
                    name=f"{server_name}_lookup_release",
                    description="Look up a release. Requires configured credentials.",
                )
            ]

    return FakeClient


def test_remote_mcp_without_secret_headers_still_connects(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    tools, _client = asyncio.run(
        load_repo_mcp_tools(
            remote_registry(),
            RepoAgentContext(1, "a/repo", "thread"),
            secret_store=secrets,
        )
    )
    assert [tool.name for tool in tools] == ["release-service_lookup_release"]
    connection = calls[0]["release-service"]
    assert connection["headers"] == {"X-Client-Version": "sweforge"}
    assert "httpx_client_factory" not in connection


def test_remote_mcp_secret_headers_resolve_for_the_authoritative_repo(
    monkeypatch, tmp_path
):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer repo-a-remote-value")
    secrets.set(2, "RELEASE_MCP_AUTH", "Bearer repo-b-remote-value")
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    sink = CaptureSink()
    registry = remote_registry({"Authorization": "RELEASE_MCP_AUTH"}, repo_ids=(1, 2))

    tools, first_client = asyncio.run(
        load_repo_mcp_tools(
            registry,
            RepoAgentContext(1, "a/repo", "thread-a"),
            secret_store=secrets,
            tracer=AgentTracer(sink),
        )
    )
    connection = calls[0]["release-service"]
    assert connection["headers"] == {
        "X-Client-Version": "sweforge",
        "Authorization": "Bearer repo-a-remote-value",
    }
    assert "repo-b-remote-value" not in json.dumps(calls[0], default=str)

    schema = json.dumps(
        [
            {
                "name": tool.name,
                "description": tool.description,
                "args": tool.args,
            }
            for tool in tools
        ]
    )
    assert "repo-a-remote-value" not in schema
    assert "Authorization" not in schema
    assert "RELEASE_MCP_AUTH" not in schema
    assert "X-Client-Version" not in schema

    trace = "\n".join(event.message for event in sink.events)
    assert "repo-a-remote-value" not in trace
    assert "RELEASE_MCP_AUTH" not in trace
    assert "transport=streamable_http" in trace
    assert "required=1 resolved=1" in trace

    _second_tools, second_client = asyncio.run(
        load_repo_mcp_tools(
            registry,
            RepoAgentContext(2, "b/repo", "thread-b"),
            secret_store=secrets,
        )
    )
    assert calls[1]["release-service"]["headers"]["Authorization"] == (
        "Bearer repo-b-remote-value"
    )
    assert "repo-a-remote-value" not in json.dumps(calls[1], default=str)
    assert first_client is not second_client
    assert calls[0] is not calls[1]


def test_remote_mcp_credentials_stay_on_the_configured_origin(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer redirect-guard-value")
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    asyncio.run(
        load_repo_mcp_tools(
            remote_registry({"Authorization": "RELEASE_MCP_AUTH"}),
            RepoAgentContext(1, "a/repo", "thread"),
            secret_store=secrets,
        )
    )
    factory = calls[0]["release-service"]["httpx_client_factory"]
    client = factory(headers={"Authorization": "Bearer redirect-guard-value"})
    assert client.follow_redirects is False
    asyncio.run(client.aclose())


def test_missing_remote_secret_fails_before_client_start(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("MCP client must not start")

    monkeypatch.setattr("sweforge.capabilities.MultiServerMCPClient", ForbiddenClient)
    sink = CaptureSink()
    with pytest.raises(PermissionError, match="not configured"):
        asyncio.run(
            load_repo_mcp_tools(
                remote_registry({"Authorization": "RELEASE_MCP_AUTH"}),
                RepoAgentContext(1, "a/repo", "thread"),
                secret_store=secrets,
                tracer=AgentTracer(sink),
            )
        )
    assert [event.category for event in sink.events] == ["SECRET RESOLUTION"]
    assert "required_missing=1" in sink.events[0].message


def test_remote_mcp_transport_failure_is_redacted(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    secret = "Bearer transport-error-value"
    secrets.set(1, "RELEASE_MCP_AUTH", secret)
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient",
        recording_client([], fail_with=f"401 for Authorization: {secret}"),
    )
    with pytest.raises(RuntimeError, match="REDACTED") as caught:
        asyncio.run(
            load_repo_mcp_tools(
                remote_registry({"Authorization": "RELEASE_MCP_AUTH"}),
                RepoAgentContext(1, "a/repo", "thread"),
                secret_store=secrets,
            )
        )
    assert secret not in str(caught.value)


def test_remote_mcp_rotation_applies_to_the_next_fresh_client(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    registry = remote_registry({"Authorization": "RELEASE_MCP_AUTH"})
    context = RepoAgentContext(1, "a/repo", "thread")

    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer rotation-value-one")
    asyncio.run(load_repo_mcp_tools(registry, context, secret_store=secrets))
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer rotation-value-two")
    asyncio.run(load_repo_mcp_tools(registry, context, secret_store=secrets))
    assert [item["release-service"]["headers"]["Authorization"] for item in calls] == [
        "Bearer rotation-value-one",
        "Bearer rotation-value-two",
    ]


def test_repo_b_remote_server_is_invisible_to_repo_a(monkeypatch, tmp_path):
    _bundle, _state, _registry, _generation, secrets, _worktree = configured(tmp_path)
    secrets.set(2, "RELEASE_MCP_AUTH", "Bearer repo-b-only-value")
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    registry = remote_registry({"Authorization": "RELEASE_MCP_AUTH"}, repo_ids=(2,))

    tools, _client = asyncio.run(
        load_repo_mcp_tools(
            registry, RepoAgentContext(1, "a/repo", "thread"), secret_store=secrets
        )
    )
    assert tools == []
    assert calls[0] == {}


def install_remote_bundle(tmp_path, name="remote-bundle"):
    """Add one secret-authenticated remote server to the reference bundle."""
    bundle = tmp_path / name
    shutil.copytree(EXAMPLE, bundle)
    servers = bundle / "tools" / "mcp" / "servers.yaml"
    servers.write_text(servers.read_text() + REMOTE_MCP_YAML)
    return bundle


def configure_reference_credentials(secrets, repo_id, suffix=""):
    """Configure the credentials the reference bundle's own servers require."""
    for name in (
        "RELEASE_CATALOG_TOKEN",
        "RELEASE_SERVICE_AUTH",
        "RELEASE_SERVICE_INTERNAL_TOKEN",
    ):
        secrets.set(repo_id, name, f"reference-value-{name.lower()}{suffix}")


def test_installed_remote_generation_freezes_references_and_hides_values(tmp_path):
    bundle = install_remote_bundle(tmp_path)
    state = SQLiteGitHubStore(tmp_path / "state.db")
    state.upsert_repository(1, "a/repo", "now")
    registry = RepoConfigRegistry(state)
    first = registry.install(1, bundle, now="one")
    secrets = RepoSecretStore(state, Fernet.generate_key())
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer never-shown-value")
    configure_reference_credentials(secrets, 1)

    capabilities = registry.capability_registry(1, first.generation_id)
    spec = capabilities.approved_servers(1)["release-service"]
    assert spec.secret_headers == {"Authorization": "RELEASE_MCP_AUTH"}
    assert spec.connection["headers"] == {"X-Client-Version": "sweforge"}

    servers = bundle / "tools" / "mcp" / "servers.yaml"
    servers.write_text(
        servers.read_text().replace("RELEASE_MCP_AUTH", "NEXT_RELEASE_MCP_AUTH")
    )
    second = registry.install(1, bundle, now="two")
    assert second.digest != first.digest
    frozen = registry.capability_registry(1, first.generation_id)
    assert frozen.approved_servers(1)["release-service"].secret_headers == {
        "Authorization": "RELEASE_MCP_AUTH"
    }
    assert registry.capability_registry(1, second.generation_id).approved_servers(1)[
        "release-service"
    ].secret_headers == {"Authorization": "NEXT_RELEASE_MCP_AUTH"}

    summary = json.dumps(registry.safe_summary(1))
    assert "never-shown-value" not in summary
    assert "Authorization" not in summary
    assert "NEXT_RELEASE_MCP_AUTH" not in summary
    assert '"required": 1' in json.dumps(registry.safe_summary(1), indent=None)


def test_installed_remote_generation_resolves_current_rotated_values(
    monkeypatch, tmp_path
):
    bundle = install_remote_bundle(tmp_path, "rotating-bundle")
    state = SQLiteGitHubStore(tmp_path / "state.db")
    state.upsert_repository(1, "a/repo", "now")
    state.upsert_repository(2, "b/repo", "now")
    config = RepoConfigRegistry(state)
    generation = config.install(1, bundle, now="one")
    secrets = RepoSecretStore(state, Fernet.generate_key())
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer installed-value-one")
    secrets.set(2, "RELEASE_MCP_AUTH", "Bearer other-repo-value")
    configure_reference_credentials(secrets, 1)
    calls = []
    monkeypatch.setattr(
        "sweforge.capabilities.MultiServerMCPClient", recording_client(calls)
    )
    capabilities = config.capability_registry(1, generation.generation_id)

    asyncio.run(
        load_repo_mcp_tools(
            capabilities,
            RepoAgentContext(1, "a/repo", "thread"),
            secret_store=secrets,
        )
    )
    secrets.set(1, "RELEASE_MCP_AUTH", "Bearer installed-value-two")
    asyncio.run(
        load_repo_mcp_tools(
            capabilities,
            RepoAgentContext(1, "a/repo", "thread"),
            secret_store=secrets,
        )
    )
    assert [item["release-service"]["headers"]["Authorization"] for item in calls] == [
        "Bearer installed-value-one",
        "Bearer installed-value-two",
    ]
    assert "other-repo-value" not in json.dumps(calls, default=str)
