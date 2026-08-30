import httpx

from sweforge.github_client import HttpxGitHubClient
from sweforge.github_models import RepositoryRef


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
    assert requests[2].headers["authorization"] == "Bearer token-value"
    assert "per_page" not in requests[2].url.params

    unchanged = client.issues(repo, "2026-01-01T00:00:00Z", "etag-1")
    assert unchanged.not_modified
    client.close()


def test_writeback_endpoints_use_repository_write_scope():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/repos/example/repo"):
            return httpx.Response(200, json={"id": 123, "full_name": "example/repo"})
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 4})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 8})

    client = HttpxGitHubClient(
        "token-value",
        api_url="https://api.example",
        transport=httpx.MockTransport(handler),
    )
    repo = client.repository("example/repo")
    assert client.pull_requests(repo, head="sweforge/issue-7", base="main") == []
    assert (
        client.create_pull_request(
            repo, head="sweforge/issue-7", base="main", title="title", body="body"
        )["number"]
        == 4
    )
    assert client.comments(repo, 7) == []
    assert client.create_comment(repo, 7, "body")["id"] == 8
    assert all(
        request.headers["authorization"] == "Bearer token-value" for request in requests
    )
    client.close()


def test_submitted_review_discovery_paginates_recent_pull_reviews():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 12,
                        "updated_at": "2026-01-01T00:05:00Z",
                        "url": "https://api.example/repos/example/repo/pulls/12",
                    },
                    {
                        "number": 11,
                        "updated_at": "2025-12-31T23:00:00Z",
                        "url": "https://api.example/repos/example/repo/pulls/11",
                    },
                ],
            )
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 72,
                        "body": "@agent second page",
                        "submitted_at": "2026-01-01T00:04:00Z",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": 71,
                    "body": "@agent first page",
                    "submitted_at": "2026-01-01T00:03:00Z",
                },
                {"id": 73, "body": "@agent pending draft", "submitted_at": None},
            ],
            headers={
                "link": (
                    "<https://api.example/repos/example/repo/pulls/12/reviews?page=2>; "
                    'rel="next"'
                )
            },
        )

    client = HttpxGitHubClient(
        "token-value",
        api_url="https://api.example",
        transport=httpx.MockTransport(handler),
    )
    reviews = client.pull_request_reviews(
        RepositoryRef(123, "example/repo"),
        "2026-01-01T00:00:00Z",
        None,
    )

    assert [item["id"] for item in reviews.items] == [71, 72]
    assert all(item["updated_at"] == item["submitted_at"] for item in reviews.items)
    assert all(item["pull_request_url"].endswith("/pulls/12") for item in reviews.items)
    assert not any(
        request.url.path.endswith("/pulls/11/reviews") for request in requests
    )
    client.close()
