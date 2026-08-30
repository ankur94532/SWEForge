"""Marker-reconciled delivery for application-owned workflow comments."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .agent_trace import AgentTracer, TraceContext
from .github_store import SQLiteGitHubStore, WorkflowCommentOutboxRecord


def deliver_pending_workflow_comments(
    *,
    store: SQLiteGitHubStore,
    client: Any,
    thread_id: str,
    now: str,
    tracer: AgentTracer | None = None,
) -> int:
    """Reconcile pending marker comments without coupling them to workflow state."""
    delivered = 0
    for record in store.pending_workflow_comments(thread_id, due_at=now):
        event = store.source_event(record.source_event_key)
        if event is None:
            store.update_workflow_comment(
                record.outbox_id,
                status="AMBIGUOUS",
                now=now,
                error_message="source event is missing",
            )
            continue
        context = TraceContext(
            thread_id=thread_id,
            repo=str(event["repo_full_name"]),
            origin_surface=str(event["origin_surface"]),
            subject_number=int(event["subject_number"]),
        )
        required_event = (
            "REVISION INPUT ACK REQUIRED"
            if record.message_kind == "REVISION_INPUT_ACK"
            else "FEEDBACK PUSHBACK REQUIRED"
        )
        posted_event = (
            "REVISION INPUT ACK POSTED"
            if record.message_kind == "REVISION_INPUT_ACK"
            else "FEEDBACK PUSHBACK POSTED"
        )
        if tracer is not None:
            tracer.emit(required_event, f"outbox={record.outbox_id}", context)
        try:
            comment = _reconcile_one(client=client, record=record, event=event)
        except Exception as exc:
            # Courtesy delivery is retryable and never rolls back durable input.
            store.update_workflow_comment(
                record.outbox_id,
                status="PENDING",
                now=now,
                error_message=f"{type(exc).__name__}: {exc}"[:500],
                next_attempt_at=_retry_at(now),
            )
            continue
        if comment is None:
            store.update_workflow_comment(
                record.outbox_id,
                status="AMBIGUOUS",
                now=now,
                error_message="multiple marker-matching comments",
            )
            continue
        store.update_workflow_comment(
            record.outbox_id,
            status="DELIVERED",
            now=now,
            comment_id=int(comment["id"]),
            error_message=None,
        )
        delivered += 1
        if tracer is not None:
            tracer.emit(posted_event, f"outbox={record.outbox_id}", context)
    return delivered


def _retry_at(now: str) -> str:
    try:
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return now
    return (parsed + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")


def _reconcile_one(*, client: Any, record: WorkflowCommentOutboxRecord, event: Any):
    repo = client.repository(event["repo_full_name"])
    if event["origin_surface"] == "PR_INLINE_REVIEW":
        comments = client.review_comments_for_pull_request(
            repo, event["subject_number"]
        )
    else:
        comments = client.comments(repo, event["subject_number"])
    matches = [
        comment
        for comment in comments
        if record.stable_marker in (comment.get("body") or "")
    ]
    if len(matches) > 1:
        return None
    if matches:
        return matches[0]
    if event["origin_surface"] == "PR_INLINE_REVIEW":
        reply_to = event["review_thread_root_id"] or event["source_id"]
        if not reply_to:
            raise RuntimeError("inline workflow response target is missing")
        return client.create_review_comment_reply(
            repo, event["subject_number"], int(reply_to), record.body
        )
    return client.create_comment(repo, event["subject_number"], record.body)
