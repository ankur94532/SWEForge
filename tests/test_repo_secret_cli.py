import io
import json
import shutil
from pathlib import Path

from cryptography.fernet import Fernet

from sweforge.cli import main as root_main
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_config import RepoConfigRegistry
from sweforge.repo_secret_cli import main

EXAMPLE = Path(__file__).parents[1] / "examples" / "repo-config"

# Every credential the reference bundle references besides the script tool's
# own RELEASE_POLICY_TOKEN: one stdio MCP server and one remote HTTP server.
REMAINING_REFERENCES = (
    "RELEASE_CATALOG_TOKEN",
    "RELEASE_SERVICE_AUTH",
    "RELEASE_SERVICE_INTERNAL_TOKEN",
)


def configured(tmp_path):
    state_path = tmp_path / "state.db"
    state = SQLiteGitHubStore(state_path)
    state.upsert_repository(1, "owner/repo", "now")
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    RepoConfigRegistry(state).install(1, bundle, now="now")
    state.close()
    return state_path


def test_secret_cli_set_list_check_delete_never_displays_values(
    tmp_path, monkeypatch, capsys
):
    state = configured(tmp_path)
    value = "operator-secret-value"
    monkeypatch.setenv("SWEFORGE_SECRET_MASTER_KEY", Fernet.generate_key().decode())

    assert main(["--state-db", str(state), "check", "owner/repo"]) == 1
    missing = capsys.readouterr().out
    assert "RELEASE_POLICY_TOKEN\tmissing" in missing
    assert "RELEASE_CATALOG_TOKEN\tmissing" in missing
    assert "RELEASE_SERVICE_AUTH\tmissing" in missing
    monkeypatch.setattr("sys.stdin", io.StringIO(value + "\n"))
    assert (
        root_main(
            [
                "secret",
                "--state-db",
                str(state),
                "set",
                "owner/repo",
                "RELEASE_POLICY_TOKEN",
                "--stdin",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "RELEASE_POLICY_TOKEN\tconfigured" in output
    assert value not in output

    assert main(["--state-db", str(state), "list", "owner/repo"]) == 0
    output = capsys.readouterr().out
    assert output == "RELEASE_POLICY_TOKEN\tconfigured\n"
    assert value not in output

    for name in REMAINING_REFERENCES:
        monkeypatch.setattr("sys.stdin", io.StringIO(value + "\n"))
        assert (
            main(["--state-db", str(state), "set", "owner/repo", name, "--stdin"]) == 0
        )
    capsys.readouterr()
    assert main(["--state-db", str(state), "check", "owner/repo"]) == 0
    assert "All required" in capsys.readouterr().out

    assert root_main(["repo", "--state-db", str(state), "show", "owner/repo"]) == 0
    shown_text = capsys.readouterr().out
    shown = json.loads(shown_text)
    assert shown["scripts"][0]["credentials"] == {
        "configured": 1,
        "required": 1,
    }
    assert value not in shown_text

    assert (
        main(
            [
                "--state-db",
                str(state),
                "delete",
                "owner/repo",
                "RELEASE_POLICY_TOKEN",
            ]
        )
        == 0
    )
    assert value not in capsys.readouterr().out


def test_secret_check_covers_script_stdio_and_remote_mcp_references(
    tmp_path, monkeypatch, capsys
):
    state = configured(tmp_path)
    monkeypatch.setenv("SWEFORGE_SECRET_MASTER_KEY", Fernet.generate_key().decode())

    assert main(["--state-db", str(state), "check", "owner/repo"]) == 1
    reported = {line.split("\t")[0] for line in capsys.readouterr().out.splitlines()}

    assert reported == {"RELEASE_POLICY_TOKEN", *REMAINING_REFERENCES}
