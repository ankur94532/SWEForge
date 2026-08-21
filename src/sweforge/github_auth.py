"""GitHub App authentication and short-lived installation-token handling."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from .github_errors import GitHubAPIError

DEFAULT_API_VERSION = "2026-03-10"
TOKEN_REFRESH_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class PermissionProfile:
    """A named set of permissions requested for an installation token."""

    name: str
    permissions: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, str]:
        return dict(self.permissions)


POLL_READ = PermissionProfile(
    "poll-read",
    (("contents", "read"), ("issues", "read"), ("pull_requests", "read")),
)
REPO_WRITE = PermissionProfile(
    "repo-write",
    (("contents", "write"), ("issues", "write"), ("pull_requests", "write")),
)


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime


class GitHubTokenProvider(Protocol):
    def token_for(self, repository: str, profile: PermissionProfile) -> str: ...


class GitHubAppConfigurationError(ValueError):
    """Raised for missing or invalid local GitHub App configuration."""


class StaticGitHubTokenProvider:
    """Legacy PAT adapter used only when App credentials are not configured."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GitHub token must not be empty")
        self._token = token

    def token_for(self, repository: str, profile: PermissionProfile) -> str:
        del repository, profile
        return self._token


class GitHubAppAuthenticator:
    """Mint and cache repository-scoped read tokens for a GitHub App."""

    def __init__(
        self,
        app_id: str | int | None,
        private_key_path: str | Path,
        *,
        client_id: str | None = None,
        api_url: str = "https://api.github.com",
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 20.0,
        now: Callable[[], datetime] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if app_id is None and not client_id:
            raise GitHubAppConfigurationError(
                "GitHub App Client ID or App ID is required"
            )
        self.app_id = str(app_id) if app_id is not None else None
        self.private_key_path = Path(private_key_path).expanduser()
        self.issuer = client_id or self.app_id
        self._now = now or (lambda: datetime.now(UTC))
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": api_version,
            },
        )
        self._private_key: bytes | None = None
        self._jwt: tuple[str, datetime] | None = None
        self._installations: dict[str, int] = {}
        self._tokens: dict[
            tuple[int, str, tuple[tuple[str, str], ...]], InstallationToken
        ] = {}

    def close(self) -> None:
        self._client.close()

    def app_jwt(self) -> str:
        now = self._aware_now()
        if self._jwt and now < self._jwt[1] - TOKEN_REFRESH_WINDOW:
            return self._jwt[0]
        issued_at = int(now.timestamp()) - 60
        expires_at = now + timedelta(minutes=9)
        try:
            token = jwt.encode(
                {
                    "iat": issued_at,
                    "exp": int(expires_at.timestamp()),
                    "iss": self.issuer,
                },
                self._load_private_key(),
                algorithm="RS256",
            )
        except GitHubAppConfigurationError:
            raise
        except Exception as exc:
            raise GitHubAppConfigurationError(
                "GitHub App private key is invalid"
            ) from exc
        self._jwt = (token, expires_at)
        return token

    def installation_id(self, repository: str) -> int:
        repository = _validate_repository(repository)
        if repository in self._installations:
            return self._installations[repository]
        response = self._request(
            "GET",
            f"/repos/{repository}/installation",
            bearer=self.app_jwt(),
        )
        if response.status_code == 404:
            raise GitHubAPIError(
                f"SWEForge GitHub App is not installed for {repository}"
            )
        try:
            installation_id = int(response.json()["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAPIError(
                "GitHub returned an invalid installation response"
            ) from exc
        self._installations[repository] = installation_id
        return installation_id

    def token_for(self, repository: str, profile: PermissionProfile = POLL_READ) -> str:
        repository = _validate_repository(repository)
        installation_id = self.installation_id(repository)
        key = (installation_id, repository, profile.permissions)
        now = self._aware_now()
        cached = self._tokens.get(key)
        if cached and now < cached.expires_at - TOKEN_REFRESH_WINDOW:
            return cached.token
        response = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer=self.app_jwt(),
            json={
                "repositories": [repository.split("/", 1)[1]],
                "permissions": profile.as_dict(),
            },
        )
        try:
            payload = response.json()
            token = payload["token"]
            expires_at = _parse_expiry(payload["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAPIError(
                "GitHub returned an invalid installation token response"
            ) from exc
        if not isinstance(token, str) or not token:
            raise GitHubAPIError("GitHub returned an invalid installation token")
        self._tokens[key] = InstallationToken(token, expires_at)
        return token

    def _load_private_key(self) -> bytes:
        if self._private_key is not None:
            return self._private_key
        path = self.private_key_path
        if not path.is_file() or not os.access(path, os.R_OK):
            raise GitHubAppConfigurationError(
                "GitHub App private key path must identify a readable file"
            )
        try:
            self._private_key = path.read_bytes()
        except OSError as exc:
            raise GitHubAppConfigurationError(
                "GitHub App private key could not be read"
            ) from exc
        return self._private_key

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("GitHub App clock must return an aware datetime")
        return now.astimezone(UTC)

    def _request(
        self,
        method: str,
        url: str,
        *,
        bearer: str,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        response = self._client.request(
            method,
            url,
            json=json,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if response.is_error:
            if response.status_code == 404:
                return response
            raise GitHubAPIError(
                f"GitHub request failed with HTTP {response.status_code}"
            )
        return response


def _validate_repository(repository: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be in owner/name form")
    return repository


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expires_at must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
