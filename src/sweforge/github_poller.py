"""One-pass GitHub polling and event normalization."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .github_client import GitHubClient
from .github_models import (
    PollResponse,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
    classify_subject,
    contains_agent_mention,
    parse_timestamp,
    starts_with_agent_invocation,
)
from .github_store import RecordBatchResult, SQLiteGitHubStore

STREAMS = ("issues", "issue_comments", "review_comments")
_NUMBER_RE = re.compile(r"/(?:issues|pulls)/(\d+)(?:$|/)")


@dataclass(frozen=True)
class PollResult:
    repositories: int = 0
    events_discovered: int = 0
    events_persisted: int = 0
    threads_created: int = 0
    events_routed: int = 0
    pr_events_unrouted: int = 0

    @property
    def discovered(self) -> int:
        return self.events_discovered

    @property
    def persisted(self) -> int:
        return self.events_persisted


class GitHubPoller:
    def __init__(
        self,
        client: GitHubClient,
        store: SQLiteGitHubStore,
        *,
        now: Callable[[], datetime] | None = None,
        initial_lookback: timedelta = timedelta(minutes=10),
        overlap: timedelta = timedelta(seconds=30),
    ) -> None:
        self.client = client
        self.store = store
        self.now = now or (lambda: datetime.now(UTC))
        self.initial_lookback = initial_lookback
        self.overlap = overlap

    def poll(self, repositories: Iterable[str]) -> PollResult:
        total = PollResult()
        for full_name in repositories:
            total = self._add(total, self._poll_repository(full_name))
        return total

    def _poll_repository(self, full_name: str) -> PollResult:
        repo = self.client.repository(full_name)
        observed = self._timestamp(self.now())
        self.store.upsert_repository(repo.repo_id, repo.full_name, observed)
        result = PollResult(repositories=1)
        classifications: dict[int, SubjectKind] = {}
        for stream in STREAMS:
            response, since = self._fetch(repo, stream, observed)
            events = list(
                self._normalize(repo, stream, response.items, classifications)
            )
            batch = self.store.record_batch(
                repo.repo_id,
                stream,
                events,
                since=since,
                etag=response.etag,
                polled_at=observed,
            )
            result = self._add(result, self._result_for_batch(events, batch))
        return result

    def _fetch(
        self, repo: RepositoryRef, stream: str, observed: str
    ) -> tuple[PollResponse, str]:
        cursor = self.store.cursor(repo.repo_id, stream)
        if cursor:
            since = cursor["since"]
            etag = cursor["etag"]
        else:
            since = (
                (
                    datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    - self.initial_lookback
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            etag = None
        method = getattr(self.client, stream)
        response = method(repo, since, etag)
        if response.not_modified or not response.items:
            return response, since
        timestamps = [
            parse_timestamp(item["updated_at"])
            for item in response.items
            if item.get("updated_at")
        ]
        if not timestamps:
            return response, since
        latest = max(timestamps) - self.overlap
        # The next cursor represents a different query, so the current page's
        # ETag is deliberately discarded. ETags remain useful for unchanged
        # queries (304), while overlap and event keys provide correctness.
        return PollResponse(items=response.items), latest.astimezone(
            UTC
        ).isoformat().replace("+00:00", "Z")

    def _normalize(
        self,
        repo: RepositoryRef,
        stream: str,
        items: Iterable[dict],
        classifications: dict[int, SubjectKind],
    ) -> Iterable[SourceEvent]:
        for item in items:
            body = item.get("body")
            actionable = (
                contains_agent_mention(body)
                if stream == "issues"
                else starts_with_agent_invocation(body)
            )
            if not actionable:
                continue
            if stream == "issues":
                if item.get("pull_request"):
                    continue
                yield self._event(
                    repo,
                    SourceKind.ISSUE,
                    item,
                    SubjectKind.ISSUE,
                    item["number"],
                    body,
                )
            elif stream == "issue_comments":
                number = self._number_from_url(item.get("issue_url"))
                if number not in classifications:
                    classifications[number] = classify_subject(
                        self.client.issue(repo, number)
                    )
                subject = classifications[number]
                yield self._event(
                    repo,
                    SourceKind.ISSUE_COMMENT,
                    item,
                    subject,
                    number,
                    body,
                )
            else:
                number = self._number_from_url(item.get("pull_request_url"))
                yield self._event(
                    repo,
                    SourceKind.REVIEW_COMMENT,
                    item,
                    SubjectKind.PULL_REQUEST,
                    number,
                    body,
                )

    @staticmethod
    def _number_from_url(value: str | None) -> int:
        match = _NUMBER_RE.search(value or "")
        if not match:
            raise ValueError("GitHub object did not include a recognizable subject URL")
        return int(match.group(1))

    @staticmethod
    def _event(
        repo: RepositoryRef,
        source_kind: SourceKind,
        item: dict,
        subject_kind: SubjectKind,
        subject_number: int,
        body: str,
    ) -> SourceEvent:
        return SourceEvent(
            repo_id=repo.repo_id,
            repo_full_name=repo.full_name,
            source_kind=source_kind,
            source_id=str(item["id"]),
            source_updated_at=item["updated_at"],
            subject_kind=subject_kind,
            subject_number=subject_number,
            author_login=(item.get("user") or {}).get("login"),
            body=body,
            html_url=item.get("html_url"),
        )

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("poller clock must return an aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _result_for_batch(
        events: list[SourceEvent], batch: RecordBatchResult
    ) -> PollResult:
        return PollResult(
            events_discovered=len(events),
            events_persisted=batch.events_persisted,
            threads_created=batch.threads_created,
            events_routed=batch.events_routed,
            pr_events_unrouted=batch.pr_events_unrouted,
        )

    @staticmethod
    def _add(left: PollResult, right: PollResult) -> PollResult:
        return PollResult(
            repositories=left.repositories + right.repositories,
            events_discovered=left.events_discovered + right.events_discovered,
            events_persisted=left.events_persisted + right.events_persisted,
            threads_created=left.threads_created + right.threads_created,
            events_routed=left.events_routed + right.events_routed,
            pr_events_unrouted=left.pr_events_unrouted + right.pr_events_unrouted,
        )
