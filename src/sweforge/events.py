"""Structured JSONL transition log.

One line per durable workflow transition, written to ``SWEFORGE_EVENT_LOG``.
The log exists so scenario predicates can assert on transitions identically
offline and live, instead of scraping SQLite or GitHub state after the fact.

Redaction here is *structural*, not filtered. ``data`` accepts only an
allowlist of scalar fields per event kind, so plan bodies, issue bodies, diffs,
model messages and tool output are not representable at all. A disallowed key
cannot leak by accident because there is no code path that writes one.
"""

import json
import os
import threading
from datetime import UTC, datetime
from enum import StrEnum

SCHEMA_VERSION = 1
LOG_ENV = "SWEFORGE_EVENT_LOG"
RUN_ID_ENV = "SWEFORGE_RUN_ID"
STRICT_ENV = "SWEFORGE_EVENT_STRICT"

# An allowlisted field still must not become a smuggling channel for free text,
# so every scalar is bounded. Identifiers, SHAs and statuses are far shorter.
MAX_VALUE_CHARS = 200


class EventKind(StrEnum):
    ROOT_INGESTED = "ROOT_INGESTED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_POSTED = "PLAN_POSTED"
    PLAN_REVISED = "PLAN_REVISED"
    PLAN_SUPERSEDED = "PLAN_SUPERSEDED"
    APPROVAL_OBSERVED = "APPROVAL_OBSERVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    PERMIT_CREATED = "PERMIT_CREATED"
    PERMIT_VALIDATED = "PERMIT_VALIDATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_ORPHANED = "EXECUTION_ORPHANED"
    EXECUTION_RECOVERED = "EXECUTION_RECOVERED"
    CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
    CLARIFICATION_ANSWERED = "CLARIFICATION_ANSWERED"
    INPUT_DEFERRED = "INPUT_DEFERRED"
    INPUT_DELIVERED = "INPUT_DELIVERED"
    REVIEW_ATTEMPT = "REVIEW_ATTEMPT"
    REVIEW_ACCEPTED = "REVIEW_ACCEPTED"
    REVIEW_NEEDS_FIXES = "REVIEW_NEEDS_FIXES"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    REVIEW_INFRA_FAILED = "REVIEW_INFRA_FAILED"
    REPAIR_AUTHORIZED = "REPAIR_AUTHORIZED"
    PUBLICATION_STARTED = "PUBLICATION_STARTED"
    COMMIT_CREATED = "COMMIT_CREATED"
    BRANCH_PUSHED = "BRANCH_PUSHED"
    PR_CREATED = "PR_CREATED"
    PR_REUSED = "PR_REUSED"
    COMMENT_POSTED = "COMMENT_POSTED"
    PUBLICATION_COMPLETED = "PUBLICATION_COMPLETED"
    LEARNING_STARTED = "LEARNING_STARTED"
    LEARNING_RESULT = "LEARNING_RESULT"
    PHASE_CHANGED = "PHASE_CHANGED"
    LOCK_ACQUIRED = "LOCK_ACQUIRED"
    LOCK_CONTENDED = "LOCK_CONTENDED"
    FAULT_FIRED = "FAULT_FIRED"
    FIXTURE_CAPTURED = "FIXTURE_CAPTURED"


# Envelope fields are common to every kind and never appear in ``data``.
ENVELOPE_FIELDS = frozenset({"thread_id", "cycle_id", "repo_id"})

_PLAN = frozenset({"plan_id", "plan_version", "root_event_key", "root_input_id"})
_EXEC = frozenset(
    {"execution_id", "attempt_id", "attempt_kind", "attempt_number", "retry_count"}
)
_PUB = frozenset({"publication_id", "commit_sha", "branch", "pr_number", "comment_id"})
_REVIEW = frozenset(
    {"review_id", "attempt_id", "review_iteration", "verdict", "guard_codes_count"}
)

ALLOWED_DATA_FIELDS: dict[EventKind, frozenset[str]] = {
    EventKind.ROOT_INGESTED: frozenset(
        {"root_event_key", "issue_number", "source_kind"}
    ),
    EventKind.PLAN_CREATED: _PLAN,
    EventKind.PLAN_POSTED: _PLAN | frozenset({"comment_id"}),
    EventKind.PLAN_REVISED: _PLAN,
    EventKind.PLAN_SUPERSEDED: _PLAN,
    EventKind.APPROVAL_OBSERVED: _PLAN | frozenset({"approval_event_key", "actor"}),
    EventKind.APPROVAL_REJECTED: _PLAN | frozenset({"reason"}),
    EventKind.PERMIT_CREATED: _PLAN | frozenset({"permit_id", "permit_source"}),
    EventKind.PERMIT_VALIDATED: frozenset({"permit_id", "plan_id", "plan_version"}),
    EventKind.EXECUTION_STARTED: _EXEC | frozenset({"permit_id"}),
    EventKind.EXECUTION_SUCCEEDED: _EXEC,
    EventKind.EXECUTION_FAILED: _EXEC | frozenset({"reason"}),
    EventKind.EXECUTION_ORPHANED: _EXEC,
    EventKind.EXECUTION_RECOVERED: _EXEC,
    EventKind.CLARIFICATION_REQUESTED: frozenset(
        {"clarification_id", "occurrence_key", "answer_type"}
    ),
    EventKind.CLARIFICATION_ANSWERED: frozenset(
        {"clarification_id", "occurrence_key", "source_event_key"}
    ),
    EventKind.INPUT_DEFERRED: frozenset({"deferred_id", "source_event_key", "purpose"}),
    EventKind.INPUT_DELIVERED: frozenset({"event_key", "purpose"}),
    EventKind.REVIEW_ATTEMPT: _REVIEW,
    EventKind.REVIEW_ACCEPTED: _REVIEW,
    EventKind.REVIEW_NEEDS_FIXES: _REVIEW,
    EventKind.REVIEW_BLOCKED: _REVIEW,
    EventKind.REVIEW_INFRA_FAILED: _REVIEW | frozenset({"error_type"}),
    EventKind.REPAIR_AUTHORIZED: frozenset(
        {"permit_id", "round_number", "parent_review_id"}
    ),
    EventKind.PUBLICATION_STARTED: _PUB,
    EventKind.COMMIT_CREATED: _PUB,
    EventKind.BRANCH_PUSHED: _PUB | frozenset({"forced"}),
    EventKind.PR_CREATED: _PUB,
    EventKind.PR_REUSED: _PUB,
    EventKind.COMMENT_POSTED: _PUB | frozenset({"marker"}),
    EventKind.PUBLICATION_COMPLETED: _PUB,
    EventKind.LEARNING_STARTED: frozenset({"learning_id", "lane"}),
    EventKind.LEARNING_RESULT: frozenset({"learning_id", "lane", "status", "attempt"}),
    EventKind.PHASE_CHANGED: frozenset({"phase_from", "phase_to", "reason"}),
    EventKind.LOCK_ACQUIRED: frozenset({"lock_kind", "lock_key"}),
    EventKind.LOCK_CONTENDED: frozenset({"lock_kind", "lock_key"}),
    EventKind.FAULT_FIRED: frozenset({"point", "action", "call_index"}),
    EventKind.FIXTURE_CAPTURED: frozenset({"fixture_id", "capture_reason"}),
}


class EventSchemaError(ValueError):
    """A caller supplied a field the event schema cannot represent."""


_lock = threading.Lock()
_seq = 0


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def enabled() -> bool:
    return bool(os.environ.get(LOG_ENV))


def _strict() -> bool:
    return bool(os.environ.get(STRICT_ENV))


def _coerce(kind: EventKind, key: str, value: object) -> object | None:
    if not isinstance(value, str | int | float | bool | None.__class__):
        raise EventSchemaError(
            f"{kind}: field {key!r} must be a scalar, got {type(value).__name__}"
        )
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        raise EventSchemaError(
            f"{kind}: field {key!r} exceeds {MAX_VALUE_CHARS} characters"
        )
    return value


def build_event(
    kind: EventKind,
    *,
    thread_id: str | None = None,
    cycle_id: int | None = None,
    repo_id: int | None = None,
    **data: object,
) -> dict:
    """Validate and assemble one envelope. Raises on anything unrepresentable."""
    if kind not in ALLOWED_DATA_FIELDS:
        raise EventSchemaError(f"unknown event kind: {kind}")
    allowed = ALLOWED_DATA_FIELDS[kind]
    payload: dict[str, object] = {}
    for key, value in data.items():
        # Envelope fields cannot reach here: they are named parameters, so
        # Python binds them before **data. That is structural, not checked.
        if key not in allowed:
            raise EventSchemaError(
                f"{kind}: field {key!r} is not in the schema; "
                f"allowed: {sorted(allowed)}"
            )
        payload[key] = _coerce(kind, key, value)
    global _seq
    with _lock:
        _seq += 1
        seq = _seq
    from .acceptance_mode import acceptance_enabled

    return {
        "v": SCHEMA_VERSION,
        "ts": _now(),
        "seq": seq,
        "run_id": os.environ.get(RUN_ID_ENV, ""),
        "acceptance_mode": acceptance_enabled(),
        "kind": str(kind),
        "thread_id": thread_id,
        "cycle_id": cycle_id,
        "repo_id": repo_id,
        "data": payload,
    }


def emit(kind: EventKind, **fields: object) -> dict | None:
    """Append one event. Inert when SWEFORGE_EVENT_LOG is unset.

    A schema violation is a programming error at the emission site. Under
    SWEFORGE_EVENT_STRICT (tests) it raises so the mistake is caught; otherwise
    the event is dropped rather than taking down a workflow. Either way the
    disallowed field is never written, so the log cannot leak.
    """
    if not enabled():
        return None
    try:
        event = build_event(kind, **fields)  # type: ignore[arg-type]
    except EventSchemaError:
        if _strict():
            raise
        return None
    line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    path = os.environ[LOG_ENV]
    with _lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def read_log(path: str | os.PathLike[str]) -> list[dict]:
    """Parse a JSONL event log. Used by scenario predicates."""
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
