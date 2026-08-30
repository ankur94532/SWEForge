import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

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
