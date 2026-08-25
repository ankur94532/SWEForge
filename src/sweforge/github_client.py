"""Small explicit GitHub REST client used by the polling boundary."""

from collections.abc import Iterator
from typing import Protocol

import httpx

from .github_auth import (
    DEFAULT_API_VERSION,
    POLL_READ,
    REPO_WRITE,
    GitHubTokenProvider,
    StaticGitHubTokenProvider,
)
from .github_errors import GitHubAPIError
from .github_models import PollResponse, RepositoryRef


class GitHubClient(Protocol):
    def repository(self, full_name: str) -> RepositoryRef: ...

    def issues(
        self, repo: RepositoryRef, since: str, etag: str | None
    ) -> PollResponse: ...

    def issue_comments(
        self, repo: RepositoryRef, since: str, etag: str | None
    ) -> PollResponse: ...

    def review_comments(
        self, repo: RepositoryRef, since: str, etag: str | None
    ) -> PollResponse: ...

    def issue(self, repo: RepositoryRef, number: int) -> dict: ...

    def collaborator_permission(self, repo: RepositoryRef, login: str) -> str: ...

    def pull_requests(
        self, repo: RepositoryRef, *, head: str, base: str
    ) -> list[dict]: ...

    def create_pull_request(
        self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str
    ) -> dict: ...

    def comments(self, repo: RepositoryRef, number: int) -> list[dict]: ...

    def create_comment(self, repo: RepositoryRef, number: int, body: str) -> dict: ...

    def review_comments_for_pull_request(
        self, repo: RepositoryRef, number: int
    ) -> list[dict]: ...

    def create_review_comment_reply(
        self, repo: RepositoryRef, pull_number: int, comment_id: int, body: str
    ) -> dict: ...


class HttpxGitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        token_provider: GitHubTokenProvider | None = None,
        api_url: str = "https://api.github.com",
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if token_provider is not None and token is not None:
            raise ValueError("provide token or token_provider, not both")
        self._token_provider = token_provider or StaticGitHubTokenProvider(token or "")
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": api_version,
            },
        )

    def close(self) -> None:
        self._client.close()

    def repository(self, full_name: str) -> RepositoryRef:
        data = self._request("GET", f"/repos/{full_name}", token_scope=full_name).json()
        return RepositoryRef(
            repo_id=int(data["id"]),
            full_name=data["full_name"],
            default_branch=data.get("default_branch", "main"),
        )

    def issue(self, repo: RepositoryRef, number: int) -> dict:
        return self._request(
            "GET",
            f"/repos/{repo.full_name}/issues/{number}",
            token_scope=repo.full_name,
        ).json()

    def collaborator_permission(self, repo: RepositoryRef, login: str) -> str:
        """Return GitHub's permission level for a login on a repository.

        One of "admin", "maintain", "write", "triage", "read", or "none".
        Callers treat any failure as unauthorized rather than guessing.
        """
        payload = self._request(
            "GET",
            f"/repos/{repo.full_name}/collaborators/{login}/permission",
            token_scope=repo.full_name,
        ).json()
        return str(payload.get("permission", "none"))

    def issues(self, repo: RepositoryRef, since: str, etag: str | None) -> PollResponse:
        return self._poll(repo, "issues", since, etag)

    def issue_comments(
        self, repo: RepositoryRef, since: str, etag: str | None
    ) -> PollResponse:
        return self._poll(repo, "issues/comments", since, etag)

    def review_comments(
        self, repo: RepositoryRef, since: str, etag: str | None
    ) -> PollResponse:
        return self._poll(repo, "pulls/comments", since, etag)

    def pull_requests(self, repo: RepositoryRef, *, head: str, base: str) -> list[dict]:
        response = self._request(
            "GET",
            f"/repos/{repo.full_name}/pulls",
            params={
                "state": "all",
                "head": f"{repo.full_name.split('/')[0]}:{head}",
                "base": base,
                "per_page": "100",
            },
            token_scope=repo.full_name,
            profile=REPO_WRITE,
        )
        return list(self._pages(response, repo.full_name, REPO_WRITE))

    def create_pull_request(
        self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str
    ) -> dict:
        return self._request(
            "POST",
            f"/repos/{repo.full_name}/pulls",
            token_scope=repo.full_name,
            profile=REPO_WRITE,
            json={"head": head, "base": base, "title": title, "body": body},
        ).json()

    def comments(self, repo: RepositoryRef, number: int) -> list[dict]:
        response = self._request(
            "GET",
            f"/repos/{repo.full_name}/issues/{number}/comments",
            params={"per_page": "100"},
            token_scope=repo.full_name,
            profile=REPO_WRITE,
        )
        return list(self._pages(response, repo.full_name, REPO_WRITE))

    def create_comment(self, repo: RepositoryRef, number: int, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{repo.full_name}/issues/{number}/comments",
            token_scope=repo.full_name,
            profile=REPO_WRITE,
            json={"body": body},
        ).json()

    def review_comments_for_pull_request(
        self, repo: RepositoryRef, number: int
    ) -> list[dict]:
        response = self._request(
            "GET",
            f"/repos/{repo.full_name}/pulls/{number}/comments",
            params={"per_page": "100"},
            token_scope=repo.full_name,
            profile=REPO_WRITE,
        )
        return list(self._pages(response, repo.full_name, REPO_WRITE))

    def create_review_comment_reply(
        self, repo: RepositoryRef, pull_number: int, comment_id: int, body: str
    ) -> dict:
        return self._request(
            "POST",
            f"/repos/{repo.full_name}/pulls/{pull_number}/comments/{comment_id}/replies",
            token_scope=repo.full_name,
            profile=REPO_WRITE,
            json={"body": body},
        ).json()

    def _poll(
        self, repo: RepositoryRef, endpoint: str, since: str, etag: str | None
    ) -> PollResponse:
        params = {
            "since": since,
            "sort": "updated",
            "direction": "asc",
            "per_page": "100",
        }
        first = self._request(
            "GET",
            f"/repos/{repo.full_name}/{endpoint}",
            params=params,
            etag=etag,
            token_scope=repo.full_name,
        )
        if first.status_code == 304:
            return PollResponse(etag=etag, not_modified=True)
        items = list(self._pages(first, repo.full_name))
        return PollResponse(items=tuple(items), etag=first.headers.get("etag"))

    def _pages(
        self, first: httpx.Response, repository: str, profile=POLL_READ
    ) -> Iterator[dict]:
        response = first
        while True:
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubAPIError("GitHub returned an unexpected list response")
            yield from payload
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return
            response = self._request(
                "GET", next_url, token_scope=repository, profile=profile
            )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        etag: str | None = None,
        token_scope: str,
        profile=POLL_READ,
        json: dict | None = None,
    ) -> httpx.Response:
        headers = {"If-None-Match": etag} if etag else None
        token = self._token_provider.token_for(token_scope, profile)
        response = self._client.request(
            method,
            url,
            params=params,
            headers={**(headers or {}), "Authorization": f"Bearer {token}"},
            json=json,
        )
        if response.status_code == 304:
            return response
        if response.is_error:
            raise GitHubAPIError(
                f"GitHub request failed with HTTP {response.status_code}"
            )
        return response
