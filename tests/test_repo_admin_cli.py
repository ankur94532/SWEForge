import json
import shutil
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from sweforge.cli import main as root_main
from sweforge.github_models import SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config import RepoConfigRegistry
from sweforge.repo_secrets import RepoSecretStore

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"

REMOTE_MCP = """version: 1
servers:
  release-service:
    connection:
      transport: streamable_http
      url: https://mcp.example.invalid/mcp
    headers:
      X-Client-Version: sweforge
    secret_headers:
      Authorization: RELEASE_MCP_AUTH
    tools: [lookup_release]
"""

NEW_TOOL = """version: 1
name: publish_report
description: Publish the rendered readiness report to the release channel.
runtime: python
entrypoint: publish_report.py
effect: read
timeout_seconds: 30
args_schema:
  type: object
  properties:
    report_path:
      type: string
  required: [report_path]
  additionalProperties: false
"""


def state_with(tmp_path, repos=(("owner/repo", 1),)):
    path = tmp_path / "state.db"
    store = SQLiteGitHubStore(path)
    for full_name, repo_id in repos:
        store.upsert_repository(repo_id, full_name, "now")
    store.close()
    return path


def configured(tmp_path, repos=(("owner/repo", 1),), source="bundle"):
    state = state_with(tmp_path, repos)
    bundle = tmp_path / source
    if not bundle.exists():
        shutil.copytree(EXAMPLE, bundle)
    store = SQLiteGitHubStore(state)
    registry = RepoConfigRegistry(store)
    for _full_name, repo_id in repos:
        registry.install(repo_id, bundle, now="initial")
    store.close()
    return state, bundle


def run(state, *argv):
    return root_main([argv[0], "--state-db", str(state), *argv[1:]])


def registry_for(state):
    return RepoConfigRegistry(SQLiteGitHubStore(state))


def current(state, repo_id=1):
    return registry_for(state).current_generation(repo_id)


def skill_source(tmp_path, name, directory=None, body=None):
    source = tmp_path / (directory or name)
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        body
        if body is not None
        else f"---\nname: {name}\ndescription: Trusted {name} guidance.\n---\n\n"
        f"# {name.title()}\n\nFollow the {name} rules.\n"
    )
    return source


def tool_source(tmp_path, directory="publish-report", metadata=NEW_TOOL):
    source = tmp_path / directory
    source.mkdir(parents=True)
    (source / "tool.yaml").write_text(metadata)
    (source / "publish_report.py").write_text("import sys\n\nprint(sys.stdin.read())\n")
    return source


def ingest_thread(state, repo_id, repo, number):
    store = SQLiteGitHubStore(state)
    result = store.record_batch(
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
                body="@agent implement the requested change",
                html_url=None,
            )
        ],
        since="2026-01-01T00:00:00Z",
        etag=None,
        polled_at=f"2026-01-01T00:01:{number:02d}Z",
    )
    store.close()
    return result.created_thread_ids[0]


def test_workflow_set_installs_the_next_generation_and_freezes_the_old_one(
    tmp_path, capsys
):
    state, bundle = configured(tmp_path)
    first = current(state)
    first_thread = ingest_thread(state, 1, "owner/repo", 1)
    updated = tmp_path / "workflow.yaml"
    updated.write_text(
        (bundle / "workflow.yaml")
        .read_text()
        .replace("workflow_id: release-readiness", "workflow_id: release-readiness-v2")
    )

    assert run(state, "workflow", "set", "owner/repo", str(updated)) == 0
    output = capsys.readouterr().out
    second = current(state)
    second_thread = ingest_thread(state, 1, "owner/repo", 2)

    assert second.generation == 2
    assert second.digest != first.digest
    assert second.workflow_id == "release-readiness-v2"
    assert f"Repository: owner/repo\nGeneration: 2\nDigest: {second.digest}" in output
    assert "Replaced workflow: workflow.yaml" in output

    registry = registry_for(state)
    assert registry.generation(1, first.generation_id).digest == first.digest
    assert registry.generation(1, first.generation_id).workflow_id == (
        "release-readiness"
    )
    assert registry.thread_generation(first_thread).generation == 1
    assert registry.thread_generation(second_thread).generation == 2


def test_invalid_workflow_leaves_the_current_generation_untouched(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    first = current(state)
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nworkflow_id: broken\ntasks: []\n")

    assert run(state, "workflow", "set", "owner/repo", str(broken)) == 2
    assert "sweforge workflow:" in capsys.readouterr().err
    assert current(state) == first
    store = SQLiteGitHubStore(state)
    assert (
        store.connection.execute(
            "SELECT count(*) FROM repo_config_generations_v1"
        ).fetchone()[0]
        == 1
    )


def test_workflow_set_reports_missing_skill_and_unknown_tool_references(
    tmp_path, capsys
):
    state, bundle = configured(tmp_path)
    missing_skill = tmp_path / "missing-skill.yaml"
    missing_skill.write_text(
        (bundle / "workflow.yaml")
        .read_text()
        .replace("skill: reporting", "skill: gone")
    )
    assert run(state, "workflow", "set", "owner/repo", str(missing_skill)) == 2
    assert "gone" in capsys.readouterr().err

    unknown_tool = tmp_path / "unknown-tool.yaml"
    unknown_tool.write_text(
        (bundle / "workflow.yaml")
        .read_text()
        .replace("validate_release", "prod_deploy")
    )
    assert run(state, "workflow", "set", "owner/repo", str(unknown_tool)) == 2
    assert "prod_deploy" in capsys.readouterr().err
    assert current(state).generation == 1


def test_skill_add_duplicate_and_replace(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    source = skill_source(tmp_path, "release-notes")

    assert run(state, "skill", "add", "owner/repo", str(source)) == 0
    assert "Added skill: release-notes" in capsys.readouterr().out
    added = current(state)
    assert added.generation == 2
    assert {item["name"] for item in added.manifest["skills"]} >= {"release-notes"}

    assert run(state, "skill", "add", "owner/repo", str(source)) == 2
    assert 'skill "release-notes" is already installed; use --replace' in (
        capsys.readouterr().err
    )
    assert current(state) == added

    (source / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Updated release notes guidance.\n"
        "---\n\n# Release notes\n\nUpdated rules.\n"
    )
    assert run(state, "skill", "add", "owner/repo", str(source), "--replace") == 0
    assert "Replaced skill: release-notes" in capsys.readouterr().out
    replaced = current(state)
    assert replaced.generation == 3
    assert replaced.digest != added.digest
    assert (
        registry_for(state)
        .read_skill(1, replaced.generation_id, "release-notes")
        .endswith("Updated rules.\n")
    )


def test_skill_name_comes_from_trusted_metadata_not_the_source_directory(
    tmp_path, capsys
):
    state, _bundle = configured(tmp_path)
    source = skill_source(tmp_path, "release-notes", directory="staging-copy")

    assert run(state, "skill", "add", "owner/repo", str(source)) == 0
    assert "Added skill: release-notes" in capsys.readouterr().out
    generation = current(state)
    assert "release-notes/SKILL.md" in registry_for(state).skill_files(
        1, generation.generation_id
    )


def test_malformed_skill_is_rejected_atomically(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    first = current(state)
    malformed = skill_source(
        tmp_path, "broken", body="---\nname: broken\n---\n\n# Broken\n"
    )
    assert run(state, "skill", "add", "owner/repo", str(malformed)) == 2
    assert "description" in capsys.readouterr().err

    empty = tmp_path / "empty-skill"
    empty.mkdir()
    assert run(state, "skill", "add", "owner/repo", str(empty)) == 2
    assert "SKILL.md" in capsys.readouterr().err
    assert current(state) == first


def test_skill_remove_requires_an_unreferenced_skill(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    assert (
        run(state, "skill", "add", "owner/repo", str(skill_source(tmp_path, "spare")))
        == 0
    )
    capsys.readouterr()

    assert run(state, "skill", "remove", "owner/repo", "reporting") == 2
    assert (
        'cannot remove skill "reporting": workflow task "reporting" still references it'
        in capsys.readouterr().err
    )
    referenced = current(state)
    assert {item["name"] for item in referenced.manifest["skills"]} >= {"reporting"}

    assert run(state, "skill", "remove", "owner/repo", "spare") == 0
    assert "Removed skill: spare" in capsys.readouterr().out
    removed = current(state)
    assert removed.generation == referenced.generation + 1
    assert "spare" not in {item["name"] for item in removed.manifest["skills"]}

    assert run(state, "skill", "remove", "owner/repo", "spare") == 2
    assert "is not installed" in capsys.readouterr().err


def test_tool_add_duplicate_replace_and_remove(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    source = tool_source(tmp_path)

    assert run(state, "tool", "add", "owner/repo", str(source)) == 0
    assert "Added tool: publish_report" in capsys.readouterr().out
    added = current(state)
    assert {item["name"] for item in added.manifest["scripts"]} == {
        "validate_release",
        "publish_report",
    }

    assert run(state, "tool", "add", "owner/repo", str(source)) == 2
    assert 'tool "publish_report" is already installed; use --replace' in (
        capsys.readouterr().err
    )
    assert current(state) == added

    (source / "tool.yaml").write_text(
        NEW_TOOL.replace("effect: read", "effect: mutate")
    )
    assert run(state, "tool", "add", "owner/repo", str(source), "--replace") == 0
    assert "Replaced tool: publish_report" in capsys.readouterr().out
    replaced = current(state)
    assert replaced.digest != added.digest
    assert [
        item["effect"]
        for item in replaced.manifest["scripts"]
        if item["name"] == "publish_report"
    ] == ["mutate"]

    assert run(state, "tool", "remove", "owner/repo", "publish_report") == 0
    assert "Removed tool: publish_report" in capsys.readouterr().out
    assert {item["name"] for item in current(state).manifest["scripts"]} == {
        "validate_release"
    }


def test_tool_remove_is_refused_while_the_workflow_references_it(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    first = current(state)
    assert run(state, "tool", "remove", "owner/repo", "validate_release") == 2
    assert (
        'cannot remove tool "validate_release": workflow task "readiness" still '
        "references it" in capsys.readouterr().err
    )
    assert current(state) == first

    assert run(state, "tool", "remove", "owner/repo", "missing_tool") == 2
    assert "is not installed" in capsys.readouterr().err
    assert current(state) == first


def test_invalid_script_tool_fails_atomically(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    first = current(state)
    broken = tool_source(
        tmp_path, "broken-tool", metadata=NEW_TOOL.replace("runtime: python", "")
    )
    assert run(state, "tool", "add", "owner/repo", str(broken)) == 2
    assert "runtime" in capsys.readouterr().err
    assert current(state) == first

    missing_entrypoint = tmp_path / "no-entrypoint"
    missing_entrypoint.mkdir()
    (missing_entrypoint / "tool.yaml").write_text(NEW_TOOL)
    assert run(state, "tool", "add", "owner/repo", str(missing_entrypoint)) == 2
    assert "entrypoint" in capsys.readouterr().err
    assert current(state) == first


def test_mcp_set_installs_remote_secret_headers_without_exposing_values(
    tmp_path, capsys
):
    state, _bundle = configured(tmp_path)
    store = SQLiteGitHubStore(state)
    RepoSecretStore(store, Fernet.generate_key()).set(
        1, "RELEASE_MCP_AUTH", "Bearer never-printed-value"
    )
    store.close()
    servers = tmp_path / "servers.yaml"
    servers.write_text(REMOTE_MCP)

    assert run(state, "mcp", "set", "owner/repo", str(servers)) == 0
    output = capsys.readouterr().out
    assert "Replaced MCP configuration: tools/mcp/servers.yaml" in output
    assert "never-printed-value" not in output
    assert "RELEASE_MCP_AUTH" not in output

    generation = current(state)
    assert generation.manifest["mcp"] == [
        {
            "server_id": "release-service",
            "connection": {
                "transport": "streamable_http",
                "url": "https://mcp.example.invalid/mcp",
                "headers": {"X-Client-Version": "sweforge"},
            },
            "tools": ["lookup_release"],
            "secret_env": {},
            "secret_headers": {"Authorization": "RELEASE_MCP_AUTH"},
        }
    ]

    assert root_main(["repo", "--state-db", str(state), "show", "owner/repo"]) == 0
    shown = capsys.readouterr().out
    assert json.loads(shown)["generation"] == generation.generation
    assert "never-printed-value" not in shown
    assert "RELEASE_MCP_AUTH" not in shown


def test_invalid_mcp_configuration_fails_atomically(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    first = current(state)
    servers = tmp_path / "servers.yaml"
    servers.write_text(REMOTE_MCP.replace("https://", "http://"))
    assert run(state, "mcp", "set", "owner/repo", str(servers)) == 2
    assert "HTTPS" in capsys.readouterr().err
    assert current(state) == first


def test_unrelated_mutations_preserve_secret_references_and_values(tmp_path, capsys):
    state, _bundle = configured(tmp_path)
    master_key = Fernet.generate_key()
    store = SQLiteGitHubStore(state)
    RepoSecretStore(store, master_key).set(
        1, "RELEASE_POLICY_TOKEN", "unchanged-secret-value"
    )
    store.close()

    servers = tmp_path / "servers.yaml"
    servers.write_text(REMOTE_MCP)
    assert run(state, "mcp", "set", "owner/repo", str(servers)) == 0
    assert (
        run(state, "skill", "add", "owner/repo", str(skill_source(tmp_path, "extra")))
        == 0
    )
    capsys.readouterr()

    generation = current(state)
    scripts = {item["name"]: item for item in generation.manifest["scripts"]}
    assert scripts["validate_release"]["secret_env"] == {
        "RELEASE_POLICY_TOKEN": "RELEASE_POLICY_TOKEN"
    }
    assert generation.manifest["mcp"][0]["secret_headers"] == {
        "Authorization": "RELEASE_MCP_AUTH"
    }
    store = SQLiteGitHubStore(state)
    reopened = RepoSecretStore(store, master_key)
    assert reopened.get(1, "RELEASE_POLICY_TOKEN").reveal() == "unchanged-secret-value"
    store.close()


def test_focused_mutations_use_installed_content_not_the_operator_source(
    tmp_path, capsys
):
    state, bundle = configured(tmp_path)
    first = current(state)
    (bundle / "workflow.yaml").write_text("not: a workflow\n")
    shutil.rmtree(bundle)
    assert not bundle.exists()

    assert (
        run(state, "skill", "add", "owner/repo", str(skill_source(tmp_path, "spare")))
        == 0
    )
    capsys.readouterr()
    second = current(state)

    assert second.generation == 2
    assert second.workflow_id == first.workflow_id
    registry = registry_for(state)
    installed = registry.generation_files(1, second.generation_id)
    assert set(installed) >= {
        "workflow.yaml",
        "skills/reporting/SKILL.md",
        "skills/spare/SKILL.md",
        "tools/scripts/validate-release/tool.yaml",
        "tools/scripts/validate-release/validate_release.py",
    }
    assert installed["workflow.yaml"] == (EXAMPLE / "workflow.yaml").read_text()


def test_focused_mutations_are_isolated_per_repository(tmp_path, capsys):
    state, _bundle = configured(tmp_path, repos=(("owner/repo", 1), ("other/repo", 2)))
    first = current(state, 2)
    assert (
        run(state, "skill", "add", "owner/repo", str(skill_source(tmp_path, "spare")))
        == 0
    )
    capsys.readouterr()

    assert current(state, 1).generation == 2
    assert current(state, 2) == first
    assert "spare" not in {
        item["name"] for item in current(state, 2).manifest["skills"]
    }


def test_focused_mutation_on_an_unconfigured_repository_fails_with_guidance(
    tmp_path, capsys
):
    state = state_with(tmp_path)
    source = skill_source(tmp_path, "spare")
    assert run(state, "skill", "add", "owner/repo", str(source)) == 2
    error = capsys.readouterr().err
    assert "repo configure" in error
    assert "repo init" in error

    assert run(state, "skill", "add", "missing/repo", str(source)) == 2
    assert "has not been observed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ("workflow", "set", "owner/repo", "missing.yaml"),
        ("mcp", "set", "owner/repo", "missing.yaml"),
        ("skill", "add", "owner/repo", "missing-dir"),
        ("tool", "add", "owner/repo", "missing-dir"),
    ],
)
def test_missing_source_paths_fail_before_any_generation_is_created(
    tmp_path, capsys, argv
):
    state, _bundle = configured(tmp_path)
    first = current(state)
    assert run(state, *argv) == 2
    assert capsys.readouterr().err
    assert current(state) == first
