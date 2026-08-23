"""One-pass GitHub polling and event normalization."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .github_client import GitHubClient
from .github_models import (
    OriginSurface,
    PollResponse,
    RepositoryRef,
    SourceEvent,
    SourceKind,
    SubjectKind,
    classify_subject,
    is_actionable_source_event,
    parse_timestamp,
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
                self._normalize(repo, stream, response.items, classifications, observed)
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

    def _snapshot_issue(
        self, repo: RepositoryRef, payload: dict, number: int, observed: str
    ) -> None:
        """Persist the canonical issue title/body already present in a payload.

        SourceEvent bodies are comments, not the issue description, and are
        immutable.  Historical cases need the issue's own title and body, so
        ingestion records a snapshot rather than re-fetching it later.
        """
        if not isinstance(payload, dict):
            return
        title = payload.get("title")
        body = payload.get("body")
        if title is None and body is None:
            return
        self.store.upsert_issue_metadata(
            repo_id=repo.repo_id,
            issue_number=number,
            title=str(title or ""),
            body=str(body or ""),
            observed_at=str(payload.get("updated_at") or observed),
        )

    def _normalize(
        self,
        repo: RepositoryRef,
        stream: str,
        items: Iterable[dict],
        classifications: dict[int, SubjectKind],
        observed: str,
    ) -> Iterable[SourceEvent]:
        for item in items:
            body = item.get("body")
            if stream == "issues":
                if item.get("pull_request"):
                    continue
                self._snapshot_issue(repo, item, item["number"], observed)
            actionable = is_actionable_source_event(
                SourceKind.ISSUE if stream == "issues" else SourceKind.ISSUE_COMMENT,
                body,
            )
            if not actionable:
                if stream == "issues":
                    self.store.observe_issue_content(
                        repo_id=repo.repo_id,
                        source_id=str(item["id"]),
                        issue_number=item["number"],
                        body=body,
                        observed_at=str(item.get("updated_at") or observed),
                    )
                continue
            if stream == "issues":
                yield self._event(
                    repo,
                    SourceKind.ISSUE,
                    item,
                    SubjectKind.ISSUE,
                    item["number"],
                    body,
                    origin_surface=OriginSurface.ISSUE,
                )
            elif stream == "issue_comments":
                number = self._number_from_url(item.get("issue_url"))
                if number not in classifications:
                    # Already fetched for classification; snapshot it here so
                    # historical learning never needs its own network call.
                    payload = self.client.issue(repo, number)
                    classifications[number] = classify_subject(payload)
                    self._snapshot_issue(repo, payload, number, observed)
                subject = classifications[number]
                yield self._event(
                    repo,
                    SourceKind.ISSUE_COMMENT,
                    item,
                    subject,
                    number,
                    body,
                    origin_surface=(
                        OriginSurface.PR_CONVERSATION
                        if subject == SubjectKind.PULL_REQUEST
                        else OriginSurface.ISSUE
                    ),
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
                    origin_surface=OriginSurface.PR_INLINE_REVIEW,
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
        *,
        origin_surface: OriginSurface,
    ) -> SourceEvent:
        in_reply_to_id = item.get("in_reply_to_id")
        return SourceEvent(
            repo_id=repo.repo_id,
            repo_full_name=repo.full_name,
            source_kind=source_kind,
            source_id=str(item["id"]),
            source_updated_at=item["updated_at"],
            source_created_at=item.get("created_at"),
            subject_kind=subject_kind,
            subject_number=subject_number,
            author_login=(item.get("user") or {}).get("login"),
            body=body,
            html_url=item.get("html_url"),
            origin_surface=origin_surface,
            path=item.get("path"),
            line=item.get("line"),
            start_line=item.get("start_line"),
            side=item.get("side"),
            start_side=item.get("start_side"),
            diff_hunk=item.get("diff_hunk"),
            commit_id=item.get("commit_id"),
            original_commit_id=item.get("original_commit_id"),
            in_reply_to_id=(
                str(in_reply_to_id) if in_reply_to_id is not None else None
            ),
            pull_request_review_id=(
                str(item["pull_request_review_id"])
                if item.get("pull_request_review_id") is not None
                else None
            ),
            review_thread_root_id=(
                str(in_reply_to_id) if in_reply_to_id is not None else str(item["id"])
            ),
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
