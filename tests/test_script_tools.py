import json
import shutil
from pathlib import Path

import pytest

from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config import RepoConfigRegistry, validate_repo_bundle
from sweforge.repo_secrets import SecretValue
from sweforge.script_tools import ScriptToolExecutor, build_script_tools

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"


def resolve_test_secrets(spec):
    return {
        environment_name: SecretValue("test-secret-value")
        for environment_name in spec.secret_env
    }


def configured(tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(1, "owner/repo", "now")
    registry = RepoConfigRegistry(store)
    generation = registry.install(1, bundle, now="now")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executor = ScriptToolExecutor(
        worktree=worktree,
        files_for=lambda spec: registry.script_files(1, generation.generation_id, spec),
        secret_resolver=resolve_test_secrets,
        unsafe_local_shell=True,
    )
    tools, effects = build_script_tools(
        registry.script_specs(1, generation.generation_id), executor
    )
    return bundle, worktree, tools[0], effects


def test_registered_script_exposes_only_declared_schema_and_fixed_effect(tmp_path):
    _bundle, _worktree, tool, effects = configured(tmp_path)
    assert tool.name == "validate_release"
    assert tool.args == {"config_path": {"type": "string"}}
    assert "entrypoint" not in tool.args
    assert "timeout" not in tool.args
    assert "env" not in tool.args
    assert effects == {"validate_release": "read"}


def test_registered_script_executes_fixed_entrypoint_in_current_worktree(tmp_path):
    _bundle, worktree, tool, _effects = configured(tmp_path)
    (worktree / "release.json").write_text(json.dumps({"checks": ["unit", "lint"]}))

    result = tool.invoke({"config_path": "release.json"})

    assert json.loads(result) == {
        "valid": True,
        "check_count": 2,
        "credential_configured": True,
        "region": "example-region",
    }
    assert list(worktree.glob(".sweforge-tool-*")) == []


def test_malformed_arguments_fail_before_script_execution(tmp_path):
    _bundle, worktree, tool, _effects = configured(tmp_path)
    with pytest.raises(Exception, match="config_path"):
        tool.invoke({})
    with pytest.raises(Exception):
        tool.invoke({"config_path": "x", "entrypoint": "other.py"})
    assert list(worktree.glob(".sweforge-tool-*")) == []


def test_nonzero_stderr_and_output_are_bounded(tmp_path):
    bundle, _worktree, _tool, _effects = configured(tmp_path)
    script = bundle / "tools" / "scripts" / "validate-release" / "validate_release.py"
    script.write_text(
        "import sys\n"
        "print('x' * 50000)\n"
        "print('failure', file=sys.stderr)\n"
        "raise SystemExit(3)\n"
    )
    validated = validate_repo_bundle(bundle)
    spec = validated.scripts[0]
    worktree = tmp_path / "other-worktree"
    worktree.mkdir()
    executor = ScriptToolExecutor(
        worktree=worktree,
        files_for=lambda _spec: {
            "validate_release.py": script.read_text(),
            "tool.yaml": (
                bundle / "tools" / "scripts" / "validate-release" / "tool.yaml"
            ).read_text(),
        },
        secret_resolver=resolve_test_secrets,
        unsafe_local_shell=True,
    )

    result = executor.invoke(spec, {"config_path": "unused"})

    assert "exit code 3" in result
    assert "script output bounded" in result
    assert "stderr:\nfailure" in result


def test_timeout_is_enforced(tmp_path):
    bundle, _worktree, _tool, _effects = configured(tmp_path)
    tool_yaml = bundle / "tools" / "scripts" / "validate-release" / "tool.yaml"
    tool_yaml.write_text(
        tool_yaml.read_text().replace("timeout_seconds: 30", "timeout_seconds: 1")
    )
    script = tool_yaml.parent / "validate_release.py"
    script.write_text("import time\ntime.sleep(5)\n")
    validated = validate_repo_bundle(bundle)
    worktree = tmp_path / "timeout-worktree"
    worktree.mkdir()
    executor = ScriptToolExecutor(
        worktree=worktree,
        files_for=lambda _spec: {
            "validate_release.py": script.read_text(),
            "tool.yaml": tool_yaml.read_text(),
        },
        secret_resolver=resolve_test_secrets,
        unsafe_local_shell=True,
    )
    with pytest.raises(TimeoutError, match="timeout"):
        executor.invoke(validated.scripts[0], {"config_path": "unused"})


def test_shell_runtime_uses_json_stdin_and_fixed_entrypoint(tmp_path):
    bundle, worktree, _tool, _effects = configured(tmp_path)
    directory = bundle / "tools" / "scripts" / "validate-release"
    metadata = directory / "tool.yaml"
    metadata.write_text(
        metadata.read_text()
        .replace("runtime: python", "runtime: shell")
        .replace("entrypoint: validate_release.py", "entrypoint: validate_release.sh")
    )
    script = directory / "validate_release.sh"
    script.write_text("payload=$(cat)\nprintf '%s\\n' \"$payload\"\n")
    validated = validate_repo_bundle(bundle)
    executor = ScriptToolExecutor(
        worktree=worktree,
        files_for=lambda _spec: {
            "validate_release.sh": script.read_text(),
            "tool.yaml": metadata.read_text(),
        },
        secret_resolver=resolve_test_secrets,
        unsafe_local_shell=True,
    )

    result = executor.invoke(
        validated.scripts[0], {"config_path": "release-policy.json"}
    )

    assert json.loads(result) == {"config_path": "release-policy.json"}
