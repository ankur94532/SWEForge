import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sweforge.github_auth import (
    POLL_READ,
    REPO_WRITE,
    GitHubAppAuthenticator,
    GitHubAppConfigurationError,
)
from sweforge.github_client import HttpxGitHubClient
from sweforge.github_errors import GitHubAPIError
from sweforge.github_store import SQLiteGitHubStore

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "test-app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path, key.public_key()


def test_jwt_is_rs256_with_expected_claims_and_lifetime(key_path):
    path, public_key = key_path
    auth = GitHubAppAuthenticator(123, path, now=lambda: NOW)
    token = auth.app_jwt()

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "iat": int(NOW.timestamp()) - 60,
        "exp": int((NOW + timedelta(minutes=9)).timestamp()),
        "iss": "123",
    }
    assert claims["exp"] - claims["iat"] <= 600
    auth.close()


def test_missing_and_invalid_key_errors_do_not_expose_contents(tmp_path):
    missing = GitHubAppAuthenticator(1, tmp_path / "missing.pem", now=lambda: NOW)
    with pytest.raises(ValueError, match="readable file") as error:
        missing.app_jwt()
    assert "missing.pem" not in str(error.value)

    secret = b"not-a-real-private-key"
    invalid_path = tmp_path / "invalid.pem"
    invalid_path.write_bytes(secret)
    invalid = GitHubAppAuthenticator(1, invalid_path, now=lambda: NOW)
    with pytest.raises(ValueError, match="private key is invalid") as error:
        invalid.app_jwt()
    assert secret.decode() not in str(error.value)


def test_client_id_is_preferred_and_app_id_is_fallback(key_path):
    path, public_key = key_path
    client_id_auth = GitHubAppAuthenticator(
        123, path, client_id="Iv1.client", now=lambda: NOW
    )
    client_id_claims = jwt.decode(
        client_id_auth.app_jwt(),
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert client_id_claims["iss"] == "Iv1.client"
    client_id_auth.close()

    app_id_auth = GitHubAppAuthenticator(123, path, now=lambda: NOW)
    app_id_claims = jwt.decode(
        app_id_auth.app_jwt(),
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert app_id_claims["iss"] == "123"
    app_id_auth.close()


def test_neither_app_identifier_is_a_clear_configuration_error(key_path):
    path, _ = key_path
    with pytest.raises(GitHubAppConfigurationError, match="Client ID or App ID"):
        GitHubAppAuthenticator(None, path)


def test_installations_and_tokens_are_cached_and_repository_scoped(key_path):
    path, _ = key_path
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(
            201,
            json={
                "token": "installation-token-value",
                "expires_at": "2026-08-21T13:00:00Z",
            },
        )

    auth = GitHubAppAuthenticator(
        123,
        path,
        now=lambda: NOW,
        transport=httpx.MockTransport(handler),
    )
    first = auth.token_for("owner/one", POLL_READ)
    assert first == "installation-token-value"
    assert auth.token_for("owner/one", POLL_READ) == first
    assert auth.token_for("owner/two", POLL_READ) == "installation-token-value"
    assert auth.token_for("owner/one", REPO_WRITE) == "installation-token-value"

    installation_calls = [
        request for request in requests if request.url.path.endswith("/installation")
    ]
    token_calls = [
        request for request in requests if request.url.path.endswith("/access_tokens")
    ]
    assert len(installation_calls) == 2
    assert len(token_calls) == 3
    assert all(
        request.headers["authorization"].startswith("Bearer ey") for request in requests
    )
    assert [request.url.path for request in token_calls] == [
        "/app/installations/77/access_tokens",
        "/app/installations/77/access_tokens",
        "/app/installations/77/access_tokens",
    ]
    assert token_calls[0].content.find(b'"repositories":["one"]') >= 0
    assert token_calls[1].content.find(b'"repositories":["two"]') >= 0
    assert b'"contents":"read"' in token_calls[0].content
    auth.close()


def test_token_near_expiry_refreshes_and_missing_installation_is_safe(key_path):
    path, _ = key_path
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 4})
        return httpx.Response(
            201,
            json={
                "token": f"installation-token-{len(calls)}",
                "expires_at": "2026-08-21T12:04:00Z",
            },
        )

    auth = GitHubAppAuthenticator(
        1, path, now=lambda: NOW, transport=httpx.MockTransport(handler)
    )
    assert auth.token_for("owner/repo", POLL_READ) == "installation-token-2"
    assert auth.token_for("owner/repo", POLL_READ) == "installation-token-3"
    auth.close()

    missing = GitHubAppAuthenticator(
        1,
        path,
        now=lambda: NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    with pytest.raises(GitHubAPIError, match="not installed for owner/repo") as error:
        missing.token_for("owner/repo")
    assert "Bearer" not in str(error.value)
    missing.close()


def test_client_requests_use_provider_for_repository_and_pagination(key_path):
    path, _ = key_path
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 9})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token-client",
                    "expires_at": "2026-08-21T13:00:00Z",
                },
            )
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 1, "full_name": "owner/repo"})
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2}])
        return httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"link": '<https://api.example/issues?page=2>; rel="next"'},
        )

    auth = GitHubAppAuthenticator(
        1, path, now=lambda: NOW, transport=httpx.MockTransport(handler)
    )
    client = HttpxGitHubClient(
        token_provider=auth, transport=httpx.MockTransport(handler)
    )
    repo = client.repository("owner/repo")
    response = client.issues(repo, "2026-01-01T00:00:00Z", None)
    assert [item["id"] for item in response.items] == [1, 2]
    assert all(
        request.headers["authorization"].startswith("Bearer ") for request in requests
    )
    client.close()
    auth.close()


def test_auth_tokens_are_not_stored_in_github_sqlite_store(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    table_names = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "github_tokens" not in table_names
    with pytest.raises(sqlite3.OperationalError):
        store.connection.execute("SELECT token FROM github_tokens")
    store.close()


def test_api_errors_never_include_authorization_header():
    secret = "installation-token-secret"
    client = HttpxGitHubClient(
        secret,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with pytest.raises(GitHubAPIError) as error:
        client.repository("owner/repo")
    assert secret not in str(error.value)
    assert "Authorization" not in str(error.value)
    client.close()
