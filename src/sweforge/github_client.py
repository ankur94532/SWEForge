"""Small explicit GitHub REST client used by the polling boundary."""

from collections.abc import Iterator
from typing import Protocol

import httpx

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


class GitHubAPIError(RuntimeError):
    """A safe GitHub API error that does not include response bodies."""


class HttpxGitHubClient:
    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self) -> None:
        self._client.close()

    def repository(self, full_name: str) -> RepositoryRef:
        data = self._request("GET", f"/repos/{full_name}").json()
        return RepositoryRef(repo_id=int(data["id"]), full_name=data["full_name"])

    def issue(self, repo: RepositoryRef, number: int) -> dict:
        return self._request("GET", f"/repos/{repo.full_name}/issues/{number}").json()

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
            "GET", f"/repos/{repo.full_name}/{endpoint}", params=params, etag=etag
        )
        if first.status_code == 304:
            return PollResponse(etag=etag, not_modified=True)
        items = list(self._pages(first))
        return PollResponse(items=tuple(items), etag=first.headers.get("etag"))

    def _pages(self, first: httpx.Response) -> Iterator[dict]:
        response = first
        while True:
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubAPIError("GitHub returned an unexpected list response")
            yield from payload
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return
            response = self._request("GET", next_url)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        etag: str | None = None,
    ) -> httpx.Response:
        headers = {"If-None-Match": etag} if etag else None
        response = self._client.request(method, url, params=params, headers=headers)
        if response.status_code == 304:
            return response
        if response.is_error:
            raise GitHubAPIError(
                f"GitHub request failed with HTTP {response.status_code}"
            )
        return response
