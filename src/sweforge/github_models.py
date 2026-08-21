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


class OriginSurface(StrEnum):
    ISSUE = "ISSUE"
    PR_CONVERSATION = "PR_CONVERSATION"
    PR_INLINE_REVIEW = "PR_INLINE_REVIEW"


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
    source_created_at: str | None = None
    thread_id: str | None = None
    origin_surface: OriginSurface = OriginSurface.ISSUE
    path: str | None = None
    line: int | None = None
    start_line: int | None = None
    side: str | None = None
    start_side: str | None = None
    diff_hunk: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    in_reply_to_id: str | None = None
    pull_request_review_id: str | None = None
    review_thread_root_id: str | None = None

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


def format_source_context(
    event: SourceEvent | dict, task: str, current_context: str | None = None
) -> str:
    """Create bounded provenance for every model-facing GitHub input."""
    get = (
        event.get
        if isinstance(event, dict)
        else lambda key, default=None: getattr(event, key, default)
    )
    surface = str(get("origin_surface", OriginSurface.ISSUE))
    author = get("author_login") or "unknown"
    number = get("subject_number")
    if surface == OriginSurface.PR_INLINE_REVIEW.value:
        lines = [
            f"[GitHub PR #{number} inline review comment by {author}]",
            f"File: {get('path') or '(unknown)'}",
            f"Lines: {get('start_line') or get('line') or '(unknown)'}-"
            f"{get('line') or get('start_line') or '(unknown)'}",
            f"Side: {get('start_side') or get('side') or '(unknown)'}",
            f"Review anchor commit: {get('commit_id') or '(unknown)'}",
            f"Original anchor commit: {get('original_commit_id') or '(unknown)'}",
            "Immutable diff context:",
            (get("diff_hunk") or "(not provided)")[:6_000],
            "User request:",
            task[:4_000],
        ]
        if current_context:
            lines.extend(
                ["Current repository context (supplemental):", current_context[:6_000]]
            )
        if get("original_commit_id") and get("commit_id") != get("original_commit_id"):
            lines.insert(
                6,
                "The review anchor may be outdated; do not assume the current "
                "line number is authoritative.",
            )
        return "\n".join(lines)
    label = "ISSUE" if surface == OriginSurface.ISSUE.value else "PR"
    kind = "issue" if label == "ISSUE" else "PR conversation"
    return f"[GitHub {kind} #{number} comment by {author}]\n{task[:4_000]}"


def classify_subject(issue_payload: dict) -> SubjectKind:
    """Classify an issue API object without confusing pull requests for issues."""
    return (
        SubjectKind.PULL_REQUEST
        if issue_payload.get("pull_request")
        else SubjectKind.ISSUE
    )
