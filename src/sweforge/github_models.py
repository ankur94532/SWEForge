"""Typed data exchanged by the GitHub ingestion boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    ISSUE = "issue"
    ISSUE_COMMENT = "issue_comment"
    REVIEW_COMMENT = "review_comment"


class SubjectKind(StrEnum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class RepositoryRef:
    repo_id: int
    full_name: str


@dataclass(frozen=True)
class SourceEvent:
    repo_id: int
    repo_full_name: str
    source_kind: SourceKind
    source_id: str
    source_updated_at: str
    subject_kind: SubjectKind
    subject_number: int
    author_login: str | None
    body: str
    html_url: str | None
    thread_id: str | None = None

    @property
    def event_key(self) -> str:
        return ":".join(
            (
                str(self.repo_id),
                self.source_kind.value,
                self.source_id,
                self.source_updated_at,
            )
        )


@dataclass(frozen=True)
class PollCursor:
    since: str
    etag: str | None = None
    last_successful_poll_at: str | None = None


@dataclass(frozen=True)
class PollResponse:
    items: tuple[dict, ...] = ()
    etag: str | None = None
    not_modified: bool = False


def parse_timestamp(value: str) -> datetime:
    """Parse GitHub's UTC timestamps into aware datetimes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def contains_agent_mention(body: str | None, token: str = "@agent") -> bool:
    """Return whether body contains the standalone, case-insensitive token."""
    if not body:
        return False
    lowered = body.casefold()
    token_lower = token.casefold()
    start = 0
    while (index := lowered.find(token_lower, start)) >= 0:
        end = index + len(token_lower)
        if end == len(lowered) or not (lowered[end].isalnum() or lowered[end] == "_"):
            return True
        start = end
    return False


def classify_subject(issue_payload: dict) -> SubjectKind:
    """Classify an issue API object without confusing pull requests for issues."""
    return (
        SubjectKind.PULL_REQUEST
        if issue_payload.get("pull_request")
        else SubjectKind.ISSUE
    )
