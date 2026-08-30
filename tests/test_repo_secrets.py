import pytest
from cryptography.fernet import Fernet

from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_secrets import RepoSecretStore, SecretValue, load_secret_master_key


def stores(tmp_path):
    path = tmp_path / "state.db"
    state = SQLiteGitHubStore(path)
    state.upsert_repository(1, "a/repo", "now")
    state.upsert_repository(2, "b/repo", "now")
    key = Fernet.generate_key()
    return path, state, RepoSecretStore(state, key), key


def test_secret_store_encrypts_at_rest_lists_names_and_redacts_repr(tmp_path):
    path, state, secrets, _key = stores(tmp_path)
    value = "repo-a-super-secret-value"
    secrets.set(1, "DEPLOY_TOKEN", value, now="one")

    resolved = secrets.get(1, "DEPLOY_TOKEN")
    assert isinstance(resolved, SecretValue)
    assert resolved.reveal() == value
    assert value not in repr(resolved)
    assert value not in str(resolved)
    assert secrets.list_names(1) == ("DEPLOY_TOKEN",)
    row = state.connection.execute(
        "SELECT ciphertext FROM repo_secrets_v1 WHERE repo_id=1"
    ).fetchone()
    assert value.encode() not in bytes(row["ciphertext"])
    state.connection.commit()
    assert value.encode() not in path.read_bytes()
    audit = state.connection.execute(
        "SELECT operation,name,subject FROM repo_secret_audit_v1"
    ).fetchall()
    assert [tuple(item) for item in audit] == [("SET", "DEPLOY_TOKEN", None)]
    assert value not in str([dict(item) for item in audit])


def test_secret_store_is_repo_scoped_and_same_names_are_independent(tmp_path):
    _path, _state, secrets, _key = stores(tmp_path)
    secrets.set(1, "TOKEN", "repository-a-token")
    secrets.set(2, "TOKEN", "repository-b-token")

    assert secrets.get(1, "TOKEN").reveal() == "repository-a-token"
    assert secrets.get(2, "TOKEN").reveal() == "repository-b-token"
    assert secrets.get(1, "OTHER") is None
    assert secrets.list_names(1) == ("TOKEN",)


def test_secret_store_delete_and_restart_with_same_master_key(tmp_path):
    path, state, secrets, key = stores(tmp_path)
    secrets.set(1, "TOKEN", "persisted-token-value")
    state.close()

    reopened = SQLiteGitHubStore(path)
    restored = RepoSecretStore(reopened, key)
    assert restored.get(1, "TOKEN").reveal() == "persisted-token-value"
    assert restored.delete(1, "TOKEN", now="delete") is True
    assert restored.get(1, "TOKEN") is None
    assert restored.delete(1, "TOKEN") is False


def test_wrong_master_key_and_short_values_fail_safely(tmp_path):
    path, state, secrets, _key = stores(tmp_path)
    with pytest.raises(ValueError, match="at least"):
        secrets.set(1, "TOKEN", "short")
    secrets.set(1, "TOKEN", "long-enough-secret")
    state.close()

    reopened = SQLiteGitHubStore(path)
    wrong = RepoSecretStore(reopened, Fernet.generate_key())
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        wrong.get(1, "TOKEN")


def test_resolve_env_loads_only_declared_references_and_audits_metadata(tmp_path):
    _path, state, secrets, _key = stores(tmp_path)
    secrets.set(1, "TOKEN_A", "declared-secret-a")
    secrets.set(1, "TOKEN_B", "unrelated-secret-b")

    resolved = secrets.resolve_env(
        1, {"TOOL_TOKEN": "TOKEN_A"}, subject="script:deploy", now="resolve"
    )

    assert set(resolved) == {"TOOL_TOKEN"}
    assert resolved["TOOL_TOKEN"].reveal() == "declared-secret-a"
    audit = state.connection.execute(
        "SELECT operation,subject,count FROM repo_secret_audit_v1 "
        "ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit) == ("RESOLVE", "script:deploy", 1)


def test_missing_required_reference_fails_before_resolution(tmp_path):
    _path, _state, secrets, _key = stores(tmp_path)
    with pytest.raises(PermissionError, match="not configured"):
        secrets.resolve_env(1, {"TOOL_TOKEN": "MISSING_TOKEN"}, subject="script:deploy")


def test_master_key_file_must_have_private_permissions(tmp_path, monkeypatch):
    key_file = tmp_path / "secret.key"
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    monkeypatch.delenv("SWEFORGE_SECRET_MASTER_KEY", raising=False)
    monkeypatch.setenv("SWEFORGE_SECRET_MASTER_KEY_FILE", str(key_file))

    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_secret_master_key()
    key_file.chmod(0o600)
    assert load_secret_master_key() == key
