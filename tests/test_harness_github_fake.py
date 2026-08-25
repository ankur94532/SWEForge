"""The shared GitHub double must cover the whole Protocol and record calls.

Three near-duplicate fakes previously drifted apart; this pins the union so a
Protocol change fails here rather than silently in one of three copies.
"""

from harness.github_fake import FakeGitHub

from sweforge.github_client import GitHubClient, PollResponse
from sweforge.github_models import RepositoryRef


def _protocol_methods() -> set[str]:
    return {
        name
        for name in dir(GitHubClient)
        if not name.startswith("_") and callable(getattr(GitHubClient, name))
    }


def test_covers_every_github_client_protocol_method():
    fake = FakeGitHub()
    missing = {name for name in _protocol_methods() if not hasattr(fake, name)}
    assert not missing, f"FakeGitHub does not implement: {sorted(missing)}"


def test_comment_surface_round_trips():
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    created = fake.create_comment(repo, 7, "hello <!-- marker -->")
    assert created["id"] == 1
    assert fake.comments(repo, 7) == [created]
    assert fake.bodies_with("marker") == [created]


def test_pull_request_surface_is_head_scoped():
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    made = fake.create_pull_request(
        repo, head="sweforge/issue-7", base="main", title="t", body="b"
    )
    assert fake.pull_requests(repo, head="sweforge/issue-7", base="main") == [made]
    assert fake.pull_requests(repo, head="other", base="main") == []


def test_polling_surface_uses_injected_responses():
    repo = RepositoryRef(9, "example/repo", "main")
    response = PollResponse()
    fake = FakeGitHub(
        repositories={"example/repo": repo},
        responses={(9, "issues"): response},
    )
    assert fake.repository("example/repo") is repo
    assert fake.issues(repo, "now", None) is response
    # An unseeded stream must still answer, not raise.
    assert isinstance(fake.review_comments(repo, "now", None), PollResponse)
    assert [item[0] for item in fake.stream_calls] == ["issues", "review_comments"]


def test_issue_payloads_default_to_a_labelless_issue():
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    assert fake.issue(repo, 7) == {"labels": []}
    seeded = FakeGitHub(issue_payloads={(1, 7): {"labels": ["AUTO"]}})
    assert seeded.issue(seeded.repository("example/repo"), 7) == {"labels": ["AUTO"]}


def test_every_call_is_recorded_for_predicates():
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    fake.create_comment(repo, 7, "body")
    fake.create_pull_request(repo, head="h", base="main", title="t", body="b")
    assert [c.method for c in fake.calls("create_comment")] == ["create_comment"]
    assert len(fake.calls("create_pull_request")) == 1
    assert {c.method for c in fake.ledger} >= {
        "repository",
        "create_comment",
        "create_pull_request",
    }
