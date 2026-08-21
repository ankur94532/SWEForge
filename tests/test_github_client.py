import httpx

from sweforge.github_client import HttpxGitHubClient


def test_http_client_headers_pagination_and_304():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/example/repo":
            return httpx.Response(200, json={"id": 123, "full_name": "example/repo"})
        if request.url.path.endswith("/issues") and request.headers.get(
            "if-none-match"
        ):
            return httpx.Response(304, headers={"etag": "etag-1"})
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2}])
        return httpx.Response(
            200,
            json=[{"id": 1}],
            headers={
                "etag": "etag-1",
                "link": (
                    '<https://api.example/repos/example/repo/issues?page=2>; rel="next"'
                ),
            },
        )

    client = HttpxGitHubClient(
        "token-value",
        api_url="https://api.example",
        transport=httpx.MockTransport(handler),
    )
    repo = client.repository("example/repo")
    response = client.issues(repo, "2026-01-01T00:00:00Z", None)
    assert [item["id"] for item in response.items] == [1, 2]
    assert response.etag == "etag-1"
    assert requests[0].headers["authorization"] == "Bearer token-value"
    assert requests[1].url.params["per_page"] == "100"

    unchanged = client.issues(repo, "2026-01-01T00:00:00Z", "etag-1")
    assert unchanged.not_modified
    client.close()
