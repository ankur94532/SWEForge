import shutil
from pathlib import Path

import pytest

from sweforge.github_models import SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config import RepoConfigRegistry, validate_repo_bundle

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"


def bundle_copy(tmp_path: Path, name: str = "bundle") -> Path:
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    return target


def event(repo_id: int, repo: str, number: int) -> SourceEvent:
    return SourceEvent(
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


def ingest(store: SQLiteGitHubStore, repo_id: int, repo: str, number: int) -> str:
    result = store.record_batch(
        repo_id,
        "issues",
        [event(repo_id, repo, number)],
        since="2026-01-01T00:00:00Z",
        etag=None,
        polled_at=f"2026-01-01T00:01:{number:02d}Z",
    )
    assert result.threads_created == 1
    return result.created_thread_ids[0]


def test_valid_bundle_installs_as_immutable_deterministic_generations(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(1, "owner/repo", "now")
    registry = RepoConfigRegistry(store)
    bundle = bundle_copy(tmp_path)

    first = registry.install(1, bundle, now="one")
    second = registry.install(1, bundle, now="two")

    assert first.generation == 1
    assert second.generation == 2
    assert first.generation_id != second.generation_id
    assert first.digest == second.digest == validate_repo_bundle(bundle).digest
    assert registry.current_generation(1) == second
    assert registry.load_workflow(1, first.generation_id).workflow_id == (
        "release-readiness"
    )
    assert registry.generation(1, first.generation_id) == first


def test_invalid_update_does_not_replace_current_generation(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(1, "owner/repo", "now")
    registry = RepoConfigRegistry(store)
    bundle = bundle_copy(tmp_path)
    first = registry.install(1, bundle, now="one")
    (bundle / "skills" / "reporting" / "SKILL.md").write_text(
        "---\nname: wrong\ndescription: mismatch\n---\n"
    )

    with pytest.raises(ValueError, match="does not match"):
        registry.install(1, bundle, now="two")

    assert registry.current_generation(1) == first
    assert (
        store.connection.execute(
            "SELECT count(*) FROM repo_config_generations_v1"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("changed", ["workflow", "skill", "script", "mcp"])
def test_behavioral_bundle_content_changes_digest(tmp_path, changed):
    first_path = bundle_copy(tmp_path, "first")
    second_path = bundle_copy(tmp_path, "second")
    if changed == "workflow":
        path = second_path / "workflow.yaml"
        path.write_text(path.read_text().replace("release-readiness", "release-v2"))
    elif changed == "skill":
        path = second_path / "skills" / "reporting" / "SKILL.md"
        path.write_text(path.read_text() + "\nPreserve a new field.\n")
    elif changed == "script":
        path = (
            second_path
            / "tools"
            / "scripts"
            / "validate-release"
            / "validate_release.py"
        )
        path.write_text(path.read_text() + "\n# version two\n")
    else:
        path = second_path / "tools" / "mcp" / "servers.yaml"
        path.write_text(path.read_text() + MCP_ENTRY)
    assert (
        validate_repo_bundle(first_path).digest
        != validate_repo_bundle(second_path).digest
    )


def test_issue_threads_freeze_generation_across_update_and_restart(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteGitHubStore(path)
    store.upsert_repository(1, "owner/repo", "now")
    registry = RepoConfigRegistry(store)
    bundle = bundle_copy(tmp_path)
    first = registry.install(1, bundle, now="one")
    thread_one = ingest(store, 1, "owner/repo", 1)
    skill_v1 = registry.read_skill(1, first.generation_id, "reporting")

    skill_path = bundle / "skills" / "reporting" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\nGeneration two instructions.\n")
    second = registry.install(1, bundle, now="two")
    thread_two = ingest(store, 1, "owner/repo", 2)

    assert registry.thread_generation(thread_one) == first
    assert registry.thread_generation(thread_two) == second
    assert registry.read_skill(1, first.generation_id, "reporting") == skill_v1
    assert "Generation two" in registry.read_skill(1, second.generation_id, "reporting")
    store.close()

    reopened = SQLiteGitHubStore(path)
    restored = RepoConfigRegistry(reopened)
    assert restored.thread_generation(thread_one) == first
    assert restored.thread_generation(thread_two) == second


def test_repo_isolation_covers_workflow_skills_scripts_and_mcp(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(1, "a/repo", "now")
    store.upsert_repository(2, "b/repo", "now")
    registry = RepoConfigRegistry(store)
    bundle_a = bundle_copy(tmp_path, "a")
    bundle_b = bundle_copy(tmp_path, "b")
    reporting_b = bundle_b / "skills" / "reporting" / "SKILL.md"
    reporting_b.write_text(reporting_b.read_text() + "\nREPO B ONLY\n")
    script_b = (
        bundle_b / "tools" / "scripts" / "validate-release" / "validate_release.py"
    )
    script_b.write_text(script_b.read_text() + "\n# REPO B SCRIPT\n")
    generation_a = registry.install(1, bundle_a, now="a")
    generation_b = registry.install(2, bundle_b, now="b")

    assert "REPO B ONLY" not in registry.read_skill(
        1, generation_a.generation_id, "reporting"
    )
    assert "REPO B ONLY" in registry.read_skill(
        2, generation_b.generation_id, "reporting"
    )
    spec_a = registry.script_specs(1, generation_a.generation_id)[0]
    spec_b = registry.script_specs(2, generation_b.generation_id)[0]
    assert registry.script_files(1, generation_a.generation_id, spec_a) != (
        registry.script_files(2, generation_b.generation_id, spec_b)
    )
    with pytest.raises(PermissionError):
        registry.generation(1, generation_b.generation_id)
    capabilities_a = registry.capability_registry(1, generation_a.generation_id)
    assert capabilities_a is not None
    assert capabilities_a.approved_servers(2) == {}


def test_cross_repo_or_unknown_authority_references_fail_install(tmp_path):
    bundle = bundle_copy(tmp_path)
    workflow = bundle / "workflow.yaml"
    workflow.write_text(
        workflow.read_text().replace(
            "tools: [ls, read_file, glob, grep, edit_file, execute]",
            "tools: [ls, read_file, repo_b_deploy]",
            1,
        )
    )
    with pytest.raises(ValueError, match="unknown tools"):
        validate_repo_bundle(bundle)


def test_missing_skill_mutating_phase_and_name_collision_fail_validation(tmp_path):
    missing = bundle_copy(tmp_path, "missing")
    shutil.rmtree(missing / "skills" / "reporting")
    with pytest.raises(ValueError, match="does not exist"):
        validate_repo_bundle(missing)

    mutating = bundle_copy(tmp_path, "mutating")
    tool = mutating / "tools" / "scripts" / "validate-release" / "tool.yaml"
    tool.write_text(tool.read_text().replace("effect: read", "effect: mutate"))
    with pytest.raises(ValueError, match="mutating script"):
        validate_repo_bundle(mutating)

    collision = bundle_copy(tmp_path, "collision")
    tool = collision / "tools" / "scripts" / "validate-release" / "tool.yaml"
    tool.write_text(tool.read_text().replace("name: validate_release", "name: execute"))
    with pytest.raises(ValueError, match="collision"):
        validate_repo_bundle(collision)


@pytest.mark.parametrize("failure", ["missing", "traversal", "symlink", "duplicate"])
def test_invalid_script_installation_fails_closed(tmp_path, failure):
    bundle = bundle_copy(tmp_path)
    directory = bundle / "tools" / "scripts" / "validate-release"
    metadata = directory / "tool.yaml"
    entrypoint = directory / "validate_release.py"
    if failure == "missing":
        entrypoint.unlink()
    elif failure == "traversal":
        metadata.write_text(
            metadata.read_text().replace(
                "entrypoint: validate_release.py", "entrypoint: ../outside.py"
            )
        )
    elif failure == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("print('outside')\n")
        entrypoint.unlink()
        entrypoint.symlink_to(outside)
    else:
        metadata.write_text(metadata.read_text() + "\nname: duplicate\n")

    with pytest.raises(ValueError):
        validate_repo_bundle(bundle)


def test_reference_bundle_is_valid_and_complete():
    bundle = validate_repo_bundle(EXAMPLE)
    assert [task.id for task in bundle.workflow.tasks] == [
        "model",
        "config",
        "readiness",
        "reporting",
    ]
    assert {item.name for item in bundle.skills} == {
        "domain-model",
        "config-loading",
        "readiness-rules",
        "release-catalog",
        "reporting",
    }
    assert [(item.name, item.runtime, item.effect) for item in bundle.scripts] == [
        ("validate_release", "python", "read"),
        ("write_readiness_report", "shell", "mutate"),
    ]
    assert {item.server_id: list(item.tools) for item in bundle.mcp_servers} == {
        "release_catalog": ["lookup_release", "list_release_windows"],
        "release_service": ["validate_release_window"],
    }
    readiness = bundle.workflow.task_map["readiness"]
    assert "release-catalog" in readiness.planning.skills
    assert "release_catalog_lookup_release" in readiness.planning.tools
    assert (
        "write_readiness_report"
        in bundle.workflow.task_map["reporting"].execution.tools
    )


def test_secret_env_collision_and_invalid_secret_names_are_rejected(tmp_path):
    collision = bundle_copy(tmp_path, "collision-env")
    tool = collision / "tools" / "scripts" / "validate-release" / "tool.yaml"
    tool.write_text(
        tool.read_text().replace(
            "RELEASE_REGION: example-region",
            "RELEASE_POLICY_TOKEN: example-region",
        )
    )
    with pytest.raises(ValueError, match="environment name twice"):
        validate_repo_bundle(collision)

    invalid = bundle_copy(tmp_path, "invalid-secret")
    tool = invalid / "tools" / "scripts" / "validate-release" / "tool.yaml"
    tool.write_text(
        tool.read_text().replace(
            "RELEASE_POLICY_TOKEN: RELEASE_POLICY_TOKEN",
            "RELEASE_POLICY_TOKEN: invalid-name",
        )
    )
    with pytest.raises(ValueError, match="secret name"):
        validate_repo_bundle(invalid)


MCP_ENTRY = """  extra:
    connection: {transport: stdio, command: server, args: []}
    tools: [inspect]
"""

REMOTE_MCP = """  remote:
    connection: {transport: streamable_http, url: https://example.invalid/mcp}
    tools: [lookup]
    headers: {X-Client-Version: sweforge}
    secret_headers: {Authorization: REMOTE_TOKEN}
"""


def write_mcp(tmp_path: Path, name: str, entry: str) -> Path:
    """Add one server to the reference bundle's own MCP configuration."""
    bundle = bundle_copy(tmp_path, name)
    path = bundle / "tools" / "mcp" / "servers.yaml"
    path.write_text(path.read_text() + entry)
    return bundle


def server_named(bundle, server_id: str):
    return next(item for item in bundle.mcp_servers if item.server_id == server_id)


def test_remote_mcp_secret_headers_are_accepted_and_frozen(tmp_path):
    bundle = validate_repo_bundle(write_mcp(tmp_path, "remote-ok", REMOTE_MCP))
    server = server_named(bundle, "remote")
    assert server.secret_headers == {"Authorization": "REMOTE_TOKEN"}
    assert server.connection["headers"] == {"X-Client-Version": "sweforge"}
    assert server.secret_env == {}
    assert [
        item["secret_headers"]
        for item in bundle.manifest["mcp"]
        if item["server_id"] == "remote"
    ] == [{"Authorization": "REMOTE_TOKEN"}]


def test_remote_mcp_secret_env_is_still_rejected(tmp_path):
    document = """  remote:
    connection: {transport: http, url: https://example.invalid/mcp}
    tools: [lookup]
    secret_env: {TOKEN: REMOTE_TOKEN}
"""
    with pytest.raises(ValueError, match="local stdio"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-env", document))


def test_fixed_and_secret_header_collision_is_rejected(tmp_path):
    document = REMOTE_MCP.replace(
        "headers: {X-Client-Version: sweforge}", "headers: {authorization: fixed}"
    )
    with pytest.raises(ValueError, match="HTTP header twice"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-collide", document))

    nested = REMOTE_MCP.replace(
        "url: https://example.invalid/mcp}",
        "url: https://example.invalid/mcp, headers: {x-client-version: other}}",
    )
    with pytest.raises(ValueError, match="HTTP header twice"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-collide-nested", nested))


def test_invalid_secret_header_reference_and_header_name_are_rejected(tmp_path):
    document = REMOTE_MCP.replace(
        "Authorization: REMOTE_TOKEN", "Authorization: bad-ref"
    )
    with pytest.raises(ValueError, match="secret name"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-bad-ref", document))

    malformed = REMOTE_MCP.replace("X-Client-Version: sweforge", '"Bad Header": value')
    with pytest.raises(ValueError, match="HTTP header"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-bad-header", malformed))


def test_secret_headers_require_https_and_a_remote_transport(tmp_path):
    plaintext = REMOTE_MCP.replace("https://example.invalid", "http://example.invalid")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-plain", plaintext))

    document = """  local:
    connection: {transport: stdio, command: server, args: []}
    tools: [lookup]
    secret_headers: {Authorization: REMOTE_TOKEN}
"""
    with pytest.raises(ValueError, match="HTTP-based remote transport"):
        validate_repo_bundle(write_mcp(tmp_path, "remote-stdio", document))


def test_secret_header_reference_changes_digest_but_fixed_values_are_frozen(tmp_path):
    baseline = validate_repo_bundle(write_mcp(tmp_path, "digest-one", REMOTE_MCP))
    same = validate_repo_bundle(write_mcp(tmp_path, "digest-two", REMOTE_MCP))
    assert baseline.digest == same.digest

    rotated_reference = REMOTE_MCP.replace("REMOTE_TOKEN", "ROTATED_TOKEN")
    assert (
        validate_repo_bundle(
            write_mcp(tmp_path, "digest-ref", rotated_reference)
        ).digest
        != baseline.digest
    )
