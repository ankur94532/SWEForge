import json
import shutil
from pathlib import Path

from cryptography.fernet import Fernet

from sweforge.cli import main as root_main
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config_cli import main
from sweforge.repo_secrets import RepoSecretStore

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"


def observed_state(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteGitHubStore(path)
    store.upsert_repository(1, "owner/repo", "now")
    store.close()
    return path


def test_repo_init_creates_uninstalled_valid_starter(tmp_path, capsys):
    state = observed_state(tmp_path)
    target = tmp_path / "starter"
    assert (
        root_main(
            [
                "repo",
                "--state-db",
                str(state),
                "init",
                "owner/repo",
                "--output",
                str(target),
            ]
        )
        == 0
    )
    assert (target / "workflow.yaml").is_file()
    assert (target / "skills" / "implementation" / "SKILL.md").is_file()
    assert (target / "tools" / "scripts").is_dir()
    store = SQLiteGitHubStore(state)
    assert (
        store.connection.execute(
            "SELECT count(*) FROM repo_config_generations_v1"
        ).fetchone()[0]
        == 0
    )
    assert str(target) in capsys.readouterr().out


def test_repo_configure_validate_and_show_safe_metadata(tmp_path, capsys):
    state = observed_state(tmp_path)
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)

    assert main(["--state-db", str(state), "validate", "owner/repo", str(bundle)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True
    assert main(["--state-db", str(state), "configure", "owner/repo", str(bundle)]) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["generation"] == 1
    assert main(["--state-db", str(state), "show", "owner/repo"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["workflow"]["id"] == "release-readiness"
    assert [item["name"] for item in shown["scripts"]] == [
        "validate_release",
        "write_readiness_report",
    ]
    assert [item["server"] for item in shown["mcp"]] == [
        "release_catalog",
        "release_service",
    ]
    assert "SKILL.md" not in json.dumps(shown)
    assert "validate_release.py" not in json.dumps(shown)


def test_bad_cli_configure_keeps_previous_generation(tmp_path, capsys):
    state = observed_state(tmp_path)
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    assert main(["--state-db", str(state), "configure", "owner/repo", str(bundle)]) == 0
    capsys.readouterr()
    (bundle / "workflow.yaml").write_text("not: a workflow\n")

    assert main(["--state-db", str(state), "configure", "owner/repo", str(bundle)]) == 2

    store = SQLiteGitHubStore(state)
    current = store.connection.execute(
        "SELECT generation_id FROM repo_config_current_v1 WHERE repo_id=1"
    ).fetchone()
    assert current is not None
    assert (
        store.connection.execute(
            "SELECT count(*) FROM repo_config_generations_v1"
        ).fetchone()[0]
        == 1
    )
    assert "workflow" in capsys.readouterr().err


def test_repo_show_never_reveals_remote_mcp_credentials(tmp_path, capsys):
    state = observed_state(tmp_path)
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    servers = bundle / "tools" / "mcp" / "servers.yaml"
    servers.write_text(
        servers.read_text()
        + """  extra_service:
    connection:
      transport: streamable_http
      url: https://mcp.example.invalid/mcp
    headers:
      X-Client-Version: sweforge
    secret_headers:
      Authorization: RELEASE_MCP_AUTH
    tools: [lookup_release]
"""
    )
    store = SQLiteGitHubStore(state)
    RepoSecretStore(store, Fernet.generate_key()).set(
        1, "RELEASE_MCP_AUTH", "Bearer never-printed-value"
    )
    store.close()

    assert main(["--state-db", str(state), "configure", "owner/repo", str(bundle)]) == 0
    capsys.readouterr()
    assert main(["--state-db", str(state), "show", "owner/repo"]) == 0
    out = capsys.readouterr().out
    shown = json.loads(out)

    assert {item["server"]: item for item in shown["mcp"]}["extra_service"] == {
        "server": "extra_service",
        "tools": ["lookup_release"],
        "credentials": {"required": 1, "configured": 1},
    }
    assert "never-printed-value" not in out
    assert "Authorization" not in out
    assert "RELEASE_MCP_AUTH" not in out
    assert "mcp.example.invalid" not in out
