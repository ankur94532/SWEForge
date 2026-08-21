"""Typed data exchanged by the GitHub ingestion boundary."""

import re
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
    default_branch: str = "main"


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
    """Return whether body contains a standalone, case-insensitive mention."""
    if not body:
        return False
    escaped = re.escape(token)
    return (
        re.search(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", body, re.IGNORECASE)
        is not None
    )


def starts_with_agent_invocation(body: str | None, token: str = "@agent") -> bool:
    """Return true only when a comment begins with the invocation token."""
    if not body:
        return False
    escaped = re.escape(token)
    return re.match(rf"^\s*{escaped}(?![A-Za-z0-9_])", body, re.IGNORECASE) is not None


def classify_subject(issue_payload: dict) -> SubjectKind:
    """Classify an issue API object without confusing pull requests for issues."""
    return (
        SubjectKind.PULL_REQUEST
        if issue_payload.get("pull_request")
        else SubjectKind.ISSUE
    )
