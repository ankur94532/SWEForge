"""One deterministic GitHub double for every offline test.

Replaces three near-duplicate ``FakeGitHub`` classes that had drifted apart:
two were byte-identical comment/PR surfaces, the third an injected-stream
surface for the poller. Keeping them separate meant a protocol change had to
be noticed in three places, and no single double covered the whole
``GitHubClient`` Protocol.

Construct it bare for the comment/PR surface, or pass ``repositories`` /
``responses`` / ``issue_payloads`` for the polling surface. Every call is
recorded in ``ledger`` so scenario predicates can assert on what was sent
rather than only on durable state.
"""

from dataclasses import dataclass
from typing import Any

from sweforge.github_client import PollResponse
from sweforge.github_models import RepositoryRef


@dataclass(frozen=True, slots=True)
class Call:
    """One recorded client call."""

    method: str
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()


class FakeGitHub:
    """Deterministic offline GitHub surface covering the whole Protocol."""

    def __init__(
        self,
        repositories: dict[str, RepositoryRef] | None = None,
        responses: dict[tuple[int, str], PollResponse] | None = None,
        issue_payloads: dict[tuple[int, int], dict] | None = None,
        *,
        default_repo_id: int = 1,
        clock=None,
        permissions: dict[str, str] | None = None,
        default_permission: str = "write",
    ) -> None:
        self.repositories = repositories or {}
        self.responses = responses or {}
        self.issue_payloads = issue_payloads or {}
        self.default_repo_id = default_repo_id
        # Approval requires repository write access. Scenarios that are not
        # about authorization get a writer by default; one testing refusal
        # sets permissions={"login": "read"} explicitly.
        self.permissions = permissions or {}
        self.default_permission = default_permission
        self.permission_calls: list[tuple[int, str]] = []
        # One time source per world. Without this the fake invents comment
        # timestamps on a different timeline than the engine clock, and
        # posted_at (which comes from the comment) lands ahead of everything.
        self._clock = clock
        self.created: list[dict] = []
        self.pulls: list[dict] = []
        self.ledger: list[Call] = []
        # Retained under their original names so migrated tests keep asserting
        # exactly what they asserted before.
        self.issue_calls: list[tuple[int, int]] = []
        self.stream_calls: list[tuple[str, str, str | None]] = []

    def _created_at(self) -> str:
        if self._clock is not None:
            return self._clock()
        return f"2026-01-01T00:{len(self.created) + 10:02d}:00Z"

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.ledger.append(Call(method, tuple(args), tuple(sorted(kwargs.items()))))

    def calls(self, method: str) -> list[Call]:
        return [item for item in self.ledger if item.method == method]

    # -- repository / issue -------------------------------------------------

    def repository(self, full_name: str) -> RepositoryRef:
        self._record("repository", full_name)
        if self.repositories:
            return self.repositories[full_name]
        return RepositoryRef(self.default_repo_id, full_name, "main")

    def collaborator_permission(self, repo: RepositoryRef, login: str) -> str:
        self._record("collaborator_permission", repo.repo_id, login)
        self.permission_calls.append((repo.repo_id, login))
        return self.permissions.get(login, self.default_permission)

    def issue(self, repo: RepositoryRef, number: int) -> dict:
        self._record("issue", repo.repo_id, number)
        self.issue_calls.append((repo.repo_id, number))
        if self.issue_payloads:
            return self.issue_payloads.get((repo.repo_id, number), {})
        return {"labels": []}

    # -- polling streams ----------------------------------------------------

    def _poll(self, stream: str, repo: RepositoryRef, since, etag) -> PollResponse:
        self._record(stream, repo.repo_id, since, etag)
        self.stream_calls.append((stream, since, etag))
        return self.responses.get((repo.repo_id, stream), PollResponse())

    def issues(self, repo: RepositoryRef, since, etag) -> PollResponse:
        return self._poll("issues", repo, since, etag)

    def issue_comments(self, repo: RepositoryRef, since, etag) -> PollResponse:
        return self._poll("issue_comments", repo, since, etag)

    def review_comments(self, repo: RepositoryRef, since, etag) -> PollResponse:
        return self._poll("review_comments", repo, since, etag)

    # -- comments -----------------------------------------------------------

    def comments(self, repo: RepositoryRef, number: int) -> list[dict]:
        self._record("comments", repo.repo_id, number)
        return list(self.created)

    def review_comments_for_pull_request(
        self, repo: RepositoryRef, number: int
    ) -> list[dict]:
        self._record("review_comments_for_pull_request", repo.repo_id, number)
        return list(self.created)

    def create_comment(self, repo: RepositoryRef, number: int, body: str) -> dict:
        self._record("create_comment", repo.repo_id, number)
        item = {
            "id": len(self.created) + 1,
            "body": body,
            "created_at": self._created_at(),
        }
        self.created.append(item)
        return item

    def create_review_comment_reply(
        self, repo: RepositoryRef, pull_number: int, comment_id: int, body: str
    ) -> dict:
        # Builds the item directly rather than delegating to create_comment:
        # delegating recorded two ledger entries for one logical creation and
        # inflated every idempotency count that reads the ledger.
        self._record("create_review_comment_reply", repo.repo_id, pull_number)
        item = {
            "id": len(self.created) + 1,
            "body": body,
            "created_at": self._created_at(),
        }
        self.created.append(item)
        return item

    # -- pull requests ------------------------------------------------------

    def pull_requests(self, repo: RepositoryRef, *, head: str, base: str) -> list[dict]:
        self._record("pull_requests", repo.repo_id, head=head, base=base)
        return [item for item in self.pulls if item["head"] == head]

    def create_pull_request(
        self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str
    ) -> dict:
        self._record("create_pull_request", repo.repo_id, head=head, base=base)
        number = 40 + len(self.pulls) + 1
        item = {
            "number": number,
            "html_url": f"https://github.com/example/repo/pull/{number}",
            "head": head,
        }
        self.pulls.append(item)
        return item

    # -- assertions helpers -------------------------------------------------

    def bodies_with(self, token: str) -> list[dict]:
        return [item for item in self.created if token in item["body"]]
