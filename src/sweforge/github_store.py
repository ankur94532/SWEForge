"""Durable SQLite persistence for GitHub ingestion."""

# SQL statements are kept readable as complete statements.
# ruff: noqa: E501

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock

from .events import EventKind, emit
from .github_models import (
    InteractionMode,
    SourceEvent,
    SubjectKind,
    is_actionable_source_event,
    is_exact_agent_approval,
    starts_with_agent_invocation,
)
from .workflow_messages import (
    PLAN_DEFERRED_FEEDBACK_MESSAGE,
    RESULT_DEFERRED_FEEDBACK_MESSAGE,
    UNSOLICITED_ACK_MESSAGE,
    deferred_feedback_marker,
    outbox_id_for,
    revision_ack_marker,
)


class _SerializedSQLiteCursor(sqlite3.Cursor):
    """Serialize every operation on a shared cross-thread connection."""

    @property
    def _lock(self) -> RLock:
        return self.connection._serialize_lock  # type: ignore[attr-defined]

    def execute(self, *args, **kwargs):
        with self._lock:
            return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return super().executescript(*args, **kwargs)

    def fetchone(self):
        with self._lock:
            return super().fetchone()

    def fetchmany(self, *args, **kwargs):
        with self._lock:
            return super().fetchmany(*args, **kwargs)

    def fetchall(self):
        with self._lock:
            return super().fetchall()

    def __next__(self):
        with self._lock:
            return super().__next__()

    def close(self) -> None:
        with self._lock:
            super().close()


class _SerializedSQLiteConnection(sqlite3.Connection):
    """A sqlite connection safe for LangGraph's parallel tool workers."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._serialize_lock = RLock()

    def serialized(self) -> RLock:
        """Hold the connection across a multi-statement transaction."""
        return self._serialize_lock

    def cursor(self, factory=None):
        with self._serialize_lock:
            return super().cursor(factory or _SerializedSQLiteCursor)

    def execute(self, *args, **kwargs):
        with self._serialize_lock:
            return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._serialize_lock:
            return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._serialize_lock:
            return self.cursor().executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._serialize_lock:
            super().commit()

    def rollback(self) -> None:
        with self._serialize_lock:
            super().rollback()

    def close(self) -> None:
        with self._serialize_lock:
            super().close()


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS repositories (
    repo_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_config_generations_v1 (
    generation_id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    generation INTEGER NOT NULL,
    digest TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    workflow_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(repo_id, generation)
);
CREATE TABLE IF NOT EXISTS repo_config_files_v1 (
    generation_id TEXT NOT NULL REFERENCES repo_config_generations_v1(generation_id),
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(generation_id, path)
);
CREATE TABLE IF NOT EXISTS repo_config_current_v1 (
    repo_id INTEGER PRIMARY KEY REFERENCES repositories(repo_id),
    generation_id TEXT NOT NULL REFERENCES repo_config_generations_v1(generation_id),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_secrets_v1 (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    name TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(repo_id, name)
);
CREATE TABLE IF NOT EXISTS repo_secret_audit_v1 (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    name TEXT,
    operation TEXT NOT NULL,
    subject TEXT,
    count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_threads (
    thread_id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    interaction_mode TEXT NOT NULL DEFAULT 'MANUAL',
    config_generation_id TEXT REFERENCES repo_config_generations_v1(generation_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, issue_number)
);
CREATE TABLE IF NOT EXISTS thread_workflow_lifecycle_v1 (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    initial_state TEXT NOT NULL,
    initial_root_event_key TEXT NOT NULL,
    initial_workflow_cycle_id TEXT,
    initial_workflow_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_events (
    event_key TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    source_created_at TEXT,
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL,
    author_login TEXT,
    body TEXT NOT NULL,
    html_url TEXT,
    thread_id TEXT REFERENCES issue_threads(thread_id),
    discovered_at TEXT NOT NULL,
    origin_surface TEXT NOT NULL DEFAULT 'ISSUE',
    path TEXT,
    line INTEGER,
    start_line INTEGER,
    side TEXT,
    start_side TEXT,
    diff_hunk TEXT,
    commit_id TEXT,
    original_commit_id TEXT,
    in_reply_to_id TEXT,
    pull_request_review_id TEXT,
    review_thread_root_id TEXT,
    review_state TEXT
);
CREATE TABLE IF NOT EXISTS poll_cursors (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    stream TEXT NOT NULL,
    since TEXT NOT NULL,
    etag TEXT,
    last_successful_poll_at TEXT,
    PRIMARY KEY(repo_id, stream)
);
CREATE TABLE IF NOT EXISTS pr_thread_mappings (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    pr_number INTEGER NOT NULL,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    PRIMARY KEY(repo_id, pr_number)
);
CREATE TABLE IF NOT EXISTS thread_workspaces (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    source_repository_path TEXT NOT NULL,
    workspace_path TEXT NOT NULL UNIQUE,
    branch_name TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_executions (
    event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    response_text TEXT,
    error_message TEXT,
    workspace_path TEXT,
    start_head_sha TEXT,
    end_head_sha TEXT,
    end_dirty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logical_executions (
    execution_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    response_text TEXT,
    error_message TEXT,
    workspace_path TEXT,
    start_head_sha TEXT,
    end_head_sha TEXT,
    end_dirty INTEGER NOT NULL DEFAULT 0,
    UNIQUE(thread_id, cycle_id, root_input_id)
);
CREATE TABLE IF NOT EXISTS logical_publications (
    publication_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    local_commit_sha TEXT,
    remote_commit_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    comment_id INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, cycle_id, root_input_id)
);
CREATE INDEX IF NOT EXISTS idx_logical_publications_source
    ON logical_publications(source_event_key);
CREATE TABLE IF NOT EXISTS issue_workflow_state (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    phase TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    root_input_id TEXT,
    current_plan_id TEXT,
    mode TEXT NOT NULL,
    response_surface TEXT NOT NULL DEFAULT 'ISSUE',
    response_subject_number INTEGER,
    response_comment_id TEXT,
    response_url TEXT,
    review_thread_root_id TEXT,
    planning_feedback_event_key TEXT REFERENCES source_events(event_key),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatcher_failures (
    thread_id TEXT PRIMARY KEY REFERENCES issue_threads(thread_id),
    failure_count INTEGER NOT NULL,
    next_eligible_at TEXT NOT NULL,
    last_error TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_plans (
    plan_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    cycle_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    root_input_id TEXT,
    plan_text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    posted_at TEXT,
    posted_comment_id INTEGER,
    approved_at TEXT,
    approved_by TEXT,
    approval_event_key TEXT,
    UNIQUE(thread_id, cycle_id, version)
);
CREATE TABLE IF NOT EXISTS execution_permits (
    permit_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    source TEXT NOT NULL,
    source_event_key TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT
);
CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    attempt_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    repair_round INTEGER NOT NULL DEFAULT 0,
    parent_review_id TEXT,
    authorization_id TEXT NOT NULL,
    status TEXT NOT NULL,
    response_text TEXT,
    start_head_sha TEXT,
    end_head_sha TEXT,
    start_dirty INTEGER NOT NULL DEFAULT 0,
    end_dirty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    repair_recovery_count INTEGER NOT NULL DEFAULT 0,
    review_recovery_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(thread_id, cycle_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS execution_tool_evidence (
    evidence_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(attempt_id),
    thread_id TEXT NOT NULL,
    cycle_id INTEGER NOT NULL,
    sequence_number INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'SHELL',
    command TEXT NOT NULL,
    exit_code INTEGER,
    output TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    UNIQUE(attempt_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_execution_tool_evidence_attempt
    ON execution_tool_evidence(attempt_id, sequence_number);
CREATE TABLE IF NOT EXISTS execution_reviews (
    review_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_attempts(attempt_id),
    review_iteration INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    repair_instructions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    requirement_checks_json TEXT NOT NULL DEFAULT '[]',
    inspection_json TEXT NOT NULL DEFAULT '{}',
    challenge_json TEXT NOT NULL DEFAULT '{}',
    read_ledger_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS review_repair_permits (
    permit_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES issue_plans(plan_id),
    plan_version INTEGER NOT NULL,
    root_event_key TEXT REFERENCES source_events(event_key),
    parent_review_id TEXT NOT NULL REFERENCES execution_reviews(review_id),
    repair_round INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT,
    UNIQUE(thread_id, cycle_id, repair_round)
);
CREATE TABLE IF NOT EXISTS thread_input_consumptions (
    event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS clarification_requests (
    clarification_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    occurrence_key TEXT NOT NULL DEFAULT '',
    requested_from_phase TEXT NOT NULL,
    question TEXT NOT NULL,
    reason TEXT NOT NULL,
    answer_type TEXT NOT NULL,
    choices_json TEXT NOT NULL DEFAULT '[]',
    origin_surface TEXT NOT NULL,
    response_subject_number INTEGER NOT NULL,
    response_comment_id TEXT,
    response_url TEXT,
    review_thread_root_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    answer_event_key TEXT REFERENCES source_events(event_key),
    answer_json TEXT
);
CREATE TABLE IF NOT EXISTS deferred_followups (
    deferred_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    originating_cycle_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    residual_text TEXT,
    queued_at TEXT NOT NULL,
    consumed_cycle_id INTEGER,
    consumed_at TEXT
);

-- Versioned generic workflow runtime.  The legacy single-task tables above
-- remain migration inputs and publication/evidence compatibility storage;
-- these tables are the workflow-control authority for declarative DAG cycles.
CREATE TABLE IF NOT EXISTS workflow_cycles_v1 (
    workflow_cycle_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    workflow_digest TEXT NOT NULL,
    workflow_spec_json TEXT NOT NULL,
    workflow_spec_ref TEXT,
    cycle_kind TEXT NOT NULL DEFAULT 'INITIAL',
    revision_sequence INTEGER,
    status TEXT NOT NULL,
    active_task_id TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, cycle_id)
);
CREATE TABLE IF NOT EXISTS revision_inputs_v1 (
    revision_input_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    residual_text TEXT,
    classification_reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    queued_at TEXT NOT NULL,
    revision_workflow_cycle_id TEXT REFERENCES workflow_cycles_v1(workflow_cycle_id),
    batched_at TEXT,
    consumed_at TEXT,
    UNIQUE(source_event_key, residual_text)
);
CREATE INDEX IF NOT EXISTS idx_revision_inputs_thread_status
    ON revision_inputs_v1(thread_id, status, queued_at, revision_input_id);
CREATE TABLE IF NOT EXISTS workflow_feedback_reviews_v1 (
    feedback_review_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL UNIQUE REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    feedback_kind TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    feedback_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'REVIEWING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_reviews_task_status
    ON workflow_feedback_reviews_v1(task_run_id, status, created_at);
CREATE TABLE IF NOT EXISTS workflow_comment_outbox_v1 (
    outbox_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    message_kind TEXT NOT NULL,
    stable_marker TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    comment_id INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(source_event_key, message_kind)
);
CREATE INDEX IF NOT EXISTS idx_workflow_comment_outbox_pending
    ON workflow_comment_outbox_v1(thread_id, status, created_at);
CREATE TABLE IF NOT EXISTS workflow_task_runs_v1 (
    task_run_id TEXT PRIMARY KEY,
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    declaration_index INTEGER NOT NULL,
    dependencies_json TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    current_plan_id TEXT,
    execution_attempt INTEGER NOT NULL DEFAULT 0,
    validation_round INTEGER NOT NULL DEFAULT 0,
    repair_feedback_json TEXT NOT NULL DEFAULT '[]',
    failure_reason TEXT,
    waiting_from_phase TEXT,
    clarification_occurrence_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_cycle_id, task_id),
    UNIQUE(workflow_cycle_id, declaration_index)
);
CREATE INDEX IF NOT EXISTS idx_workflow_task_runs_cycle
    ON workflow_task_runs_v1(workflow_cycle_id, declaration_index);
CREATE TABLE IF NOT EXISTS workflow_task_plans_v1 (
    plan_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    plan_text TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    posted_comment_id INTEGER NOT NULL,
    approval_occurrence_key TEXT NOT NULL UNIQUE,
    approved_at TEXT,
    approved_by TEXT,
    approval_event_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(task_run_id, version)
);
CREATE TABLE IF NOT EXISTS workflow_task_permits_v1 (
    permit_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    plan_id TEXT NOT NULL REFERENCES workflow_task_plans_v1(plan_id),
    plan_version INTEGER NOT NULL,
    plan_digest TEXT NOT NULL,
    approval_event_key TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval_mode TEXT NOT NULL DEFAULT 'HUMAN',
    created_at TEXT NOT NULL,
    invalidated_at TEXT
);
CREATE TABLE IF NOT EXISTS workflow_task_executions_v1 (
    execution_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    plan_id TEXT NOT NULL REFERENCES workflow_task_plans_v1(plan_id),
    permit_id TEXT NOT NULL REFERENCES workflow_task_permits_v1(permit_id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE(task_run_id, attempt)
);
CREATE TABLE IF NOT EXISTS workflow_task_validations_v1 (
    validation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    plan_id TEXT NOT NULL REFERENCES workflow_task_plans_v1(plan_id),
    execution_attempt INTEGER NOT NULL,
    validation_round INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    repair_instructions_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_run_id, validation_round)
);
CREATE TABLE IF NOT EXISTS workflow_task_results_v1 (
    result_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    plan_id TEXT NOT NULL REFERENCES workflow_task_plans_v1(plan_id),
    execution_id TEXT NOT NULL REFERENCES workflow_task_executions_v1(execution_id),
    validation_id TEXT NOT NULL REFERENCES workflow_task_validations_v1(validation_id),
    result_occurrence_key TEXT NOT NULL UNIQUE,
    posted_at TEXT NOT NULL,
    posted_comment_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_run_id, validation_id)
);
CREATE TABLE IF NOT EXISTS workflow_task_result_approvals_v1 (
    result_approval_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE REFERENCES workflow_task_results_v1(result_id),
    task_run_id TEXT NOT NULL REFERENCES workflow_task_runs_v1(task_run_id),
    workflow_cycle_id TEXT NOT NULL REFERENCES workflow_cycles_v1(workflow_cycle_id),
    plan_id TEXT NOT NULL REFERENCES workflow_task_plans_v1(plan_id),
    execution_id TEXT NOT NULL REFERENCES workflow_task_executions_v1(execution_id),
    validation_id TEXT NOT NULL REFERENCES workflow_task_validations_v1(validation_id),
    result_occurrence_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval_event_key TEXT,
    approved_at TEXT NOT NULL,
    invalidated_at TEXT
);
"""

MAX_EXECUTION_EVIDENCE_PER_ATTEMPT = 96_000

REPO_MEMORY_LEARNING_DDL = """
CREATE TABLE IF NOT EXISTS repo_memory_learning (
    learning_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    status TEXT NOT NULL,
    accepted_candidates INTEGER NOT NULL DEFAULT 0,
    rejected_candidates INTEGER NOT NULL DEFAULT 0,
    proposal_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, cycle_id, root_input_id)
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS issue_metadata (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    issue_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    PRIMARY KEY(repo_id, issue_number)
);
CREATE TABLE IF NOT EXISTS issue_content_observations (
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    source_id TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(repo_id, source_id)
);
CREATE TABLE IF NOT EXISTS repo_memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    category TEXT NOT NULL,
    fact TEXT NOT NULL,
    durability_reason TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_start_line INTEGER NOT NULL,
    evidence_end_line INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repo_memory_candidates_cycle
    ON repo_memory_candidates(thread_id, cycle_id, root_input_id);
CREATE TABLE IF NOT EXISTS issue_resolution_memory (
    resolution_id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
    cycle_id INTEGER NOT NULL,
    root_input_id TEXT NOT NULL,
    source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL DEFAULT '',
    issue_description_snapshot TEXT NOT NULL DEFAULT '',
    task_summary TEXT NOT NULL DEFAULT '',
    symptom_summary TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    fix_summary TEXT NOT NULL DEFAULT '',
    affected_components_json TEXT NOT NULL DEFAULT '[]',
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    validation_summary TEXT NOT NULL DEFAULT '',
    search_terms_json TEXT NOT NULL DEFAULT '[]',
    limitations TEXT NOT NULL DEFAULT '',
    plan_id TEXT,
    execution_id TEXT,
    review_id TEXT,
    publication_id TEXT,
    commit_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, cycle_id, root_input_id)
);
CREATE INDEX IF NOT EXISTS idx_issue_resolution_repo
    ON issue_resolution_memory(repo_id, issue_number);
"""

SCHEMA += REPO_MEMORY_LEARNING_DDL

# Derived retrieval index.  The base table stays authoritative; this is
# rebuildable from it and never the source of truth.
ISSUE_RESOLUTION_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS issue_resolution_fts USING fts5(
    resolution_id UNINDEXED,
    haystack
);
"""

SCHEMA += ISSUE_RESOLUTION_FTS_DDL


@dataclass
class RecordBatchResult:
    events_persisted: int = 0
    threads_created: int = 0
    events_routed: int = 0
    pr_events_unrouted: int = 0
    # Which threads were newly created, so ingestion can be announced after the
    # transaction commits rather than from inside it.
    created_thread_ids: list[str] = field(default_factory=list)


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    RETRY_PENDING = "RETRY_PENDING"
    SKIPPED = "SKIPPED"


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    PR_CREATED = "PR_CREATED"
    COMMENTED = "COMMENTED"
    COMPLETED = "COMPLETED"
    NO_CHANGES = "NO_CHANGES"
    FAILED = "FAILED"


class WorkflowPhase(StrEnum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    WAITING_FOR_PLAN_APPROVAL = "WAITING_FOR_PLAN_APPROVAL"
    EXECUTION_READY = "EXECUTION_READY"
    EXECUTING = "EXECUTING"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    AWAITING_PUBLICATION = "AWAITING_PUBLICATION"
    REVIEW_EXECUTION = "REVIEW_EXECUTION"
    REPAIR_READY = "REPAIR_READY"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"


class WorkflowMode(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    AUTO = "AUTO"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    SUPERSEDED = "SUPERSEDED"
    APPROVED = "APPROVED"
    AUTO_APPROVED = "AUTO_APPROVED"
    EXECUTED = "EXECUTED"


class PermitSource(StrEnum):
    USER = "USER"
    AUTO = "AUTO"


class InputPurpose(StrEnum):
    CYCLE_ROOT = "CYCLE_ROOT"
    PLAN_FEEDBACK = "PLAN_FEEDBACK"
    LIVE_PLANNING_INPUT = "LIVE_PLANNING_INPUT"
    LIVE_EXECUTION_INPUT = "LIVE_EXECUTION_INPUT"
    PLAN_APPROVAL = "PLAN_APPROVAL"
    EARLY_PLAN_APPROVAL = "EARLY_PLAN_APPROVAL"
    STALE_PLAN_APPROVAL = "STALE_PLAN_APPROVAL"
    LIVE_REVIEW_INPUT = "LIVE_REVIEW_INPUT"
    PLANNING_INPUT = "PLANNING_INPUT"
    CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
    DEFERRED_FOLLOWUP = "DEFERRED_FOLLOWUP"
    CLARIFICATION_ROUTED = "CLARIFICATION_ROUTED"


class ClarificationStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CANCELLED = "CANCELLED"


class AttemptKind(StrEnum):
    INITIAL = "INITIAL"
    REVIEW_REPAIR = "REVIEW_REPAIR"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class PendingWorkflowInputError(ValueError):
    def __init__(self, event_key: str) -> None:
        self.event_key = event_key
        super().__init__(f"workflow input requires replanning: {event_key}")


class PendingRepairInputError(PendingWorkflowInputError):
    """A repair was prevented by new user input that must not be consumed."""


class AmbiguousLifecycleError(ValueError):
    """A SourceEvent backs several lifecycles, so it is not a valid key."""


# Mirrors memory_learning.MemoryLearningStatus.PENDING without importing the
# model-dependent learning module into the persistence layer.
MEMORY_LEARNING_PENDING = "PENDING"

# Repository memory is an optimization, never an authorization input, so a
# curator that keeps failing must not hold an IssueThread at IDLE forever.
MAX_MEMORY_LEARNING_ATTEMPTS = 3

# `execution_attempts.retry_count` counts executions STARTED for one attempt:
# bind_authorized_execution inserts the row and ensure_execution_attempt bumps
# it immediately before each run, so the first execution leaves it at 1.  An
# INITIAL attempt therefore gets at most this many executions in total before
# recovery fails closed, which bounds the spend of a permanently crashing run.
MAX_INITIAL_EXECUTION_RECOVERIES = 3

# Crash recovery is bounded separately from executions started for an attempt.
MAX_REPAIR_EXECUTION_RECOVERIES = 3
# Review-infrastructure failures (provider/structured-output) previously
# retried forever: ReviewFinalizationError escaped to the dispatcher, whose
# backoff caps its delay at an hour but never its count. Bounded at 3 to match
# execution, restoring the property that every retry path terminates.
MAX_REVIEW_RECOVERIES = 3

# Historical case generation is model-dependent, so it is bounded the same way.
MAX_ISSUE_RESOLUTION_ATTEMPTS = 3


class RepoMemoryCandidateStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class IssueResolutionStatus(StrEnum):
    """Distinguishes "no useful case" from "failed" from "not configured"."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    NO_CASE = "NO_CASE"
    FAILED = "FAILED"


RESUMABLE_PUBLICATION_STATUSES = (
    PublicationStatus.PENDING,
    PublicationStatus.COMMITTED,
    PublicationStatus.PUSHED,
    PublicationStatus.PR_CREATED,
    PublicationStatus.COMMENTED,
)


def _lifecycle_digest(thread_id: str, cycle_id: int, root_input_id: str) -> str:
    return hashlib.sha256(
        f"{thread_id}\0{cycle_id}\0{root_input_id}".encode()
    ).hexdigest()[:24]


def execution_id_for(*, thread_id: str, cycle_id: int, root_input_id: str) -> str:
    """Return the stable identity for one executable workflow input."""
    return f"execution-{_lifecycle_digest(thread_id, cycle_id, root_input_id)}"


def publication_id_for(*, thread_id: str, cycle_id: int, root_input_id: str) -> str:
    """Return the stable identity for one lifecycle's publication."""
    return f"publication-{_lifecycle_digest(thread_id, cycle_id, root_input_id)}"


def memory_learning_id_for(*, thread_id: str, cycle_id: int, root_input_id: str) -> str:
    """Return the stable identity for one lifecycle's memory learning."""
    return f"learning-{_lifecycle_digest(thread_id, cycle_id, root_input_id)}"


def resolution_id_for(*, thread_id: str, cycle_id: int, root_input_id: str) -> str:
    """Return the stable identity for one lifecycle's resolved-issue case.

    One IssueThread can resolve several logical inputs, so a historical case
    belongs to a lifecycle rather than to `repo_id + issue_number`.
    """
    return f"resolution-{_lifecycle_digest(thread_id, cycle_id, root_input_id)}"


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_FTS_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how in into is it its of on
    or that the then there these this to was were what when where which who why with""".split()
)


def fts_match_expression(query: str, *, max_terms: int = 24) -> str:
    """Build a safe FTS5 MATCH expression from arbitrary human text.

    User and issue text is never passed to FTS5 directly: unbalanced quotes and
    bare operators are a syntax error, so tokens are extracted, bounded and
    quoted deterministically.
    """
    seen: list[str] = []
    for token in _FTS_TOKEN_RE.findall(query or ""):
        folded = token.casefold()
        if len(folded) < 3 or folded in _FTS_STOPWORDS or folded in seen:
            continue
        seen.append(folded)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{token}"' for token in seen)


def normalized_memory_fact(fact: str) -> str:
    """Collapse a proposed fact so replays hash to one candidate identity."""
    return " ".join(fact.split()).casefold()


def repo_memory_candidate_id_for(
    *,
    repo_id: int,
    thread_id: str,
    cycle_id: int,
    root_input_id: str,
    fact: str,
    evidence_path: str,
    evidence_start_line: int,
    evidence_end_line: int,
) -> str:
    """Return the stable identity for one execution-time memory proposal."""
    material = "\0".join(
        [
            str(repo_id),
            thread_id,
            str(cycle_id),
            root_input_id,
            normalized_memory_fact(fact),
            evidence_path,
            str(evidence_start_line),
            str(evidence_end_line),
        ]
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"repo-memory-candidate-{digest}"


@dataclass(frozen=True)
class ClaimedEvent:
    event_key: str
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    body: str
    workspace_path: str | None
    retrying: bool = False
    origin_surface: str = "ISSUE"
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
    review_state: str | None = None
    execution_id: str | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    event_key: str
    thread_id: str
    status: ExecutionStatus
    attempt_count: int
    started_at: str
    completed_at: str | None
    error_message: str | None
    execution_id: str | None = None
    cycle_id: int | None = None
    root_input_id: str | None = None


@dataclass(frozen=True)
class ExecutionAttemptRecord:
    attempt_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    attempt_number: int
    kind: AttemptKind
    repair_round: int
    parent_review_id: str | None
    authorization_id: str
    status: AttemptStatus
    response_text: str | None
    start_head_sha: str | None
    end_head_sha: str | None
    start_dirty: bool
    end_dirty: bool
    created_at: str
    completed_at: str | None
    retry_count: int
    repair_recovery_count: int
    review_recovery_count: int = 0


@dataclass(frozen=True)
class ExecutionToolEvidenceRecord:
    evidence_id: str
    attempt_id: str
    thread_id: str
    cycle_id: int
    sequence_number: int
    kind: str
    command: str
    exit_code: int | None
    output: str
    output_hash: str
    truncated: bool
    recorded_at: str


@dataclass(frozen=True)
class ExecutionReviewRecord:
    review_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    attempt_id: str
    review_iteration: int
    verdict: str
    summary: str
    findings_json: str
    repair_instructions_json: str
    created_at: str
    completed_at: str
    requirement_checks_json: str = "[]"
    inspection_json: str = "{}"
    challenge_json: str = "{}"
    read_ledger_json: str = "[]"


@dataclass(frozen=True)
class ThreadWorkspaceRecord:
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    source_repository_path: str
    workspace_path: str
    branch_name: str
    base_commit: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PublicationRecord:
    publication_id: str
    source_event_key: str
    thread_id: str
    cycle_id: int
    root_input_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    status: PublicationStatus
    branch_name: str
    local_commit_sha: str | None
    remote_commit_sha: str | None
    pr_number: int | None
    pr_url: str | None
    comment_id: int | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AcceptedTaskLifecycle:
    """One task's exact, final, result-approved lifecycle material."""

    cycle_id: int
    workflow_cycle_id: str
    cycle_kind: str
    revision_sequence: int | None
    task_id: str
    declaration_index: int
    plan_id: str
    plan_text: str
    execution_id: str
    execution_summary: str
    validation_id: str
    validation_summary: str


@dataclass(frozen=True)
class PublicationGeneration:
    """Accepted declarative cycles since the previous finalized publication."""

    publication_id: str
    thread_id: str
    first_cycle_id: int
    last_cycle_id: int
    cycle_ids: tuple[int, ...]
    previous_publication_id: str | None
    previous_commit_sha: str | None


@dataclass(frozen=True)
class PublicationTarget:
    """The exact lifecycle a publication is authorized to act for."""

    publication_id: str
    source_event_key: str
    thread_id: str
    cycle_id: int
    root_input_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    branch_name: str
    plan_id: str
    plan_version: int
    attempt_id: str
    review_id: str
    execution_completed_at: str | None
    workflow_cycle_id: str | None = None
    declarative: bool = False


@dataclass(frozen=True)
class WorkflowStateRecord:
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    phase: WorkflowPhase
    cycle_id: int
    root_event_key: str
    current_plan_id: str | None
    mode: WorkflowMode
    created_at: str
    updated_at: str
    response_surface: str = "ISSUE"
    response_subject_number: int | None = None
    response_comment_id: str | None = None
    response_url: str | None = None
    review_thread_root_id: str | None = None
    planning_feedback_event_key: str | None = None
    root_input_id: str | None = None


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    thread_id: str
    repo_id: int
    repo_full_name: str
    issue_number: int
    cycle_id: int
    version: int
    root_event_key: str
    plan_text: str
    status: PlanStatus
    created_at: str
    posted_at: str | None
    posted_comment_id: int | None
    approved_at: str | None
    approved_by: str | None
    approval_event_key: str | None
    root_input_id: str | None = None


@dataclass(frozen=True)
class ExecutionPermit:
    permit_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str
    source: PermitSource
    source_event_key: str | None
    created_at: str
    consumed_at: str | None
    invalidated_at: str | None


@dataclass(frozen=True)
class WorkflowInputRecord:
    event_key: str
    thread_id: str
    cycle_id: int
    purpose: InputPurpose
    status: str
    claimed_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class ClarificationRequestRecord:
    clarification_id: str
    thread_id: str
    cycle_id: int
    root_event_key: str
    occurrence_key: str
    requested_from_phase: str
    question: str
    reason: str
    answer_type: str
    choices_json: str
    origin_surface: str
    response_subject_number: int
    response_comment_id: str | None
    response_url: str | None
    review_thread_root_id: str | None
    status: str
    created_at: str
    answered_at: str | None
    answer_event_key: str | None
    answer_json: str | None


@dataclass(frozen=True)
class DeferredFollowupRecord:
    deferred_id: str
    source_event_key: str
    thread_id: str
    originating_cycle_id: int
    status: str
    residual_text: str | None
    queued_at: str
    consumed_cycle_id: int | None
    consumed_at: str | None


@dataclass(frozen=True)
class FeedbackReviewRecord:
    feedback_review_id: str
    source_event_key: str
    thread_id: str
    workflow_cycle_id: str
    task_run_id: str
    feedback_kind: str
    occurrence_key: str
    feedback_text: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkflowCommentOutboxRecord:
    outbox_id: str
    source_event_key: str
    thread_id: str
    message_kind: str
    stable_marker: str
    body: str
    status: str
    comment_id: int | None
    error_message: str | None
    created_at: str
    updated_at: str
    next_attempt_at: str
    delivered_at: str | None


@dataclass(frozen=True)
class RepoMemoryLearningRecord:
    learning_id: str
    source_event_key: str
    thread_id: str
    cycle_id: int
    root_input_id: str
    repo_id: int
    status: str
    accepted_candidates: int
    rejected_candidates: int
    error_message: str | None
    created_at: str
    updated_at: str
    proposal_json: str = "{}"
    attempt_count: int = 0


@dataclass(frozen=True)
class RepoMemoryCandidateRecord:
    candidate_id: str
    repo_id: int
    thread_id: str
    cycle_id: int
    root_input_id: str
    source_event_key: str
    category: str
    fact: str
    durability_reason: str
    evidence_path: str
    evidence_start_line: int
    evidence_end_line: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IssueMetadataRecord:
    repo_id: int
    issue_number: int
    title: str
    body: str
    observed_at: str


@dataclass(frozen=True)
class IssueResolutionRecord:
    resolution_id: str
    repo_id: int
    thread_id: str
    cycle_id: int
    root_input_id: str
    source_event_key: str
    issue_number: int
    issue_title: str
    issue_description_snapshot: str
    task_summary: str
    symptom_summary: str
    root_cause: str
    fix_summary: str
    affected_components_json: str
    changed_files_json: str
    validation_summary: str
    search_terms_json: str
    limitations: str
    plan_id: str | None
    execution_id: str | None
    review_id: str | None
    publication_id: str | None
    commit_sha: str | None
    pr_number: int | None
    pr_url: str | None
    status: str
    error_message: str | None
    attempt_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewRepairPermit:
    permit_id: str
    thread_id: str
    cycle_id: int
    plan_id: str
    plan_version: int
    root_event_key: str | None
    parent_review_id: str
    repair_round: int
    created_at: str
    consumed_at: str | None
    invalidated_at: str | None


class SQLiteGitHubStore:
    @staticmethod
    def _execution_target(
        db: sqlite3.Connection, *, thread_id: str, cycle_id: int, root_event_key: str
    ) -> str:
        row = db.execute(
            """SELECT root_input_id FROM issue_plans
               WHERE thread_id=? AND cycle_id=?
               ORDER BY version DESC LIMIT 1""",
            (thread_id, cycle_id),
        ).fetchone()
        root_input_id = (row["root_input_id"] if row else None) or root_event_key
        if root_input_id == root_event_key:
            return root_event_key
        return execution_id_for(
            thread_id=thread_id, cycle_id=cycle_id, root_input_id=root_input_id
        )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # LangGraph executes lifecycle tools on worker threads. The durable
        # workflow runtime is constructed on the dispatcher worker, so its
        # repository store must remain usable when a gateway tool calls back
        # from LangGraph's tool executor.
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            factory=_SerializedSQLiteConnection,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self._migrate_declarative_lifecycle()
        self._migrate_execution_baselines()
        self._migrate_post_execution_identity()
        self.connection.commit()

    def _migrate_declarative_lifecycle(self) -> None:
        """Add exact two-barrier workflow identity without rewriting old data."""
        thread_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(issue_threads)")
        }
        if "interaction_mode" not in thread_columns:
            self.connection.execute(
                "ALTER TABLE issue_threads ADD COLUMN interaction_mode "
                "TEXT NOT NULL DEFAULT 'MANUAL'"
            )
        if "config_generation_id" not in thread_columns:
            self.connection.execute(
                "ALTER TABLE issue_threads ADD COLUMN config_generation_id TEXT"
            )
        cycle_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(workflow_cycles_v1)")
        }
        if "cycle_kind" not in cycle_columns:
            self.connection.execute(
                "ALTER TABLE workflow_cycles_v1 ADD COLUMN cycle_kind "
                "TEXT NOT NULL DEFAULT 'INITIAL'"
            )
        if "revision_sequence" not in cycle_columns:
            self.connection.execute(
                "ALTER TABLE workflow_cycles_v1 ADD COLUMN revision_sequence INTEGER"
            )
        permit_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(workflow_task_permits_v1)"
            )
        }
        if "approval_mode" not in permit_columns:
            self.connection.execute(
                "ALTER TABLE workflow_task_permits_v1 ADD COLUMN approval_mode "
                "TEXT NOT NULL DEFAULT 'HUMAN'"
            )
        self.connection.execute(
            """UPDATE workflow_task_runs_v1
               SET status='WAITING_FOR_PLAN_APPROVAL'
               WHERE status='WAITING_FOR_APPROVAL'"""
        )
        self.connection.execute(
            """UPDATE workflow_task_runs_v1
               SET phase='WAITING_FOR_PLAN_APPROVAL'
               WHERE phase='WAITING_FOR_APPROVAL'"""
        )
        # Threads created by this version receive an explicit NOT_STARTED row
        # at ingestion.  Pre-feature rows are backfilled only from durable
        # lifecycle/publication evidence; otherwise they fail closed.
        for thread in self.connection.execute(
            """SELECT t.* FROM issue_threads AS t
               LEFT JOIN thread_workflow_lifecycle_v1 AS l
                 ON l.thread_id=t.thread_id WHERE l.thread_id IS NULL"""
        ).fetchall():
            initial = self.connection.execute(
                """SELECT * FROM workflow_cycles_v1
                   WHERE thread_id=? AND cycle_kind='INITIAL'
                   ORDER BY cycle_id LIMIT 1""",
                (thread["thread_id"],),
            ).fetchone()
            published = self.connection.execute(
                """SELECT 1 FROM logical_publications
                   WHERE thread_id=? AND status IN ('COMPLETED','NO_CHANGES')
                   LIMIT 1""",
                (thread["thread_id"],),
            ).fetchone()
            legacy_active = self.connection.execute(
                "SELECT 1 FROM issue_workflow_state WHERE thread_id=? LIMIT 1",
                (thread["thread_id"],),
            ).fetchone()
            root = self.connection.execute(
                """SELECT event_key FROM source_events WHERE thread_id=?
                   ORDER BY source_updated_at,discovered_at,event_key LIMIT 1""",
                (thread["thread_id"],),
            ).fetchone()
            if root is None:
                continue
            if published:
                state = "PUBLISHED"
            elif initial is not None:
                state = "ACTIVE" if initial["status"] == "ACTIVE" else "COMPLETE"
            elif legacy_active:
                state = "ACTIVE"
            else:
                state = "LEGACY_BLOCKED"
            self.connection.execute(
                """INSERT OR IGNORE INTO thread_workflow_lifecycle_v1(
                   thread_id,initial_state,initial_root_event_key,
                   initial_workflow_cycle_id,initial_workflow_digest,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                (
                    thread["thread_id"],
                    state,
                    root["event_key"],
                    initial["workflow_cycle_id"] if initial else None,
                    initial["workflow_digest"] if initial else None,
                    thread["created_at"],
                    thread["updated_at"],
                ),
            )

    def _migrate_execution_baselines(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(event_executions)")
        }
        migrations = {
            "start_head_sha": (
                "ALTER TABLE event_executions ADD COLUMN start_head_sha TEXT"
            ),
            "end_head_sha": (
                "ALTER TABLE event_executions ADD COLUMN end_head_sha TEXT"
            ),
            "end_dirty": (
                "ALTER TABLE event_executions ADD COLUMN "
                "end_dirty INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.connection.execute(statement)
        review_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_reviews)")
        }
        if "requirement_checks_json" not in review_columns:
            self.connection.execute(
                "ALTER TABLE execution_reviews ADD COLUMN "
                "requirement_checks_json TEXT NOT NULL DEFAULT '[]'"
            )
        for column, statement in {
            "inspection_json": (
                "ALTER TABLE execution_reviews ADD COLUMN inspection_json "
                "TEXT NOT NULL DEFAULT '{}'"
            ),
            "challenge_json": (
                "ALTER TABLE execution_reviews ADD COLUMN challenge_json "
                "TEXT NOT NULL DEFAULT '{}'"
            ),
            "read_ledger_json": (
                "ALTER TABLE execution_reviews ADD COLUMN read_ledger_json "
                "TEXT NOT NULL DEFAULT '[]'"
            ),
        }.items():
            if column not in review_columns:
                self.connection.execute(statement)
        attempt_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_attempts)")
        }
        if "repair_recovery_count" not in attempt_columns:
            self.connection.execute(
                "ALTER TABLE execution_attempts ADD COLUMN "
                "repair_recovery_count INTEGER NOT NULL DEFAULT 0"
            )
        if "review_recovery_count" not in attempt_columns:
            self.connection.execute(
                "ALTER TABLE execution_attempts ADD COLUMN "
                "review_recovery_count INTEGER NOT NULL DEFAULT 0"
            )
        repair_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(review_repair_permits)"
            )
        }
        if "root_event_key" not in repair_columns:
            self.connection.execute(
                "ALTER TABLE review_repair_permits ADD COLUMN root_event_key TEXT"
            )
        self.connection.execute(
            """UPDATE review_repair_permits SET root_event_key = (
               SELECT r.root_event_key FROM execution_reviews r
               WHERE r.review_id = review_repair_permits.parent_review_id)
               WHERE root_event_key IS NULL"""
        )
        permit_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_permits)")
        }
        memory_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(repo_memory_learning)"
            )
        }
        deferred_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(deferred_followups)")
        }
        clarification_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(clarification_requests)"
            )
        }
        workflow_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(issue_workflow_state)"
            )
        }
        if "root_input_id" not in workflow_columns:
            self.connection.execute(
                "ALTER TABLE issue_workflow_state ADD COLUMN root_input_id TEXT"
            )
        plan_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(issue_plans)")
        }
        if "root_input_id" not in plan_columns:
            self.connection.execute(
                "ALTER TABLE issue_plans ADD COLUMN root_input_id TEXT"
            )
        if "occurrence_key" not in clarification_columns:
            self.connection.execute(
                "ALTER TABLE clarification_requests ADD COLUMN occurrence_key "
                "TEXT NOT NULL DEFAULT ''"
            )
        deferred_pk = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(deferred_followups)")
            if row[5]
        }
        if "source_event_key" in deferred_pk:
            # 76e3504 made source_event_key the table primary key.  Adding a
            # column cannot remove that constraint, so rebuild the table.
            self.connection.execute("PRAGMA foreign_keys=OFF")
            self.connection.execute(
                "ALTER TABLE deferred_followups RENAME TO deferred_followups_legacy"
            )
            self.connection.execute(
                """CREATE TABLE deferred_followups(
                   deferred_id TEXT PRIMARY KEY,
                   source_event_key TEXT NOT NULL REFERENCES source_events(event_key),
                   thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
                   originating_cycle_id INTEGER NOT NULL,
                   status TEXT NOT NULL DEFAULT 'QUEUED',
                   residual_text TEXT,
                   queued_at TEXT NOT NULL,
                   consumed_cycle_id INTEGER,
                   consumed_at TEXT)"""
            )
            legacy_columns = {
                row[1]
                for row in self.connection.execute(
                    "PRAGMA table_info(deferred_followups_legacy)"
                )
            }
            residual = "residual_text" if "residual_text" in legacy_columns else "NULL"
            deferred_id = (
                "deferred_id"
                if "deferred_id" in legacy_columns
                else "'deferred-' || substr(hex(randomblob(16)), 1, 24)"
            )
            self.connection.execute(
                f"""INSERT INTO deferred_followups(
                   deferred_id, source_event_key, thread_id, originating_cycle_id,
                   status, residual_text, queued_at, consumed_cycle_id, consumed_at)
                   SELECT {deferred_id},
                          source_event_key, thread_id, originating_cycle_id,
                          status, {residual}, queued_at, consumed_cycle_id, consumed_at
                   FROM deferred_followups_legacy"""
            )
            # Replace random backfill IDs with deterministic IDs, including
            # rowid as a tie-breaker for any legacy duplicate rows.
            if "deferred_id" not in legacy_columns:
                for row in self.connection.execute(
                    "SELECT rowid, source_event_key, residual_text FROM deferred_followups"
                ).fetchall():
                    suffix = f"{row[1]}\0{row[2] or ''}\0{row[0]}"
                    self.connection.execute(
                        "UPDATE deferred_followups SET deferred_id=? WHERE rowid=?",
                        (
                            "deferred-"
                            + hashlib.sha256(suffix.encode()).hexdigest()[:24],
                            row[0],
                        ),
                    )
            self.connection.execute("DROP TABLE deferred_followups_legacy")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deferred_followups_source "
                "ON deferred_followups(source_event_key)"
            )
            self.connection.execute("PRAGMA foreign_keys=ON")
        else:
            if "residual_text" not in deferred_columns:
                self.connection.execute(
                    "ALTER TABLE deferred_followups ADD COLUMN residual_text TEXT"
                )
            if "deferred_id" not in deferred_columns:
                self.connection.execute(
                    "ALTER TABLE deferred_followups ADD COLUMN deferred_id TEXT"
                )
                for row in self.connection.execute(
                    "SELECT rowid, source_event_key FROM deferred_followups WHERE deferred_id IS NULL"
                ).fetchall():
                    deferred_id = (
                        "deferred-"
                        + hashlib.sha256(f"{row[1]}\0{row[0]}".encode()).hexdigest()[
                            :24
                        ]
                    )
                    self.connection.execute(
                        "UPDATE deferred_followups SET deferred_id=? WHERE rowid=?",
                        (deferred_id, row[0]),
                    )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deferred_followups_id "
            "ON deferred_followups(deferred_id)"
        )
        if "proposal_json" not in memory_columns:
            self.connection.execute(
                "ALTER TABLE repo_memory_learning ADD COLUMN proposal_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "root_event_key" not in permit_columns:
            self.connection.execute(
                "ALTER TABLE execution_permits ADD COLUMN root_event_key TEXT"
            )
            self.connection.execute(
                """UPDATE execution_permits SET root_event_key=(
                   SELECT root_event_key FROM issue_plans
                   WHERE issue_plans.plan_id=execution_permits.plan_id)"""
            )
        # Successful executions created before the review gate must never be
        # published implicitly after an upgrade.  Only an exact permit/plan
        # binding is recoverable; all other legacy rows fail closed.
        exact_recovery = """
            EXISTS (
                SELECT 1 FROM execution_permits p
                JOIN issue_plans pplan ON pplan.plan_id = p.plan_id
                JOIN event_executions e ON e.event_key = p.root_event_key
                WHERE p.thread_id = issue_workflow_state.thread_id
                  AND p.cycle_id = issue_workflow_state.cycle_id
                  AND p.plan_id = issue_workflow_state.current_plan_id
                  AND p.root_event_key = issue_workflow_state.root_event_key
                  AND p.plan_version = pplan.version
                  AND pplan.status IN ('APPROVED', 'AUTO_APPROVED')
                  AND e.status = 'SUCCEEDED'
            )
        """
        self.connection.execute(
            f"""UPDATE issue_workflow_state SET phase = ?
                WHERE phase = ? AND NOT EXISTS (
                    SELECT 1 FROM execution_reviews r
                    WHERE r.thread_id = issue_workflow_state.thread_id
                      AND r.cycle_id = issue_workflow_state.cycle_id
                      AND r.root_event_key = issue_workflow_state.root_event_key
                      AND r.verdict = 'ACCEPT'
                ) AND {exact_recovery}""",
            (
                WorkflowPhase.REVIEW_EXECUTION.value,
                WorkflowPhase.AWAITING_PUBLICATION.value,
            ),
        )
        self.connection.execute(
            f"""UPDATE issue_workflow_state SET phase = ?
                WHERE phase = ? AND NOT EXISTS (
                    SELECT 1 FROM execution_reviews r
                    WHERE r.thread_id = issue_workflow_state.thread_id
                      AND r.cycle_id = issue_workflow_state.cycle_id
                      AND r.root_event_key = issue_workflow_state.root_event_key
                      AND r.verdict = 'ACCEPT'
                ) AND NOT ({exact_recovery})""",
            (
                WorkflowPhase.REVIEW_BLOCKED.value,
                WorkflowPhase.AWAITING_PUBLICATION.value,
            ),
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO execution_attempts(
               attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
               attempt_number,kind,repair_round,authorization_id,status,response_text,
               start_head_sha,end_head_sha,end_dirty,created_at,completed_at)
               SELECT 'attempt-' || p.permit_id, s.thread_id, s.cycle_id, p.plan_id,
                      p.plan_version, p.root_event_key, 1, 'INITIAL', 0, p.permit_id,
                      'SUCCEEDED', e.response_text, e.start_head_sha, e.end_head_sha,
                      e.end_dirty, e.started_at, e.completed_at
               FROM issue_workflow_state s
               JOIN execution_permits p ON p.thread_id=s.thread_id AND p.cycle_id=s.cycle_id
                 AND p.plan_id=s.current_plan_id
               JOIN event_executions e ON e.event_key=p.root_event_key
               WHERE e.status='SUCCEEDED'"""
        )
        self.connection.execute(
            """UPDATE issue_workflow_state SET phase=?
               WHERE phase='EXECUTING' AND EXISTS (
                 SELECT 1 FROM event_executions e
                 WHERE e.event_key=issue_workflow_state.root_event_key
                   AND e.status='SUCCEEDED')""",
            (WorkflowPhase.REVIEW_EXECUTION.value,),
        )
        source_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(source_events)")
        }
        source_migrations = {
            "source_created_at": (
                "ALTER TABLE source_events ADD COLUMN source_created_at TEXT"
            ),
            "origin_surface": (
                "ALTER TABLE source_events ADD COLUMN origin_surface TEXT "
                "NOT NULL DEFAULT 'ISSUE'"
            ),
            "path": "ALTER TABLE source_events ADD COLUMN path TEXT",
            "line": "ALTER TABLE source_events ADD COLUMN line INTEGER",
            "start_line": "ALTER TABLE source_events ADD COLUMN start_line INTEGER",
            "side": "ALTER TABLE source_events ADD COLUMN side TEXT",
            "start_side": "ALTER TABLE source_events ADD COLUMN start_side TEXT",
            "diff_hunk": "ALTER TABLE source_events ADD COLUMN diff_hunk TEXT",
            "commit_id": "ALTER TABLE source_events ADD COLUMN commit_id TEXT",
            "original_commit_id": (
                "ALTER TABLE source_events ADD COLUMN original_commit_id TEXT"
            ),
            "in_reply_to_id": (
                "ALTER TABLE source_events ADD COLUMN in_reply_to_id TEXT"
            ),
            "pull_request_review_id": (
                "ALTER TABLE source_events ADD COLUMN pull_request_review_id TEXT"
            ),
            "review_thread_root_id": (
                "ALTER TABLE source_events ADD COLUMN review_thread_root_id TEXT"
            ),
            "review_state": "ALTER TABLE source_events ADD COLUMN review_state TEXT",
        }
        for column, statement in source_migrations.items():
            if column not in source_columns:
                self.connection.execute(statement)
        workflow_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(issue_workflow_state)"
            )
        }
        workflow_migrations = {
            "response_surface": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_surface TEXT "
                "NOT NULL DEFAULT 'ISSUE'"
            ),
            "response_subject_number": (
                "ALTER TABLE issue_workflow_state ADD COLUMN "
                "response_subject_number INTEGER"
            ),
            "response_comment_id": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_comment_id TEXT"
            ),
            "response_url": (
                "ALTER TABLE issue_workflow_state ADD COLUMN response_url TEXT"
            ),
            "review_thread_root_id": (
                "ALTER TABLE issue_workflow_state ADD COLUMN review_thread_root_id TEXT"
            ),
            "planning_feedback_event_key": (
                "ALTER TABLE issue_workflow_state ADD COLUMN "
                "planning_feedback_event_key TEXT"
            ),
        }
        for column, statement in workflow_migrations.items():
            if column not in workflow_columns:
                self.connection.execute(statement)
        permit_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(execution_permits)")
        }
        if "root_event_key" not in permit_columns:
            self.connection.execute(
                "ALTER TABLE execution_permits ADD COLUMN root_event_key TEXT"
            )
        self.connection.execute(
            """UPDATE execution_permits SET root_event_key = (
               SELECT root_event_key FROM issue_plans
               WHERE issue_plans.plan_id = execution_permits.plan_id
            ) WHERE root_event_key IS NULL"""
        )

    def _legacy_publication_cycle(self, thread_id: str, event_key: str) -> int:
        """Deterministically recover the cycle an event-keyed publication served."""
        row = self.connection.execute(
            """SELECT cycle_id FROM issue_workflow_state
               WHERE thread_id = ? AND root_event_key = ?""",
            (thread_id, event_key),
        ).fetchone()
        if row is not None:
            return int(row["cycle_id"])
        row = self.connection.execute(
            """SELECT MAX(cycle_id) AS cycle_id FROM issue_plans
               WHERE thread_id = ? AND root_event_key = ?""",
            (thread_id, event_key),
        ).fetchone()
        if row is not None and row["cycle_id"] is not None:
            return int(row["cycle_id"])
        return 1

    def _migrate_post_execution_identity(self) -> None:
        """Move event-keyed publication/learning rows onto lifecycle identity.

        Legacy rows are ordinary lifecycles, so their logical input is their
        event key.  Every field is preserved and the SourceEvent is retained as
        provenance; the migration is idempotent because it runs only while the
        legacy shape is still present.
        """
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "event_publications" in tables:
            for row in self.connection.execute(
                "SELECT * FROM event_publications"
            ).fetchall():
                cycle_id = self._legacy_publication_cycle(
                    row["thread_id"], row["event_key"]
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO logical_publications(
                       publication_id, source_event_key, thread_id, cycle_id,
                       root_input_id, repo_id, repo_full_name, issue_number,
                       status, branch_name, local_commit_sha, remote_commit_sha,
                       pr_number, pr_url, comment_id, error_message,
                       created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        publication_id_for(
                            thread_id=row["thread_id"],
                            cycle_id=cycle_id,
                            root_input_id=row["event_key"],
                        ),
                        row["event_key"],
                        row["thread_id"],
                        cycle_id,
                        row["event_key"],
                        row["repo_id"],
                        row["repo_full_name"],
                        row["issue_number"],
                        row["status"],
                        row["branch_name"],
                        row["local_commit_sha"],
                        row["remote_commit_sha"],
                        row["pr_number"],
                        row["pr_url"],
                        row["comment_id"],
                        row["error_message"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
            self.connection.execute("DROP TABLE event_publications")
        memory_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(repo_memory_learning)"
            )
        }
        if "event_key" in memory_columns:
            legacy = self.connection.execute(
                "SELECT * FROM repo_memory_learning"
            ).fetchall()
            self.connection.execute("DROP TABLE repo_memory_learning")
            self.connection.execute(REPO_MEMORY_LEARNING_DDL)
            for row in legacy:
                cycle_id = int(row["cycle_id"])
                self.connection.execute(
                    """INSERT OR IGNORE INTO repo_memory_learning(
                       learning_id, source_event_key, thread_id, cycle_id,
                       root_input_id, repo_id, status, accepted_candidates,
                       rejected_candidates, proposal_json, error_message,
                       attempt_count, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                    (
                        memory_learning_id_for(
                            thread_id=row["thread_id"],
                            cycle_id=cycle_id,
                            root_input_id=row["event_key"],
                        ),
                        row["event_key"],
                        row["thread_id"],
                        cycle_id,
                        row["event_key"],
                        row["repo_id"],
                        row["status"],
                        row["accepted_candidates"],
                        row["rejected_candidates"],
                        row["proposal_json"],
                        row["error_message"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
        if "attempt_count" not in {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(repo_memory_learning)"
            )
        }:
            self.connection.execute(
                "ALTER TABLE repo_memory_learning ADD COLUMN "
                "attempt_count INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_repo_memory_learning_source "
            "ON repo_memory_learning(source_event_key)"
        )

    # ------------------------------------------------------------------
    # Durable attempt budgets
    #
    # Model-dependent learning must consume its budget BEFORE the external
    # call, so a hard crash mid-call cannot reset the budget and retry forever.
    # ------------------------------------------------------------------

    def _claim_attempt(self, table: str, key_column: str, key: str) -> int:
        with self.transaction(immediate=True) as db:
            cursor = db.execute(
                f"UPDATE {table} SET attempt_count = attempt_count + 1 "
                f"WHERE {key_column} = ?",
                (key,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"{table} record does not exist")
            row = db.execute(
                f"SELECT attempt_count FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
        return int(row["attempt_count"])

    def claim_memory_learning_attempt(self, learning_id: str) -> int:
        """Durably consume one repository-learning attempt before curating."""
        return self._claim_attempt("repo_memory_learning", "learning_id", learning_id)

    def claim_issue_resolution_attempt(self, resolution_id: str) -> int:
        """Durably consume one resolved-issue attempt before curating."""
        return self._claim_attempt(
            "issue_resolution_memory", "resolution_id", resolution_id
        )

    # ------------------------------------------------------------------
    # Execution-time repository-memory proposals
    #
    # A proposal only nominates WHERE evidence lives.  The application derives
    # what those lines contain and the existing validator remains the sole
    # writer of durable repository memory.
    # ------------------------------------------------------------------

    def save_repo_memory_candidate(
        self, record: RepoMemoryCandidateRecord
    ) -> RepoMemoryCandidateRecord:
        expected = repo_memory_candidate_id_for(
            repo_id=record.repo_id,
            thread_id=record.thread_id,
            cycle_id=record.cycle_id,
            root_input_id=record.root_input_id,
            fact=record.fact,
            evidence_path=record.evidence_path,
            evidence_start_line=record.evidence_start_line,
            evidence_end_line=record.evidence_end_line,
        )
        if record.candidate_id != expected:
            raise ValueError("repository memory candidate id does not match evidence")
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO repo_memory_candidates(
                   candidate_id, repo_id, thread_id, cycle_id, root_input_id,
                   source_event_key, category, fact, durability_reason,
                   evidence_path, evidence_start_line, evidence_end_line,
                   status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     status=excluded.status, updated_at=excluded.updated_at""",
                (
                    record.candidate_id,
                    record.repo_id,
                    record.thread_id,
                    record.cycle_id,
                    record.root_input_id,
                    record.source_event_key,
                    record.category,
                    record.fact,
                    record.durability_reason,
                    record.evidence_path,
                    record.evidence_start_line,
                    record.evidence_end_line,
                    record.status,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return self.repo_memory_candidate(record.candidate_id)  # type: ignore[return-value]

    def repo_memory_candidate(
        self, candidate_id: str
    ) -> RepoMemoryCandidateRecord | None:
        row = self.connection.execute(
            "SELECT * FROM repo_memory_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return RepoMemoryCandidateRecord(**dict(row)) if row else None

    def repo_memory_candidates_for_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str | None,
    ) -> list[RepoMemoryCandidateRecord]:
        rows = self.connection.execute(
            """SELECT * FROM repo_memory_candidates
               WHERE thread_id = ? AND cycle_id = ? AND root_input_id = ?
               ORDER BY created_at, candidate_id""",
            (thread_id, cycle_id, root_input_id or root_event_key),
        ).fetchall()
        return [RepoMemoryCandidateRecord(**dict(row)) for row in rows]

    def set_repo_memory_candidate_status(
        self, candidate_id: str, *, status: str, now: str
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE repo_memory_candidates SET status = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (status, now, candidate_id),
            )

    # ------------------------------------------------------------------
    # Canonical issue metadata snapshots
    # ------------------------------------------------------------------

    def upsert_issue_metadata(
        self,
        *,
        repo_id: int,
        issue_number: int,
        title: str,
        body: str,
        observed_at: str,
    ) -> None:
        """Record the newest observed issue title/body seen during ingestion."""
        with self.transaction() as db:
            db.execute(
                """INSERT INTO issue_metadata(
                   repo_id, issue_number, title, body, observed_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(repo_id, issue_number) DO UPDATE SET
                     title=excluded.title, body=excluded.body,
                     observed_at=excluded.observed_at
                   WHERE excluded.observed_at >= issue_metadata.observed_at""",
                (repo_id, issue_number, title or "", body or "", observed_at),
            )

    def issue_metadata(
        self, *, repo_id: int, issue_number: int
    ) -> IssueMetadataRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_metadata WHERE repo_id = ? AND issue_number = ?",
            (repo_id, issue_number),
        ).fetchone()
        return IssueMetadataRecord(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # Resolved-issue (historical case) memory
    # ------------------------------------------------------------------

    def issue_resolution(self, resolution_id: str) -> IssueResolutionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_resolution_memory WHERE resolution_id = ?",
            (resolution_id,),
        ).fetchone()
        return IssueResolutionRecord(**dict(row)) if row else None

    def issue_resolution_for_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str | None,
    ) -> IssueResolutionRecord | None:
        return self.issue_resolution(
            resolution_id_for(
                thread_id=thread_id,
                cycle_id=cycle_id,
                root_input_id=root_input_id or root_event_key,
            )
        )

    def pending_issue_resolution(self, thread_id: str) -> IssueResolutionRecord | None:
        row = self.connection.execute(
            """SELECT * FROM issue_resolution_memory
               WHERE thread_id = ? AND status IN ('PENDING', 'FAILED')
                 AND attempt_count < ?
               ORDER BY cycle_id, created_at, resolution_id LIMIT 1""",
            (thread_id, MAX_ISSUE_RESOLUTION_ATTEMPTS),
        ).fetchone()
        return IssueResolutionRecord(**dict(row)) if row else None

    def save_issue_resolution(
        self, record: IssueResolutionRecord
    ) -> IssueResolutionRecord:
        expected = resolution_id_for(
            thread_id=record.thread_id,
            cycle_id=record.cycle_id,
            root_input_id=record.root_input_id,
        )
        if record.resolution_id != expected:
            raise ValueError("resolution id does not match its lifecycle")
        columns = [field for field in IssueResolutionRecord.__dataclass_fields__]
        assignments = ", ".join(
            f"{name}=excluded.{name}" for name in columns if name != "resolution_id"
        )
        with self.transaction(immediate=True) as db:
            db.execute(
                f"""INSERT INTO issue_resolution_memory({", ".join(columns)})
                    VALUES({", ".join("?" for _ in columns)})
                    ON CONFLICT(resolution_id) DO UPDATE SET {assignments}""",
                [getattr(record, name) for name in columns],
            )
            self._index_issue_resolution(db, record)
        return self.issue_resolution(record.resolution_id)  # type: ignore[return-value]

    @staticmethod
    def _resolution_haystack(record: IssueResolutionRecord) -> str:
        try:
            components = " ".join(json.loads(record.affected_components_json or "[]"))
            terms = " ".join(json.loads(record.search_terms_json or "[]"))
            changed = " ".join(json.loads(record.changed_files_json or "[]"))
        except (TypeError, ValueError):
            components = terms = changed = ""
        return "\n".join(
            part
            for part in (
                record.issue_title,
                record.issue_description_snapshot,
                record.task_summary,
                record.symptom_summary,
                record.root_cause,
                record.fix_summary,
                record.validation_summary,
                components,
                terms,
                changed,
            )
            if part
        )

    def _index_issue_resolution(
        self, db: sqlite3.Connection, record: IssueResolutionRecord
    ) -> None:
        db.execute(
            "DELETE FROM issue_resolution_fts WHERE resolution_id = ?",
            (record.resolution_id,),
        )
        # Only terminal, genuinely resolved cases are retrievable.
        if record.status != IssueResolutionStatus.COMPLETED.value:
            return
        db.execute(
            "INSERT INTO issue_resolution_fts(resolution_id, haystack) VALUES(?, ?)",
            (record.resolution_id, self._resolution_haystack(record)),
        )

    def rebuild_issue_resolution_index(self) -> int:
        """Recreate the derived search index from the authoritative rows."""
        with self.transaction(immediate=True) as db:
            db.execute("DELETE FROM issue_resolution_fts")
            rows = db.execute(
                "SELECT * FROM issue_resolution_memory ORDER BY resolution_id"
            ).fetchall()
            for row in rows:
                self._index_issue_resolution(db, IssueResolutionRecord(**dict(row)))
            indexed = db.execute(
                "SELECT count(*) AS total FROM issue_resolution_fts"
            ).fetchone()
        return int(indexed["total"])

    def search_issue_resolutions(
        self,
        *,
        repo_id: int,
        query: str,
        limit: int = 5,
        per_thread_limit: int | None = None,
    ) -> list[IssueResolutionRecord]:
        """Rank completed cases from ONE repository by lexical relevance.

        `repo_id` comes from authoritative context and is applied against the
        base rows, so the derived index can never widen repository scope.
        """
        expression = fts_match_expression(query)
        bounded = max(0, min(int(limit), 25))
        if not expression or not bounded:
            return []
        rows = self.connection.execute(
            """SELECT base.*, bm25(issue_resolution_fts) AS relevance
               FROM issue_resolution_fts
               JOIN issue_resolution_memory AS base
                 ON base.resolution_id = issue_resolution_fts.resolution_id
               WHERE issue_resolution_fts MATCH ?
                 AND base.repo_id = ? AND base.status = ?
               ORDER BY relevance, base.resolution_id""",
            (expression, repo_id, IssueResolutionStatus.COMPLETED.value),
        ).fetchall()
        selected: list[IssueResolutionRecord] = []
        per_thread: dict[str, int] = {}
        for row in rows:
            values = dict(row)
            values.pop("relevance", None)
            record = IssueResolutionRecord(**values)
            if per_thread_limit is not None:
                seen = per_thread.get(record.thread_id, 0)
                if seen >= per_thread_limit:
                    continue
                per_thread[record.thread_id] = seen + 1
            selected.append(record)
            if len(selected) >= bounded:
                break
        return selected

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connection.serialized():
            try:
                self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def upsert_repository(self, repo_id: int, full_name: str, observed_at: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO repositories(repo_id, full_name, observed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(repo_id) DO UPDATE SET full_name=excluded.full_name,
                   observed_at=excluded.observed_at""",
                (repo_id, full_name, observed_at),
            )

    def repository_id_for_full_name(self, full_name: str) -> int | None:
        """Resolve an observed repository name to its stable GitHub ID."""
        row = self.connection.execute(
            "SELECT repo_id FROM repositories WHERE full_name = ?", (full_name,)
        ).fetchone()
        return int(row["repo_id"]) if row is not None else None

    def cursor(self, repo_id: int, stream: str):
        return self.connection.execute(
            "SELECT since, etag, last_successful_poll_at FROM poll_cursors "
            "WHERE repo_id = ? AND stream = ?",
            (repo_id, stream),
        ).fetchone()

    def record_batch(
        self,
        repo_id: int,
        stream: str,
        events: list[SourceEvent],
        *,
        since: str,
        etag: str | None,
        polled_at: str,
    ) -> RecordBatchResult:
        result = RecordBatchResult()
        with self.transaction() as db:
            self._repair_redundant_issue_events(db, now=polled_at)
            for event in events:
                if event.repo_id != repo_id:
                    raise ValueError("event repository does not match the batch")
                if db.execute(
                    "SELECT 1 FROM source_events WHERE event_key = ?",
                    (event.event_key,),
                ).fetchone():
                    continue
                if not self._is_new_issue_content(db, event):
                    continue
                if (
                    event.source_kind.value == "issue"
                    and not is_actionable_source_event(event.source_kind, event.body)
                ):
                    continue
                if self._has_resolved_issue_snapshot(db, event):
                    continue
                thread_id = self._resolve_thread(db, event, polled_at, result)
                db.execute(
                    """INSERT OR IGNORE INTO source_events(
                       event_key, repo_id, repo_full_name, source_kind, source_id,
                       source_updated_at, source_created_at, subject_kind,
                       subject_number, author_login,
                       body, html_url, thread_id, discovered_at, origin_surface,
                       path, line, start_line, side, start_side, diff_hunk,
                       commit_id, original_commit_id, in_reply_to_id,
                       pull_request_review_id, review_thread_root_id, review_state)
                       VALUES (?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_key,
                        event.repo_id,
                        event.repo_full_name,
                        event.source_kind.value,
                        event.source_id,
                        event.source_updated_at,
                        event.source_created_at,
                        event.subject_kind.value,
                        event.subject_number,
                        event.author_login,
                        event.body,
                        event.html_url,
                        thread_id,
                        polled_at,
                        event.origin_surface.value,
                        event.path,
                        event.line,
                        event.start_line,
                        event.side,
                        event.start_side,
                        event.diff_hunk,
                        event.commit_id,
                        event.original_commit_id,
                        event.in_reply_to_id,
                        event.pull_request_review_id,
                        event.review_thread_root_id,
                        event.review_state,
                    ),
                )
                if thread_id is not None:
                    db.execute(
                        """INSERT OR IGNORE INTO thread_workflow_lifecycle_v1(
                           thread_id,initial_state,initial_root_event_key,
                           created_at,updated_at) VALUES(?,'NOT_STARTED',?,?,?)""",
                        (thread_id, event.event_key, polled_at, polled_at),
                    )
                    self._classify_revision_input(
                        db,
                        event_key=event.event_key,
                        thread_id=thread_id,
                        queued_at=polled_at,
                    )
                result.events_persisted += 1
            db.execute(
                """INSERT INTO poll_cursors(repo_id, stream, since, etag,
                   last_successful_poll_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repo_id, stream) DO UPDATE SET since=excluded.since,
                   etag=excluded.etag,
                   last_successful_poll_at=excluded.last_successful_poll_at""",
                (repo_id, stream, since, etag, polled_at),
            )
        # Announced only after the transaction commits: emitting inside it
        # would claim an ingestion a rollback could erase.
        for thread_id in result.created_thread_ids:
            emit(
                EventKind.ROOT_INGESTED,
                thread_id=thread_id,
                repo_id=repo_id,
                source_kind=stream,
            )
        return result

    @classmethod
    def _classify_revision_input(
        cls,
        db: sqlite3.Connection,
        *,
        event_key: str,
        thread_id: str,
        queued_at: str,
    ) -> str:
        """Classify one durable input without consulting model/checkpoint state."""
        event = db.execute(
            "SELECT * FROM source_events WHERE event_key=?", (event_key,)
        ).fetchone()
        lifecycle = db.execute(
            "SELECT * FROM thread_workflow_lifecycle_v1 WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if event is None or lifecycle is None:
            raise RuntimeError("routed SourceEvent lifecycle is missing")
        if event_key == lifecycle["initial_root_event_key"]:
            return "INITIAL_ROOT"
        if not is_actionable_source_event(event["source_kind"], event["body"]):
            return "IGNORED"
        has_declarative = db.execute(
            "SELECT 1 FROM workflow_cycles_v1 WHERE thread_id=? LIMIT 1",
            (thread_id,),
        ).fetchone()
        legacy_state = db.execute(
            "SELECT 1 FROM issue_workflow_state WHERE thread_id=? LIMIT 1",
            (thread_id,),
        ).fetchone()
        # Until application code chooses the initial runtime, retain exact
        # SourceEvent order without guessing whether the legacy or declarative
        # controller will own this thread. The selected controller classifies
        # it before the next model invocation.
        if (
            lifecycle["initial_state"] == "NOT_STARTED"
            and has_declarative is None
            and legacy_state is None
        ):
            return "PENDING_INITIAL_SELECTION"
        if has_declarative is None and legacy_state is not None:
            return "LEGACY_WORKFLOW_INPUT"
        expected = cls._matches_open_interaction(db, event, thread_id)
        if expected:
            return "EXPECTED_RESPONSE"
        if is_exact_agent_approval(event["body"]):
            cls._record_disposition_sql(
                db,
                event_key=event_key,
                thread_id=thread_id,
                cycle_id=cls._latest_cycle_id_sql(db, thread_id),
                status="STALE_APPROVAL",
                recorded_at=queued_at,
            )
            return "STALE_APPROVAL"
        revision_input_id = cls.revision_input_id_for(event_key)
        db.execute(
            """INSERT OR IGNORE INTO revision_inputs_v1(
               revision_input_id,source_event_key,thread_id,residual_text,
               classification_reason,status,queued_at)
               VALUES(?,?,?,NULL,'UNSOLICITED_STEERING','PENDING',?)""",
            (revision_input_id, event_key, thread_id, queued_at),
        )
        cls._record_disposition_sql(
            db,
            event_key=event_key,
            thread_id=thread_id,
            cycle_id=cls._latest_cycle_id_sql(db, thread_id),
            status="REVISION_QUEUED",
            recorded_at=queued_at,
        )
        marker = revision_ack_marker(revision_input_id)
        cls._enqueue_workflow_comment_sql(
            db,
            source_event_key=event_key,
            thread_id=thread_id,
            message_kind="REVISION_INPUT_ACK",
            marker=marker,
            body=f"{marker}\n{UNSOLICITED_ACK_MESSAGE}",
            now=queued_at,
        )
        return "REVISION_QUEUED"

    @staticmethod
    def revision_input_id_for(event_key: str) -> str:
        return "revision-input-" + hashlib.sha256(event_key.encode()).hexdigest()[:24]

    @staticmethod
    def _enqueue_workflow_comment_sql(
        db: sqlite3.Connection,
        *,
        source_event_key: str,
        thread_id: str,
        message_kind: str,
        marker: str,
        body: str,
        now: str,
    ) -> None:
        db.execute(
            """INSERT OR IGNORE INTO workflow_comment_outbox_v1(
               outbox_id,source_event_key,thread_id,message_kind,stable_marker,
               body,status,created_at,updated_at,next_attempt_at)
               VALUES(?,?,?,?,?,?,'PENDING',?,?,?)""",
            (
                outbox_id_for(
                    source_event_key=source_event_key, message_kind=message_kind
                ),
                source_event_key,
                thread_id,
                message_kind,
                marker,
                body,
                now,
                now,
                now,
            ),
        )

    @staticmethod
    def _latest_cycle_id_sql(db: sqlite3.Connection, thread_id: str) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(cycle_id),0) FROM workflow_cycles_v1 WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _record_disposition_sql(
        db: sqlite3.Connection,
        *,
        event_key: str,
        thread_id: str,
        cycle_id: int,
        status: str,
        recorded_at: str,
    ) -> None:
        db.execute(
            """INSERT INTO thread_input_consumptions(
               event_key,thread_id,cycle_id,purpose,status,claimed_at,consumed_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (
                event_key,
                thread_id,
                cycle_id,
                InputPurpose.CLARIFICATION_ROUTED.value,
                status,
                recorded_at,
                recorded_at,
            ),
        )

    @staticmethod
    def _cycle_root_event_sql(
        db: sqlite3.Connection, root_input_id: str
    ) -> sqlite3.Row | None:
        root = db.execute(
            "SELECT * FROM source_events WHERE event_key=?", (root_input_id,)
        ).fetchone()
        if root is not None:
            return root
        root = db.execute(
            """SELECT se.* FROM deferred_followups AS d
               JOIN source_events AS se ON se.event_key=d.source_event_key
               WHERE d.deferred_id=?""",
            (root_input_id,),
        ).fetchone()
        if root is not None:
            return root
        return db.execute(
            """SELECT se.* FROM revision_inputs_v1 AS r
               JOIN source_events AS se ON se.event_key=r.source_event_key
               WHERE r.revision_input_id=?""",
            (root_input_id,),
        ).fetchone()

    @classmethod
    def _matches_open_interaction(
        cls, db: sqlite3.Connection, event: sqlite3.Row, thread_id: str
    ) -> bool:
        if not starts_with_agent_invocation(event["body"]):
            return False
        cycle = db.execute(
            """SELECT * FROM workflow_cycles_v1 WHERE thread_id=? AND status='ACTIVE'
               ORDER BY cycle_id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if cycle is None or not cycle["active_task_id"]:
            return False
        task = db.execute(
            """SELECT * FROM workflow_task_runs_v1
               WHERE workflow_cycle_id=? AND task_id=?""",
            (cycle["workflow_cycle_id"], cycle["active_task_id"]),
        ).fetchone()
        if task is None or task["phase"] not in {
            "WAITING_FOR_PLAN_APPROVAL",
            "WAITING_FOR_RESULT_APPROVAL",
            "WAITING_FOR_INPUT",
        }:
            return False
        root = cls._cycle_root_event_sql(db, cycle["root_input_id"])
        if root is None or not cls._same_generic_target(event, root):
            return False
        threshold = task["updated_at"]
        if task["phase"] == "WAITING_FOR_PLAN_APPROVAL":
            occurrence = db.execute(
                "SELECT posted_at FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task["current_plan_id"],),
            ).fetchone()
            threshold = occurrence["posted_at"] if occurrence else threshold
        elif task["phase"] == "WAITING_FOR_RESULT_APPROVAL":
            occurrence = db.execute(
                """SELECT r.posted_at FROM workflow_task_results_v1 AS r
                   JOIN workflow_task_validations_v1 AS v
                     ON v.validation_id=r.validation_id
                   WHERE r.task_run_id=? ORDER BY v.validation_round DESC LIMIT 1""",
                (task["task_run_id"],),
            ).fetchone()
            threshold = occurrence["posted_at"] if occurrence else threshold
        return SQLiteGitHubStore._after_posted_at(event, threshold)

    def observe_issue_content(
        self,
        *,
        repo_id: int,
        source_id: str,
        issue_number: int,
        body: str | None,
        observed_at: str,
    ) -> None:
        """Record a non-actionable issue body as the latest content version."""
        with self.transaction() as db:
            self._upsert_issue_content_observation(
                db,
                repo_id=repo_id,
                source_id=source_id,
                issue_number=issue_number,
                body=body,
                observed_at=observed_at,
            )

    @staticmethod
    def _issue_body_hash(body: str | None) -> str:
        return hashlib.sha256((body or "").encode("utf-8")).hexdigest()

    @classmethod
    def _upsert_issue_content_observation(
        cls,
        db: sqlite3.Connection,
        *,
        repo_id: int,
        source_id: str,
        issue_number: int,
        body: str | None,
        observed_at: str,
    ) -> None:
        normalized = body or ""
        db.execute(
            """INSERT INTO issue_content_observations(
               repo_id, source_id, issue_number, body_hash, body, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, source_id) DO UPDATE SET
                 issue_number=excluded.issue_number,
                 body_hash=excluded.body_hash,
                 body=excluded.body,
                 observed_at=excluded.observed_at""",
            (
                repo_id,
                source_id,
                issue_number,
                cls._issue_body_hash(normalized),
                normalized,
                observed_at,
            ),
        )

    @classmethod
    def _is_new_issue_content(cls, db: sqlite3.Connection, event: SourceEvent) -> bool:
        if event.source_kind.value != "issue":
            return True
        body_hash = cls._issue_body_hash(event.body)
        row = db.execute(
            """SELECT body_hash FROM issue_content_observations
               WHERE repo_id = ? AND source_id = ?""",
            (event.repo_id, event.source_id),
        ).fetchone()
        if row is None:
            # Bootstrap upgrades from databases that predate observations.  A
            # newer generic issue timestamp must not replay the latest known
            # body as a new logical task.
            historical = db.execute(
                """SELECT body FROM source_events
                   WHERE repo_id = ? AND source_kind = 'issue'
                     AND source_id = ?
                   ORDER BY source_updated_at DESC, discovered_at DESC
                   LIMIT 1""",
                (event.repo_id, event.source_id),
            ).fetchone()
            if (
                historical is not None
                and cls._issue_body_hash(historical[0]) == body_hash
            ):
                cls._upsert_issue_content_observation(
                    db,
                    repo_id=event.repo_id,
                    source_id=event.source_id,
                    issue_number=event.subject_number,
                    body=event.body,
                    observed_at=event.source_updated_at,
                )
                return False
        elif row[0] == body_hash:
            cls._upsert_issue_content_observation(
                db,
                repo_id=event.repo_id,
                source_id=event.source_id,
                issue_number=event.subject_number,
                body=event.body,
                observed_at=event.source_updated_at,
            )
            return False
        cls._upsert_issue_content_observation(
            db,
            repo_id=event.repo_id,
            source_id=event.source_id,
            issue_number=event.subject_number,
            body=event.body,
            observed_at=event.source_updated_at,
        )
        return True

    @staticmethod
    def _has_resolved_issue_snapshot(
        db: sqlite3.Connection, event: SourceEvent
    ) -> bool:
        if event.source_kind.value != "issue":
            return False
        return (
            db.execute(
                """SELECT 1
                   FROM source_events earlier
                   JOIN event_executions execution
                     ON execution.event_key = earlier.event_key
                   WHERE earlier.repo_id = ?
                     AND earlier.source_kind = 'issue'
                     AND earlier.source_id = ?
                     AND earlier.body = ?
                     AND (
                         execution.status = ? OR (
                             execution.status = ? AND EXISTS (
                                 SELECT 1 FROM logical_publications publication
                                 WHERE publication.source_event_key
                                       = earlier.event_key
                                   AND publication.root_input_id
                                       = earlier.event_key
                                   AND publication.status IN (?, ?)
                             )
                         )
                     )
                   LIMIT 1""",
                (
                    event.repo_id,
                    event.source_id,
                    event.body,
                    ExecutionStatus.SKIPPED.value,
                    ExecutionStatus.SUCCEEDED.value,
                    PublicationStatus.COMPLETED.value,
                    PublicationStatus.NO_CHANGES.value,
                ),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _repair_redundant_issue_events(db: sqlite3.Connection, *, now: str) -> None:
        db.execute(
            """INSERT INTO event_executions(
                   event_key, thread_id, status, attempt_count, started_at,
                   completed_at, response_text, error_message, workspace_path)
               SELECT later.event_key, later.thread_id, ?, 0, later.discovered_at,
                      ?, NULL, ?, NULL
               FROM source_events later
               JOIN source_events earlier
                 ON earlier.repo_id = later.repo_id
                AND earlier.source_kind = 'issue'
                AND later.source_kind = 'issue'
                AND earlier.source_id = later.source_id
                AND earlier.body = later.body
                AND (
                    earlier.source_updated_at < later.source_updated_at OR
                    (earlier.source_updated_at = later.source_updated_at AND
                     earlier.event_key < later.event_key)
                )
               JOIN event_executions prior_execution
                 ON prior_execution.event_key = earlier.event_key
               LEFT JOIN event_executions later_execution
                 ON later_execution.event_key = later.event_key
               WHERE later_execution.event_key IS NULL
                 AND (
                     prior_execution.status = ? OR (
                         prior_execution.status = ? AND EXISTS (
                             SELECT 1 FROM logical_publications prior_publication
                             WHERE prior_publication.source_event_key
                                   = earlier.event_key
                               AND prior_publication.root_input_id
                                   = earlier.event_key
                               AND prior_publication.status IN (?, ?)
                         )
                     )
                 )""",
            (
                ExecutionStatus.SKIPPED.value,
                now,
                "duplicate unchanged issue snapshot after resolved event",
                ExecutionStatus.SKIPPED.value,
                ExecutionStatus.SUCCEEDED.value,
                PublicationStatus.COMPLETED.value,
                PublicationStatus.NO_CHANGES.value,
            ),
        )

    def claim_next_event(self, *, now: str) -> ClaimedEvent | None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT se.event_key, se.thread_id, se.repo_id,
                          se.repo_full_name, thread.issue_number, se.body,
                          se.origin_surface, se.path, se.line, se.start_line,
                          se.side, se.start_side, se.diff_hunk, se.commit_id,
                          se.original_commit_id, se.in_reply_to_id,
                          se.pull_request_review_id, se.review_thread_root_id,
                          se.review_state,
                          ee.status AS execution_status
                   FROM source_events AS se
                   JOIN issue_threads AS thread
                     ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions AS ee
                     ON ee.event_key = se.event_key
                   WHERE se.thread_id IS NOT NULL
                     AND (ee.event_key IS NULL OR ee.status = ?)
                     AND NOT EXISTS (
                         SELECT 1
                         FROM source_events AS earlier
                         LEFT JOIN event_executions AS prior
                           ON prior.event_key = earlier.event_key
                         WHERE earlier.thread_id = se.thread_id
                           AND (
                               earlier.source_updated_at < se.source_updated_at
                               OR (
                                   earlier.source_updated_at = se.source_updated_at
                                   AND earlier.discovered_at < se.discovered_at
                               )
                               OR (
                                   earlier.source_updated_at = se.source_updated_at
                                   AND earlier.discovered_at = se.discovered_at
                                   AND earlier.event_key < se.event_key
                               )
                           )
                           AND (
                               prior.event_key IS NULL OR NOT (
                                   prior.status = ? OR (
                                       prior.status = ? AND (
                                           SELECT prior_publication.status
                                           FROM logical_publications
                                                AS prior_publication
                                           WHERE prior_publication
                                                 .source_event_key
                                                 = earlier.event_key
                                             AND prior_publication.root_input_id
                                                 = earlier.event_key
                                           LIMIT 1
                                       ) IN (?, ?)
                                   )
                               )
                           )
                     )
                   ORDER BY se.source_updated_at, se.discovered_at, se.event_key
                   LIMIT 1""",
                (
                    ExecutionStatus.RETRY_PENDING.value,
                    ExecutionStatus.SKIPPED.value,
                    ExecutionStatus.SUCCEEDED.value,
                    PublicationStatus.COMPLETED.value,
                    PublicationStatus.NO_CHANGES.value,
                ),
            ).fetchone()
            if row is None:
                return None
            retrying = row["execution_status"] == ExecutionStatus.RETRY_PENDING.value
            if retrying:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, row["event_key"]),
                )
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (
                        row["event_key"],
                        row["thread_id"],
                        ExecutionStatus.RUNNING.value,
                        now,
                    ),
                )
            return ClaimedEvent(
                event_key=row["event_key"],
                thread_id=row["thread_id"],
                repo_id=row["repo_id"],
                repo_full_name=row["repo_full_name"],
                issue_number=row["issue_number"],
                body=row["body"],
                workspace_path=None,
                retrying=retrying,
                origin_surface=row["origin_surface"],
                path=row["path"],
                line=row["line"],
                start_line=row["start_line"],
                side=row["side"],
                start_side=row["start_side"],
                diff_hunk=row["diff_hunk"],
                commit_id=row["commit_id"],
                original_commit_id=row["original_commit_id"],
                in_reply_to_id=row["in_reply_to_id"],
                pull_request_review_id=row["pull_request_review_id"],
                review_thread_root_id=row["review_thread_root_id"],
                review_state=row["review_state"],
            )

    def release_execution_claim(self, event_key: str, *, retrying: bool) -> None:
        with self.transaction() as db:
            if retrying:
                db.execute(
                    "UPDATE event_executions SET status = ? WHERE event_key = ? "
                    "AND status = ?",
                    (
                        ExecutionStatus.RETRY_PENDING.value,
                        event_key,
                        ExecutionStatus.RUNNING.value,
                    ),
                )
            else:
                db.execute(
                    "DELETE FROM event_executions WHERE event_key = ? AND status = ?",
                    (event_key, ExecutionStatus.RUNNING.value),
                )

    def running_executions_before(self, started_before: str) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """SELECT event_key, thread_id, status, attempt_count, started_at,
                      completed_at, error_message, NULL AS execution_id,
                      NULL AS cycle_id, NULL AS root_input_id
               FROM event_executions WHERE status = ? AND started_at < ?
               UNION ALL
               SELECT source_event_key AS event_key, thread_id, status, attempt_count,
                      started_at, completed_at, error_message, execution_id, cycle_id,
                      root_input_id
               FROM logical_executions WHERE status = ? AND started_at < ?
               ORDER BY started_at, event_key""",
            (
                ExecutionStatus.RUNNING.value,
                started_before,
                ExecutionStatus.RUNNING.value,
                started_before,
            ),
        ).fetchall()
        return [self._execution_record(row) for row in rows]

    def mark_execution_interrupted(
        self,
        event_key: str,
        *,
        completed_at: str,
        error_message: str,
        execution_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            target = execution_id or event_key
            table = (
                "logical_executions"
                if target.startswith("execution-")
                else "event_executions"
            )
            key = "execution_id" if table == "logical_executions" else "event_key"
            cursor = db.execute(
                f"""UPDATE {table} SET status = ?, completed_at = ?,
                   error_message = ? WHERE {key} = ? AND status = ?""",
                (
                    ExecutionStatus.INTERRUPTED.value,
                    completed_at,
                    error_message,
                    target,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("event execution is no longer running")

    def retry_execution(self, event_key: str) -> ExecutionStatus:
        with self.transaction() as db:
            logical = event_key.startswith("execution-")
            table = "logical_executions" if logical else "event_executions"
            key = "execution_id" if logical else "event_key"
            row = db.execute(
                f"SELECT status FROM {table} WHERE {key} = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown event key")
            status = ExecutionStatus(row["status"])
            if status == ExecutionStatus.RETRY_PENDING:
                return status
            if status not in (ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED):
                raise ValueError(f"cannot retry execution in {status.value} status")
            db.execute(
                f"UPDATE {table} SET status = ?, completed_at = NULL WHERE {key} = ?",
                (ExecutionStatus.RETRY_PENDING.value, event_key),
            )
            if logical:
                db.execute(
                    "UPDATE execution_permits SET consumed_at = NULL "
                    "WHERE root_event_key = (SELECT source_event_key FROM logical_executions WHERE execution_id=?) "
                    "AND cycle_id=(SELECT cycle_id FROM logical_executions WHERE execution_id=?) "
                    "AND consumed_at IS NOT NULL AND invalidated_at IS NULL",
                    (event_key, event_key),
                )
            else:
                db.execute(
                    "UPDATE execution_permits SET consumed_at = NULL "
                    "WHERE root_event_key = ? AND consumed_at IS NOT NULL "
                    "AND invalidated_at IS NULL",
                    (event_key,),
                )
            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            if logical:
                cursor = db.execute(
                    """UPDATE issue_workflow_state SET phase=?, updated_at=?
                       WHERE thread_id=(SELECT thread_id FROM logical_executions WHERE execution_id=?)
                         AND cycle_id=(SELECT cycle_id FROM logical_executions WHERE execution_id=?)
                         AND root_input_id=(SELECT root_input_id FROM logical_executions WHERE execution_id=?)
                         AND phase=?""",
                    (
                        WorkflowPhase.EXECUTION_READY.value,
                        timestamp,
                        event_key,
                        event_key,
                        event_key,
                        WorkflowPhase.EXECUTING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "logical execution is not the current retry target"
                    )
            else:
                db.execute(
                    """UPDATE issue_workflow_state SET phase = ?, updated_at = ?
                       WHERE root_event_key = ? AND phase = ?""",
                    (
                        WorkflowPhase.EXECUTION_READY.value,
                        timestamp,
                        event_key,
                        WorkflowPhase.EXECUTING.value,
                    ),
                )
            return ExecutionStatus.RETRY_PENDING

    def skip_execution(self, event_key: str, *, completed_at: str, reason: str) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM event_executions WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown event key")
            status = ExecutionStatus(row["status"])
            if status == ExecutionStatus.SKIPPED:
                return
            if status not in (ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED):
                raise ValueError(f"cannot skip execution in {status.value} status")
            db.execute(
                "UPDATE event_executions SET status = ?, completed_at = ?, "
                "error_message = ? WHERE event_key = ?",
                (ExecutionStatus.SKIPPED.value, completed_at, reason, event_key),
            )

    def execution_records(self) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """SELECT event_key, thread_id, status, attempt_count, started_at,
                      completed_at, error_message, NULL AS execution_id,
                      NULL AS cycle_id, NULL AS root_input_id FROM event_executions
               UNION ALL
               SELECT source_event_key AS event_key, thread_id, status, attempt_count,
                      started_at, completed_at, error_message, execution_id,
                      cycle_id, root_input_id FROM logical_executions
               ORDER BY started_at, event_key"""
        ).fetchall()
        return [self._execution_record(row) for row in rows]

    @staticmethod
    def _execution_record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            event_key=row["event_key"],
            thread_id=row["thread_id"],
            status=ExecutionStatus(row["status"]),
            attempt_count=row["attempt_count"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error_message=row["error_message"],
            execution_id=row["execution_id"] if "execution_id" in row.keys() else None,
            cycle_id=row["cycle_id"] if "cycle_id" in row.keys() else None,
            root_input_id=row["root_input_id"]
            if "root_input_id" in row.keys()
            else None,
        )

    def mark_execution_succeeded(
        self,
        event_key: str,
        *,
        execution_id: str | None = None,
        completed_at: str,
        response_text: str,
        workspace_path: str,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> None:
        with self.transaction() as db:
            self._update_execution(
                db,
                execution_id or event_key,
                ExecutionStatus.SUCCEEDED,
                completed_at=completed_at,
                response_text=response_text,
                error_message=None,
                workspace_path=workspace_path,
                start_head_sha=start_head_sha,
                end_head_sha=end_head_sha,
                end_dirty=end_dirty,
            )

    def mark_execution_failed(
        self,
        event_key: str,
        *,
        execution_id: str | None = None,
        completed_at: str,
        error_message: str,
        workspace_path: str | None,
    ) -> None:
        with self.transaction() as db:
            self._update_execution(
                db,
                execution_id or event_key,
                ExecutionStatus.FAILED,
                completed_at=completed_at,
                response_text=None,
                error_message=error_message,
                workspace_path=workspace_path,
            )

    def interrupt_execution_for_clarification(
        self, *, permit_id: str, event_key: str, now: str
    ) -> None:
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                """SELECT p.thread_id, p.cycle_id, p.root_event_key
                   FROM execution_permits p WHERE p.permit_id=?""",
                (permit_id,),
            ).fetchone()
            target = (
                self._execution_target(
                    db,
                    thread_id=permit["thread_id"],
                    cycle_id=permit["cycle_id"],
                    root_event_key=permit["root_event_key"],
                )
                if permit
                else event_key
            )
            self._update_execution(
                db,
                target,
                ExecutionStatus.INTERRUPTED,
                completed_at=now,
                response_text=None,
                error_message="execution is waiting for clarification",
                workspace_path=None,
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?, updated_at=? "
                "WHERE current_plan_id=(SELECT plan_id FROM execution_permits WHERE permit_id=?)",
                (WorkflowPhase.WAITING_FOR_INPUT.value, now, permit_id),
            )

    def resume_clarification(
        self,
        clarification_id: str,
        *,
        answer_event_key: str,
        answer_json: str,
        now: str,
    ) -> ClarificationRequestRecord:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM clarification_requests WHERE clarification_id=?",
                (clarification_id,),
            ).fetchone()
            if row is None or row["status"] != ClarificationStatus.OPEN.value:
                raise ValueError("clarification is not open")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                (
                    answer_event_key,
                    row["thread_id"],
                    row["cycle_id"],
                    InputPurpose.CLARIFICATION_RESPONSE.value,
                    "CONSUMED",
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE clarification_requests SET status=?, answered_at=?, "
                "answer_event_key=?, answer_json=? WHERE clarification_id=?",
                (
                    ClarificationStatus.ANSWERED.value,
                    now,
                    answer_event_key,
                    answer_json,
                    clarification_id,
                ),
            )
            permit = db.execute(
                "SELECT permit_id, root_event_key FROM execution_permits "
                "WHERE thread_id=? AND cycle_id=? AND invalidated_at IS NULL",
                (row["thread_id"], row["cycle_id"]),
            ).fetchone()
            if permit:
                target = self._execution_target(
                    db,
                    thread_id=row["thread_id"],
                    cycle_id=row["cycle_id"],
                    root_event_key=permit["root_event_key"],
                )
                db.execute(
                    "UPDATE execution_permits SET consumed_at=NULL WHERE permit_id=?",
                    (permit["permit_id"],),
                )
                table = (
                    "logical_executions"
                    if target.startswith("execution-")
                    else "event_executions"
                )
                key = "execution_id" if table == "logical_executions" else "event_key"
                db.execute(
                    f"UPDATE {table} SET status=?, completed_at=NULL, "
                    f"error_message=NULL WHERE {key}=?",
                    (ExecutionStatus.RETRY_PENDING.value, target),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?, updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.EXECUTION_READY.value, now, row["thread_id"]),
                )
        return self.clarification(clarification_id)  # type: ignore[return-value]

    def resolve_clarification_answer(
        self,
        clarification_id: str,
        *,
        answer_event_key: str,
        answer_json: str,
        residual_text: str | None,
        now: str,
    ) -> ClarificationRequestRecord:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM clarification_requests WHERE clarification_id=?",
                (clarification_id,),
            ).fetchone()
            if row is None or row["status"] != ClarificationStatus.OPEN.value:
                raise ValueError("clarification is not open")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                (
                    answer_event_key,
                    row["thread_id"],
                    row["cycle_id"],
                    InputPurpose.CLARIFICATION_RESPONSE.value,
                    "CONSUMED",
                    now,
                    now,
                ),
            )
            if residual_text:
                deferred_id = (
                    "deferred-"
                    + hashlib.sha256(
                        f"{answer_event_key}\0{residual_text}".encode()
                    ).hexdigest()[:24]
                )
                db.execute(
                    """INSERT INTO deferred_followups(
                       deferred_id, source_event_key, thread_id, originating_cycle_id,
                       status, residual_text, queued_at)
                       VALUES(?,?,?,?,'QUEUED',?,?)
                       ON CONFLICT(deferred_id) DO NOTHING""",
                    (
                        deferred_id,
                        answer_event_key,
                        row["thread_id"],
                        row["cycle_id"],
                        residual_text,
                        now,
                    ),
                )
            db.execute(
                "UPDATE clarification_requests SET status=?, answered_at=?, "
                "answer_event_key=?, answer_json=? WHERE clarification_id=?",
                (
                    ClarificationStatus.ANSWERED.value,
                    now,
                    answer_event_key,
                    answer_json,
                    clarification_id,
                ),
            )
            permit = db.execute(
                "SELECT permit_id, root_event_key FROM execution_permits "
                "WHERE thread_id=? AND cycle_id=? AND invalidated_at IS NULL",
                (row["thread_id"], row["cycle_id"]),
            ).fetchone()
            if permit:
                target = self._execution_target(
                    db,
                    thread_id=row["thread_id"],
                    cycle_id=row["cycle_id"],
                    root_event_key=permit["root_event_key"],
                )
                db.execute(
                    "UPDATE execution_permits SET consumed_at=NULL WHERE permit_id=?",
                    (permit["permit_id"],),
                )
                table = (
                    "logical_executions"
                    if target.startswith("execution-")
                    else "event_executions"
                )
                key = "execution_id" if table == "logical_executions" else "event_key"
                db.execute(
                    f"UPDATE {table} SET status=?, completed_at=NULL, "
                    f"error_message=NULL WHERE {key}=?",
                    (ExecutionStatus.RETRY_PENDING.value, target),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?, updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.EXECUTION_READY.value, now, row["thread_id"]),
                )
        return self.clarification(clarification_id)  # type: ignore[return-value]

    def cancel_clarification_for_replan(
        self, clarification_id: str, *, event_key: str, now: str
    ) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM clarification_requests WHERE clarification_id=?",
                (clarification_id,),
            ).fetchone()
            if row is None or row["status"] != ClarificationStatus.OPEN.value:
                raise ValueError("clarification is not open")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                (
                    event_key,
                    row["thread_id"],
                    row["cycle_id"],
                    InputPurpose.PLANNING_INPUT.value,
                    "CONSUMED",
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE clarification_requests SET status=?, answered_at=?, "
                "answer_event_key=? WHERE clarification_id=?",
                (ClarificationStatus.CANCELLED.value, now, event_key, clarification_id),
            )
            db.execute(
                "UPDATE execution_permits SET invalidated_at=? WHERE thread_id=? "
                "AND cycle_id=? AND invalidated_at IS NULL",
                (now, row["thread_id"], row["cycle_id"]),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?, planning_feedback_event_key=?, "
                "updated_at=? WHERE thread_id=?",
                (WorkflowPhase.PLANNING.value, event_key, now, row["thread_id"]),
            )

    @staticmethod
    def _update_execution(
        db: sqlite3.Connection,
        event_key: str,
        status: ExecutionStatus,
        *,
        completed_at: str,
        response_text: str | None,
        error_message: str | None,
        workspace_path: str | None,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> None:
        if event_key.startswith("execution-"):
            cursor = db.execute(
                """UPDATE logical_executions SET status=?, completed_at=?,
                   response_text=?, error_message=?, workspace_path=?,
                   start_head_sha=?, end_head_sha=?, end_dirty=?
                   WHERE execution_id=? AND status=?""",
                (
                    status.value,
                    completed_at,
                    response_text,
                    error_message,
                    workspace_path,
                    start_head_sha,
                    end_head_sha,
                    int(end_dirty),
                    event_key,
                    ExecutionStatus.RUNNING.value,
                ),
            )
        else:
            cursor = db.execute(
                """UPDATE event_executions SET status = ?, completed_at = ?,
               response_text = ?, error_message = ?, workspace_path = ?,
               start_head_sha = ?, end_head_sha = ?, end_dirty = ?
               WHERE event_key = ? AND status = ?""",
                (
                    status.value,
                    completed_at,
                    response_text,
                    error_message,
                    workspace_path,
                    start_head_sha,
                    end_head_sha,
                    int(end_dirty),
                    event_key,
                    ExecutionStatus.RUNNING.value,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("event execution is not currently running")

    def execution_for_event(self, event_key: str):
        return self.connection.execute(
            "SELECT * FROM event_executions WHERE event_key = ?", (event_key,)
        ).fetchone()

    def execution_for_id(self, execution_id: str):
        if execution_id.startswith("execution-"):
            return self.connection.execute(
                "SELECT * FROM logical_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        return self.execution_for_event(execution_id)

    def execution_for_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str | None,
    ):
        effective = root_input_id or root_event_key
        if effective != root_event_key:
            return self.execution_for_id(
                execution_id_for(
                    thread_id=thread_id, cycle_id=cycle_id, root_input_id=effective
                )
            )
        return self.execution_for_event(root_event_key)

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> ExecutionAttemptRecord:
        return ExecutionAttemptRecord(
            attempt_id=row["attempt_id"],
            thread_id=row["thread_id"],
            cycle_id=row["cycle_id"],
            plan_id=row["plan_id"],
            plan_version=row["plan_version"],
            root_event_key=row["root_event_key"],
            attempt_number=row["attempt_number"],
            kind=AttemptKind(row["kind"]),
            repair_round=row["repair_round"],
            parent_review_id=row["parent_review_id"],
            authorization_id=row["authorization_id"],
            status=AttemptStatus(row["status"]),
            response_text=row["response_text"],
            start_head_sha=row["start_head_sha"],
            end_head_sha=row["end_head_sha"],
            start_dirty=bool(row["start_dirty"]),
            end_dirty=bool(row["end_dirty"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            retry_count=row["retry_count"],
            repair_recovery_count=row["repair_recovery_count"],
            review_recovery_count=row["review_recovery_count"],
        )

    def execution_attempt(self, attempt_id: str) -> ExecutionAttemptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._attempt_record(row) if row else None

    @staticmethod
    def _tool_evidence_record(row: sqlite3.Row) -> ExecutionToolEvidenceRecord:
        return ExecutionToolEvidenceRecord(
            evidence_id=row["evidence_id"],
            attempt_id=row["attempt_id"],
            thread_id=row["thread_id"],
            cycle_id=row["cycle_id"],
            sequence_number=row["sequence_number"],
            kind=row["kind"],
            command=row["command"],
            exit_code=row["exit_code"],
            output=row["output"],
            output_hash=row["output_hash"],
            truncated=bool(row["truncated"]),
            recorded_at=row["recorded_at"],
        )

    def record_execution_tool_evidence(
        self,
        *,
        attempt_id: str,
        thread_id: str,
        cycle_id: int,
        kind: str,
        command: str,
        exit_code: int | None,
        output: str,
        output_hash: str,
        truncated: bool,
        recorded_at: str,
    ) -> ExecutionToolEvidenceRecord:
        with self.transaction(immediate=True) as db:
            used = db.execute(
                "SELECT COALESCE(SUM(length(output)), 0) FROM execution_tool_evidence WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            remaining = max(0, MAX_EXECUTION_EVIDENCE_PER_ATTEMPT - int(used))
            if len(output) > remaining:
                output = output[:remaining]
                truncated = True
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM execution_tool_evidence WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            evidence_id = (
                "exec-evidence-"
                + hashlib.sha256(
                    f"{attempt_id}:{sequence}:{output_hash}".encode()
                ).hexdigest()[:32]
            )
            db.execute(
                """INSERT INTO execution_tool_evidence
                (evidence_id,attempt_id,thread_id,cycle_id,sequence_number,kind,
                 command,exit_code,output,output_hash,truncated,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    attempt_id,
                    thread_id,
                    cycle_id,
                    sequence,
                    kind,
                    command,
                    exit_code,
                    output,
                    output_hash,
                    int(truncated),
                    recorded_at,
                ),
            )
            row = db.execute(
                "SELECT * FROM execution_tool_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        return self._tool_evidence_record(row)  # type: ignore[arg-type]

    def execution_tool_evidence_for_attempt(
        self, attempt_id: str
    ) -> tuple[ExecutionToolEvidenceRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM execution_tool_evidence WHERE attempt_id=? ORDER BY sequence_number",
            (attempt_id,),
        ).fetchall()
        return tuple(self._tool_evidence_record(row) for row in rows)

    def execution_tool_evidence_for_cycle(
        self, thread_id: str, cycle_id: int
    ) -> tuple[ExecutionToolEvidenceRecord, ...]:
        rows = self.connection.execute(
            """SELECT e.* FROM execution_tool_evidence e
               JOIN execution_attempts a ON a.attempt_id=e.attempt_id
               WHERE e.thread_id=? AND e.cycle_id=?
               ORDER BY a.attempt_number, e.sequence_number""",
            (thread_id, cycle_id),
        ).fetchall()
        return tuple(self._tool_evidence_record(row) for row in rows)

    def latest_attempt(
        self, thread_id: str, cycle_id: int
    ) -> ExecutionAttemptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_attempts WHERE thread_id = ? AND cycle_id = "
            "? ORDER BY attempt_number DESC LIMIT 1",
            (thread_id, cycle_id),
        ).fetchone()
        return self._attempt_record(row) if row else None

    def ensure_execution_attempt(
        self,
        *,
        attempt_id: str,
        thread_id: str,
        cycle_id: int,
        plan_id: str,
        plan_version: int,
        root_event_key: str,
        authorization_id: str,
        created_at: str,
        kind: AttemptKind = AttemptKind.INITIAL,
        parent_review_id: str | None = None,
        repair_round: int = 0,
        attempt_number: int | None = None,
    ) -> ExecutionAttemptRecord:
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing:
                if existing["status"] != AttemptStatus.SUCCEEDED.value:
                    db.execute(
                        "UPDATE execution_attempts SET status=?,retry_count=retry_count+1,completed_at=NULL WHERE attempt_id=?",
                        (AttemptStatus.RUNNING.value, attempt_id),
                    )
                return self._attempt_record(existing)
            number = (
                attempt_number
                or (
                    db.execute(
                        "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM execution_attempts "
                        "WHERE thread_id = ? AND cycle_id = ?",
                        (thread_id, cycle_id),
                    ).fetchone()[0]
                )
            )
            db.execute(
                "INSERT INTO execution_attempts(attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,attempt_number,kind,repair_round,parent_review_id,authorization_id,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    thread_id,
                    cycle_id,
                    plan_id,
                    plan_version,
                    root_event_key,
                    number,
                    kind.value,
                    repair_round,
                    parent_review_id,
                    authorization_id,
                    AttemptStatus.RUNNING.value,
                    created_at,
                ),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def finish_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: AttemptStatus,
        completed_at: str,
        response_text: str | None = None,
        start_head_sha: str | None = None,
        end_head_sha: str | None = None,
        end_dirty: bool = False,
    ) -> ExecutionAttemptRecord:
        with self.transaction() as db:
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=?,response_text=?,start_head_sha=COALESCE(?,start_head_sha),end_head_sha=?,end_dirty=? WHERE attempt_id=?",
                (
                    status.value,
                    completed_at,
                    response_text,
                    start_head_sha,
                    end_head_sha,
                    int(end_dirty),
                    attempt_id,
                ),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def execution_review_for_attempt(
        self, attempt_id: str
    ) -> ExecutionReviewRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_reviews WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not row:
            return None
        return ExecutionReviewRecord(**dict(row))

    def execution_review(self, review_id: str) -> ExecutionReviewRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return ExecutionReviewRecord(**dict(row)) if row else None

    def save_execution_review(
        self, review: ExecutionReviewRecord
    ) -> ExecutionReviewRecord:
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO execution_reviews(
                   review_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
                   attempt_id,review_iteration,verdict,summary,findings_json,
                   repair_instructions_json,created_at,completed_at,
                   requirement_checks_json,inspection_json,challenge_json,
                   read_ledger_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review.review_id,
                    review.thread_id,
                    review.cycle_id,
                    review.plan_id,
                    review.plan_version,
                    review.root_event_key,
                    review.attempt_id,
                    review.review_iteration,
                    review.verdict,
                    review.summary,
                    review.findings_json,
                    review.repair_instructions_json,
                    review.created_at,
                    review.completed_at,
                    review.requirement_checks_json,
                    review.inspection_json,
                    review.challenge_json,
                    review.read_ledger_json,
                ),
            )
        return self.execution_review_for_attempt(review.attempt_id)  # type: ignore[return-value]

    def accept_execution_review(self, review_id: str, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            state = (
                db.execute(
                    "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                    (row["thread_id"],),
                ).fetchone()
                if row
                else None
            )
            attempt = (
                db.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()
                if row
                else None
            )
            plan = (
                db.execute(
                    "SELECT * FROM issue_plans WHERE plan_id = ?", (row["plan_id"],)
                ).fetchone()
                if row
                else None
            )
            latest = (
                db.execute(
                    "SELECT attempt_id FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (state["thread_id"], state["cycle_id"]),
                ).fetchone()
                if state
                else None
            )
            if (
                not row
                or not state
                or not attempt
                or row["verdict"] != "ACCEPT"
                or state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or row["thread_id"] != state["thread_id"]
                or row["cycle_id"] != state["cycle_id"]
                or row["root_event_key"] != state["root_event_key"]
                or not plan
                or state["current_plan_id"] != plan["plan_id"]
                or plan["version"] != row["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or not latest
                or latest["attempt_id"] != attempt["attempt_id"]
                or attempt["thread_id"] != state["thread_id"]
                or attempt["cycle_id"] != state["cycle_id"]
                or attempt["root_event_key"] != state["root_event_key"]
                or attempt["plan_id"] != plan["plan_id"]
                or attempt["plan_version"] != plan["version"]
            ):
                raise ValueError("execution review is stale or not acceptable")
            if (
                state["current_plan_id"] != row["plan_id"]
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
            ):
                raise ValueError(
                    "execution review does not match current successful attempt"
                )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.AWAITING_PUBLICATION.value, now, row["thread_id"]),
            )

    def block_execution_review(self, review_id: str, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?",
                (row["thread_id"],) if row else (None,),
            ).fetchone()
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?",
                (row["attempt_id"],) if row else (None,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=?",
                (row["plan_id"],) if row else (None,),
            ).fetchone()
            latest = db.execute(
                "SELECT * FROM execution_attempts WHERE thread_id=? AND cycle_id=? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (state["thread_id"], state["cycle_id"]) if state else (None, None),
            ).fetchone()
            if (
                not row
                or not state
                or not attempt
                or not plan
                or not latest
                or row["verdict"] != "BLOCKED"
                or state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or row["thread_id"] != state["thread_id"]
                or row["cycle_id"] != state["cycle_id"]
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
                or state["root_event_key"] != row["root_event_key"]
                or state["current_plan_id"] != row["plan_id"]
                or row["plan_version"] != plan["version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or attempt["thread_id"] != state["thread_id"]
                or attempt["cycle_id"] != state["cycle_id"]
                or attempt["root_event_key"] != state["root_event_key"]
                or attempt["plan_id"] != plan["plan_id"]
                or attempt["plan_version"] != plan["version"]
                or latest["attempt_id"] != attempt["attempt_id"]
                or row["attempt_id"] != latest["attempt_id"]
            ):
                raise ValueError("execution review is stale or not blockable")
            db.execute(
                "UPDATE review_repair_permits SET invalidated_at=? "
                "WHERE thread_id=? AND cycle_id=? AND plan_id=? "
                "AND consumed_at IS NULL AND invalidated_at IS NULL",
                (now, row["thread_id"], state["cycle_id"], plan["plan_id"]),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.REVIEW_BLOCKED.value, now, row["thread_id"]),
            )

    def create_repair_permit(
        self, *, thread_id: str, now: str, max_repairs: int = 5
    ) -> ReviewRepairPermit:
        exhausted = False
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?", (thread_id,)
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=(SELECT current_plan_id FROM issue_workflow_state WHERE thread_id=?)",
                (thread_id,),
            ).fetchone()
            attempt = (
                db.execute(
                    "SELECT * FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (thread_id, state["cycle_id"] if state else -1),
                ).fetchone()
                if state
                else None
            )
            review = db.execute(
                "SELECT * FROM execution_reviews WHERE attempt_id=?",
                (attempt["attempt_id"],) if attempt else (None,),
            ).fetchone()
            if not state or not plan or not attempt or not review:
                raise ValueError("repair requires a current reviewed execution")
            latest = db.execute(
                "SELECT attempt_id FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                (thread_id, state["cycle_id"]),
            ).fetchone()
            if (
                state["phase"] != WorkflowPhase.REVIEW_EXECUTION.value
                or attempt["status"] != AttemptStatus.SUCCEEDED.value
                or review["verdict"] != "NEEDS_FIXES"
                or review["thread_id"] != thread_id
                or review["cycle_id"] != state["cycle_id"]
                or review["root_event_key"] != state["root_event_key"]
                or review["plan_id"] != plan["plan_id"]
                or review["plan_version"] != plan["version"]
                or not latest
                or latest["attempt_id"] != attempt["attempt_id"]
                or plan["status"]
                not in (
                    PlanStatus.APPROVED.value,
                    PlanStatus.AUTO_APPROVED.value,
                )
            ):
                raise ValueError("repair review is stale")
            round_number = attempt["repair_round"] + 1
            if round_number > max_repairs:
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REVIEW_BLOCKED.value, now, thread_id),
                )
                db.execute(
                    "UPDATE review_repair_permits SET invalidated_at=? WHERE thread_id=? AND consumed_at IS NULL",
                    (now, thread_id),
                )
                exhausted = True
            if not exhausted:
                permit_id = f"repair-{review['review_id']}-{round_number}"
                db.execute(
                    """INSERT OR IGNORE INTO review_repair_permits(
                   permit_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
                   parent_review_id,repair_round,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        permit_id,
                        thread_id,
                        state["cycle_id"],
                        plan["plan_id"],
                        plan["version"],
                        state["root_event_key"],
                        review["review_id"],
                        round_number,
                        now,
                    ),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REPAIR_READY.value, now, thread_id),
                )
        if exhausted:
            raise ValueError("maximum review repair rounds reached")
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row))  # type: ignore[arg-type]

    def consume_repair_permit(self, permit_id: str, *, now: str) -> ReviewRepairPermit:
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    "UPDATE review_repair_permits SET consumed_at=? WHERE permit_id=? "
                    "AND consumed_at IS NULL AND invalidated_at IS NULL",
                    (now, permit_id),
                ).rowcount
                != 1
            ):
                raise ValueError("repair permit is unavailable")
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row))

    def repair_permit(self, permit_id: str) -> ReviewRepairPermit | None:
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        return ReviewRepairPermit(**dict(row)) if row else None

    def repair_permit_for_thread(self, thread_id: str) -> ReviewRepairPermit | None:
        row = self.connection.execute(
            "SELECT * FROM review_repair_permits WHERE thread_id=? AND consumed_at IS NULL "
            "AND invalidated_at IS NULL ORDER BY repair_round DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return ReviewRepairPermit(**dict(row)) if row else None

    def invalidate_repair_permit(self, permit_id: str, *, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE review_repair_permits SET invalidated_at=? WHERE permit_id=? AND consumed_at IS NULL",
                (now, permit_id),
            )

    def begin_or_resume_repair_attempt(
        self, permit_id: str, *, now: str
    ) -> ExecutionAttemptRecord:
        permit = self.repair_permit(permit_id)
        if permit is None:
            raise ValueError("repair permit is unavailable")
        return self.bind_repair_execution(
            permit_id, expected_thread_id=permit.thread_id, now=now
        )

    def bind_repair_execution(
        self, permit_id: str, *, expected_thread_id: str, now: str
    ) -> ExecutionAttemptRecord:
        pending_event: str | None = None
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
            if not permit or permit["root_event_key"] is None:
                raise ValueError("repair permit has no reconstructable root event")
            if permit["thread_id"] != expected_thread_id:
                raise ValueError("repair permit belongs to another thread")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?",
                (expected_thread_id,),
            ).fetchone()
            if not permit or permit["invalidated_at"] or permit["consumed_at"]:
                raise ValueError("repair permit is unavailable")
            if not state or state["phase"] != WorkflowPhase.REPAIR_READY.value:
                raise ValueError("workflow is not repair-ready")
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=?", (permit["plan_id"],)
            ).fetchone()
            parent = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id=?",
                (permit["parent_review_id"],),
            ).fetchone()
            latest = db.execute(
                "SELECT * FROM execution_attempts WHERE thread_id=? AND cycle_id=? ORDER BY attempt_number DESC LIMIT 1",
                (permit["thread_id"], permit["cycle_id"]),
            ).fetchone()
            parent_attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?",
                (parent["attempt_id"],) if parent else (None,),
            ).fetchone()
            attempt_id = f"attempt-{permit_id}"
            existing = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if (
                state["cycle_id"] != permit["cycle_id"]
                or state["root_event_key"] != permit["root_event_key"]
                or state["current_plan_id"] != permit["plan_id"]
                or not plan
                or plan["version"] != permit["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or not parent
                or parent["verdict"] != "NEEDS_FIXES"
                or parent["thread_id"] != expected_thread_id
                or parent["cycle_id"] != permit["cycle_id"]
                or parent["root_event_key"] != permit["root_event_key"]
                or parent["plan_id"] != permit["plan_id"]
                or parent["plan_version"] != permit["plan_version"]
                or not parent_attempt
                or parent_attempt["status"] != AttemptStatus.SUCCEEDED.value
                or parent_attempt["thread_id"] != expected_thread_id
                or parent_attempt["cycle_id"] != permit["cycle_id"]
                or parent_attempt["root_event_key"] != permit["root_event_key"]
                or parent_attempt["plan_id"] != permit["plan_id"]
                or parent_attempt["plan_version"] != permit["plan_version"]
                or (parent_attempt["repair_round"] or 0) + 1 != permit["repair_round"]
                or (
                    existing is not None
                    and (
                        existing["kind"] != AttemptKind.REVIEW_REPAIR.value
                        or existing["status"]
                        not in (
                            AttemptStatus.FAILED.value,
                            AttemptStatus.RUNNING.value,
                        )
                        or existing["thread_id"] != expected_thread_id
                        or existing["cycle_id"] != permit["cycle_id"]
                        or existing["root_event_key"] != permit["root_event_key"]
                        or existing["plan_id"] != permit["plan_id"]
                        or existing["plan_version"] != permit["plan_version"]
                        or existing["parent_review_id"] != permit["parent_review_id"]
                        or existing["repair_round"] != permit["repair_round"]
                        or existing["attempt_number"]
                        != parent_attempt["attempt_number"] + 1
                    )
                )
                or (
                    existing is None
                    and (
                        not latest
                        or latest["attempt_id"] != parent_attempt["attempt_id"]
                        or latest["status"] != AttemptStatus.SUCCEEDED.value
                    )
                )
            ):
                raise ValueError("repair permit binding is stale")
            pending = db.execute(
                """SELECT event_key, body FROM source_events
                   WHERE thread_id=? AND event_key != ? AND event_key NOT IN
                   (SELECT event_key FROM thread_input_consumptions)
                   ORDER BY source_updated_at, discovered_at, event_key""",
                (expected_thread_id, permit["root_event_key"]),
            ).fetchall()
            for candidate in pending:
                if is_exact_agent_approval(candidate["body"]):
                    db.execute(
                        """INSERT OR IGNORE INTO thread_input_consumptions(
                           event_key,thread_id,cycle_id,purpose,status,claimed_at,consumed_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            candidate["event_key"],
                            expected_thread_id,
                            permit["cycle_id"],
                            InputPurpose.STALE_PLAN_APPROVAL.value,
                            "CONSUMED",
                            now,
                            now,
                        ),
                    )
                    self._resolve_workflow_control(
                        db,
                        event_key=candidate["event_key"],
                        thread_id=expected_thread_id,
                        claimed_at=now,
                        purpose=InputPurpose.STALE_PLAN_APPROVAL,
                    )
                elif starts_with_agent_invocation(candidate["body"]):
                    db.execute(
                        "UPDATE review_repair_permits SET invalidated_at=? WHERE permit_id=?",
                        (now, permit_id),
                    )
                    db.execute(
                        "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                        (WorkflowPhase.REVIEW_BLOCKED.value, now, expected_thread_id),
                    )
                    pending_event = candidate["event_key"]
                    break
            if pending_event:
                pass
            else:
                if existing:
                    db.execute(
                        "UPDATE execution_attempts SET status=?,retry_count=retry_count+1,completed_at=NULL WHERE attempt_id=?",
                        (AttemptStatus.RUNNING.value, attempt_id),
                    )
                else:
                    db.execute(
                        """INSERT INTO execution_attempts(
                           attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
                           attempt_number,kind,repair_round,parent_review_id,authorization_id,
                           status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            attempt_id,
                            expected_thread_id,
                            permit["cycle_id"],
                            permit["plan_id"],
                            permit["plan_version"],
                            permit["root_event_key"],
                            latest["attempt_number"] + 1,
                            AttemptKind.REVIEW_REPAIR.value,
                            permit["repair_round"],
                            permit["parent_review_id"],
                            permit_id,
                            AttemptStatus.RUNNING.value,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.EXECUTING.value, now, expected_thread_id),
                )
                bound_attempt_id = attempt_id
        if pending_event:
            raise PendingRepairInputError(pending_event)
        return self.execution_attempt(bound_attempt_id)  # type: ignore[return-value]

    def finish_repair_attempt_success(
        self,
        permit_id: str,
        *,
        attempt_id: str,
        now: str,
        response_text: str,
        end_head_sha: str,
        end_dirty: bool,
        workspace_path: str,
        start_head_sha: str,
    ) -> ExecutionAttemptRecord:
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM review_repair_permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if not permit or not attempt or attempt["authorization_id"] != permit_id:
                raise ValueError("repair attempt binding is invalid")
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=?,response_text=?,end_head_sha=?,end_dirty=? WHERE attempt_id=?",
                (
                    AttemptStatus.SUCCEEDED.value,
                    now,
                    response_text,
                    end_head_sha,
                    int(end_dirty),
                    attempt_id,
                ),
            )
            db.execute(
                "UPDATE event_executions SET response_text=?,workspace_path=?,end_head_sha=?,end_dirty=? WHERE event_key=?",
                (
                    response_text,
                    workspace_path,
                    end_head_sha,
                    int(end_dirty),
                    permit["root_event_key"],
                ),
            )
            db.execute(
                "UPDATE review_repair_permits SET consumed_at=? WHERE permit_id=? AND consumed_at IS NULL",
                (now, permit_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                (WorkflowPhase.REVIEW_EXECUTION.value, now, permit["thread_id"]),
            )
        return self.execution_attempt(attempt_id)  # type: ignore[return-value]

    def mark_repair_attempt_failed(self, attempt_id: str, *, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE execution_attempts SET status=?,completed_at=? WHERE attempt_id=?",
                (AttemptStatus.FAILED.value, now, attempt_id),
            )
            db.execute(
                "UPDATE review_repair_permits SET invalidated_at=? "
                "WHERE permit_id=(SELECT authorization_id FROM execution_attempts WHERE attempt_id=?) "
                "AND consumed_at IS NULL",
                (now, attempt_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=(SELECT thread_id FROM execution_attempts WHERE attempt_id=?)",
                (WorkflowPhase.REVIEW_BLOCKED.value, now, attempt_id),
            )

    def recover_orphaned_repair_attempt(self, attempt_id: str, *, now: str) -> None:
        """Fail a lock-free running repair without consuming its permit."""
        with self.transaction(immediate=True) as db:
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            permit = db.execute(
                "SELECT * FROM review_repair_permits WHERE permit_id=?",
                (attempt["authorization_id"],) if attempt else (None,),
            ).fetchone()
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id=?",
                (attempt["thread_id"],) if attempt else (None,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id=?",
                (attempt["plan_id"],) if attempt else (None,),
            ).fetchone()
            parent = db.execute(
                "SELECT * FROM execution_reviews WHERE review_id=?",
                (attempt["parent_review_id"],) if attempt else (None,),
            ).fetchone()
            parent_attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?",
                (parent["attempt_id"],) if parent else (None,),
            ).fetchone()
            latest = db.execute(
                "SELECT attempt_id FROM execution_attempts WHERE thread_id=? AND cycle_id=? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (attempt["thread_id"], attempt["cycle_id"])
                if attempt
                else (None, None),
            ).fetchone()
            if (
                not attempt
                or not permit
                or not state
                or not plan
                or not parent
                or not latest
                or attempt["kind"] != AttemptKind.REVIEW_REPAIR.value
                or attempt["status"] != AttemptStatus.RUNNING.value
                or permit["consumed_at"] is not None
                or permit["invalidated_at"] is not None
                or state["phase"] != WorkflowPhase.EXECUTING.value
                or state["cycle_id"] != attempt["cycle_id"]
                or state["root_event_key"] != attempt["root_event_key"]
                or state["current_plan_id"] != attempt["plan_id"]
                or permit["thread_id"] != attempt["thread_id"]
                or permit["cycle_id"] != attempt["cycle_id"]
                or permit["plan_id"] != attempt["plan_id"]
                or permit["plan_version"] != attempt["plan_version"]
                or permit["root_event_key"] != attempt["root_event_key"]
                or attempt["authorization_id"] != permit["permit_id"]
                or permit["parent_review_id"] != attempt["parent_review_id"]
                or permit["repair_round"] != attempt["repair_round"]
                or plan["version"] != attempt["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or parent["verdict"] != "NEEDS_FIXES"
                or parent["thread_id"] != attempt["thread_id"]
                or parent["cycle_id"] != attempt["cycle_id"]
                or parent["root_event_key"] != attempt["root_event_key"]
                or parent["plan_id"] != attempt["plan_id"]
                or parent["plan_version"] != attempt["plan_version"]
                or not parent_attempt
                or parent_attempt["status"] != AttemptStatus.SUCCEEDED.value
                or parent_attempt["thread_id"] != attempt["thread_id"]
                or parent_attempt["cycle_id"] != attempt["cycle_id"]
                or parent_attempt["root_event_key"] != attempt["root_event_key"]
                or parent_attempt["plan_id"] != attempt["plan_id"]
                or parent_attempt["plan_version"] != attempt["plan_version"]
                or parent_attempt["attempt_number"] + 1 != attempt["attempt_number"]
                or (parent_attempt["repair_round"] or 0) + 1 != attempt["repair_round"]
                or latest["attempt_id"] != attempt["attempt_id"]
            ):
                raise ValueError("orphaned repair attempt binding is stale")
            if attempt["repair_recovery_count"] >= MAX_REPAIR_EXECUTION_RECOVERIES:
                db.execute(
                    "UPDATE execution_attempts SET status=?,completed_at=? WHERE attempt_id=?",
                    (AttemptStatus.FAILED.value, now, attempt_id),
                )
                db.execute(
                    "UPDATE review_repair_permits SET invalidated_at=? "
                    "WHERE permit_id=? AND consumed_at IS NULL",
                    (now, attempt["authorization_id"]),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REVIEW_BLOCKED.value, now, attempt["thread_id"]),
                )
            else:
                db.execute(
                    "UPDATE execution_attempts SET repair_recovery_count=repair_recovery_count+1, "
                    "status=?,completed_at=? WHERE attempt_id=?",
                    (AttemptStatus.FAILED.value, now, attempt_id),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=?",
                    (WorkflowPhase.REPAIR_READY.value, now, attempt["thread_id"]),
                )

    def record_review_infrastructure_failure(self, attempt_id: str, *, now: str) -> str:
        """Count one review-infrastructure failure; fail closed at the bound.

        Returns "RETRY" while budget remains, so the caller re-raises and the
        dispatcher's existing backoff drives the next attempt, or "EXHAUSTED"
        once the bound is reached, having moved the thread to REVIEW_BLOCKED.

        The counter lives on the attempt row so a hard crash consumes budget
        rather than resetting it -- the same reason execution retries are
        committed before each run.
        """
        with self.transaction(immediate=True) as db:
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise ValueError("execution attempt is missing")
            if attempt["review_recovery_count"] >= MAX_REVIEW_RECOVERIES:
                db.execute(
                    "UPDATE issue_workflow_state SET phase=?,updated_at=? "
                    "WHERE thread_id=?",
                    (WorkflowPhase.REVIEW_BLOCKED.value, now, attempt["thread_id"]),
                )
                return "EXHAUSTED"
            db.execute(
                "UPDATE execution_attempts "
                "SET review_recovery_count=review_recovery_count+1 "
                "WHERE attempt_id=?",
                (attempt_id,),
            )
            return "RETRY"

    def recover_orphaned_initial_attempt(
        self, attempt_id: str, *, now: str, max_recoveries: int
    ) -> str:
        """Reclaim an INITIAL execution whose worker died, or fail closed.

        Only the caller's IssueThread lock proves the previous worker is gone,
        so this is called under it.  The attempt keeps its identity: the same
        cycle, plan, permit and workspace are reused, and nothing here claims
        the dead attempt succeeded.  Returns "RETRY" or "EXHAUSTED".
        """
        with self.transaction(immediate=True) as db:
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None or attempt["kind"] != AttemptKind.INITIAL.value:
                raise ValueError("attempt is not a recoverable initial execution")
            if attempt["status"] != AttemptStatus.RUNNING.value:
                raise ValueError("initial attempt is not running")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (attempt["thread_id"],),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (attempt["plan_id"],)
            ).fetchone()
            permit = db.execute(
                "SELECT * FROM execution_permits WHERE permit_id = ?",
                (attempt["authorization_id"],),
            ).fetchone()
            if (
                state is None
                or plan is None
                or permit is None
                or state["phase"] != WorkflowPhase.EXECUTING.value
                or state["cycle_id"] != attempt["cycle_id"]
                or state["current_plan_id"] != attempt["plan_id"]
                or plan["version"] != attempt["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or permit["invalidated_at"] is not None
            ):
                # Authorization no longer proves this execution may resume.
                db.execute(
                    "UPDATE execution_attempts SET status = ?, completed_at = ? "
                    "WHERE attempt_id = ?",
                    (AttemptStatus.FAILED.value, now, attempt_id),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                    "WHERE thread_id = ? AND phase = ?",
                    (
                        WorkflowPhase.REVIEW_BLOCKED.value,
                        now,
                        attempt["thread_id"],
                        WorkflowPhase.EXECUTING.value,
                    ),
                )
                return "EXHAUSTED"
            # `retry_count` is bumped by ensure_execution_attempt immediately
            # before each model run, so it already counts executions durably --
            # including ones lost to a hard crash.  No parallel counter.
            if attempt["retry_count"] >= max_recoveries:
                db.execute(
                    "UPDATE execution_attempts SET status = ?, completed_at = ? "
                    "WHERE attempt_id = ?",
                    (AttemptStatus.FAILED.value, now, attempt_id),
                )
                db.execute(
                    "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                    "WHERE thread_id = ? AND phase = ?",
                    (
                        WorkflowPhase.REVIEW_BLOCKED.value,
                        now,
                        attempt["thread_id"],
                        WorkflowPhase.EXECUTING.value,
                    ),
                )
                return "EXHAUSTED"
            db.execute(
                "UPDATE execution_attempts SET status = ?, completed_at = ? "
                "WHERE attempt_id = ?",
                (AttemptStatus.INTERRUPTED.value, now, attempt_id),
            )
            target = self._execution_target(
                db,
                thread_id=attempt["thread_id"],
                cycle_id=attempt["cycle_id"],
                root_event_key=attempt["root_event_key"],
            )
            table = (
                "logical_executions"
                if target.startswith("execution-")
                else "event_executions"
            )
            key = "execution_id" if table == "logical_executions" else "event_key"
            db.execute(
                f"UPDATE {table} SET status = ?, completed_at = NULL, "
                f"error_message = ? WHERE {key} = ? AND status = ?",
                (
                    ExecutionStatus.RETRY_PENDING.value,
                    "executor died while the initial execution was running",
                    target,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            db.execute(
                "UPDATE execution_permits SET consumed_at = NULL WHERE permit_id = ?",
                (attempt["authorization_id"],),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ? AND phase = ?",
                (
                    WorkflowPhase.EXECUTION_READY.value,
                    now,
                    attempt["thread_id"],
                    WorkflowPhase.EXECUTING.value,
                ),
            )
            return "RETRY"

    def fail_closed_repair_recovery(self, attempt_id: str, *, now: str) -> None:
        """Quarantine impossible EXECUTING/repair state without claiming success."""
        with self.transaction(immediate=True) as db:
            attempt = db.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if not attempt or attempt["kind"] != AttemptKind.REVIEW_REPAIR.value:
                raise ValueError("repair recovery attempt is invalid")
            db.execute(
                "UPDATE issue_workflow_state SET phase=?,updated_at=? WHERE thread_id=? "
                "AND phase=?",
                (
                    WorkflowPhase.REVIEW_BLOCKED.value,
                    now,
                    attempt["thread_id"],
                    WorkflowPhase.EXECUTING.value,
                ),
            )

    # ------------------------------------------------------------------
    # Publication identity
    #
    # A publication belongs to one exact lifecycle -- thread, cycle and
    # logical workflow input -- never to a SourceEvent.  One SourceEvent can
    # back several logical inputs, so `source_event_key` is provenance only.
    # ------------------------------------------------------------------

    @staticmethod
    def _execution_row(
        db: sqlite3.Connection,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str,
    ) -> sqlite3.Row | None:
        if root_input_id != root_event_key:
            return db.execute(
                "SELECT * FROM logical_executions WHERE execution_id = ?",
                (
                    execution_id_for(
                        thread_id=thread_id,
                        cycle_id=cycle_id,
                        root_input_id=root_input_id,
                    ),
                ),
            ).fetchone()
        return db.execute(
            "SELECT * FROM event_executions WHERE event_key = ?", (root_event_key,)
        ).fetchone()

    @staticmethod
    def _publication_target(
        db: sqlite3.Connection, thread_id: str
    ) -> PublicationTarget | None:
        """Resolve the exact lifecycle a thread is currently allowed to publish.

        Every predicate is bound to the current thread/cycle/logical input, so
        an ACCEPT from one logical input can never authorize another one that
        happens to share the same SourceEvent.
        """
        declarative = SQLiteGitHubStore._declarative_publication_target(db, thread_id)
        if declarative is not None:
            return declarative
        if (
            db.execute(
                "SELECT 1 FROM workflow_cycles_v1 WHERE thread_id=? LIMIT 1",
                (thread_id,),
            ).fetchone()
            is not None
        ):
            # Once a thread enters the declarative authority, historical
            # compatibility rows can never authorize a later publication.
            return None
        state = db.execute(
            "SELECT * FROM issue_workflow_state WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if state is None or state["phase"] != WorkflowPhase.AWAITING_PUBLICATION.value:
            return None
        if not state["current_plan_id"]:
            return None
        plan = db.execute(
            "SELECT * FROM issue_plans WHERE plan_id = ?", (state["current_plan_id"],)
        ).fetchone()
        if plan is None:
            return None
        if (
            plan["thread_id"] != thread_id
            or plan["cycle_id"] != state["cycle_id"]
            or plan["root_event_key"] != state["root_event_key"]
            or plan["status"]
            not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
        ):
            return None
        root_input_id = (
            state["root_input_id"] or plan["root_input_id"] or state["root_event_key"]
        )
        if plan["root_input_id"] and plan["root_input_id"] != root_input_id:
            return None
        execution = SQLiteGitHubStore._execution_row(
            db,
            thread_id=thread_id,
            cycle_id=state["cycle_id"],
            root_event_key=state["root_event_key"],
            root_input_id=root_input_id,
        )
        if execution is None or execution["status"] != ExecutionStatus.SUCCEEDED.value:
            return None
        attempt = db.execute(
            """SELECT * FROM execution_attempts WHERE thread_id = ? AND cycle_id = ?
               ORDER BY attempt_number DESC LIMIT 1""",
            (thread_id, state["cycle_id"]),
        ).fetchone()
        if (
            attempt is None
            or attempt["status"] != AttemptStatus.SUCCEEDED.value
            or attempt["plan_id"] != plan["plan_id"]
            or attempt["plan_version"] != plan["version"]
            or attempt["root_event_key"] != state["root_event_key"]
        ):
            return None
        review = db.execute(
            "SELECT * FROM execution_reviews WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        ).fetchone()
        if (
            review is None
            or review["verdict"] != "ACCEPT"
            or review["thread_id"] != thread_id
            or review["cycle_id"] != state["cycle_id"]
            or review["root_event_key"] != state["root_event_key"]
            or review["plan_id"] != plan["plan_id"]
            or review["plan_version"] != plan["version"]
        ):
            return None
        workspace = db.execute(
            "SELECT branch_name FROM thread_workspaces WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if workspace is None:
            return None
        return PublicationTarget(
            publication_id=publication_id_for(
                thread_id=thread_id,
                cycle_id=state["cycle_id"],
                root_input_id=root_input_id,
            ),
            source_event_key=state["root_event_key"],
            thread_id=thread_id,
            cycle_id=state["cycle_id"],
            root_input_id=root_input_id,
            repo_id=state["repo_id"],
            repo_full_name=state["repo_full_name"],
            issue_number=state["issue_number"],
            branch_name=workspace["branch_name"],
            plan_id=plan["plan_id"],
            plan_version=plan["version"],
            attempt_id=attempt["attempt_id"],
            review_id=review["review_id"],
            execution_completed_at=execution["completed_at"],
        )

    @staticmethod
    def _declarative_publication_target(
        db: sqlite3.Connection, thread_id: str
    ) -> PublicationTarget | None:
        """Prove cumulative publication eligibility from generic task state."""
        if db.execute(
            """SELECT 1 FROM revision_inputs_v1
               WHERE thread_id=? AND status='PENDING' LIMIT 1""",
            (thread_id,),
        ).fetchone():
            return None
        cycle = db.execute(
            """SELECT * FROM workflow_cycles_v1 WHERE thread_id=?
               ORDER BY cycle_id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if (
            cycle is None
            or cycle["status"] != "AWAITING_PUBLICATION"
            or cycle["active_task_id"] is not None
        ):
            return None
        lifecycle = db.execute(
            "SELECT * FROM thread_workflow_lifecycle_v1 WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if lifecycle is None or lifecycle["initial_state"] not in {
            "COMPLETE",
            "PUBLISHED",
        }:
            return None
        initial = db.execute(
            "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
            (lifecycle["initial_workflow_cycle_id"],),
        ).fetchone()
        if (
            initial is None
            or initial["thread_id"] != thread_id
            or initial["cycle_kind"] != "INITIAL"
            or initial["workflow_digest"] != lifecycle["initial_workflow_digest"]
            or initial["status"] not in {"AWAITING_PUBLICATION", "PUBLISHED"}
        ):
            return None
        if cycle["cycle_kind"] == "REVISION" and (
            cycle["workflow_spec_ref"]
            != f"derived:{initial['workflow_cycle_id']}:{initial['workflow_digest']}"
            or cycle["workflow_id"] != f"revision-{initial['workflow_digest'][:16]}"
            or cycle["revision_sequence"] is None
        ):
            return None
        try:
            document = json.loads(cycle["workflow_spec_json"])
            canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
            declared = document["tasks"]
            declared_ids = [item["id"] for item in declared]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            document.get("workflow_id") != cycle["workflow_id"]
            or document.get("version") != cycle["workflow_version"]
            or hashlib.sha256(canonical.encode()).hexdigest()
            != cycle["workflow_digest"]
            or not declared_ids
            or len(declared_ids) != len(set(declared_ids))
        ):
            return None
        tasks = db.execute(
            """SELECT * FROM workflow_task_runs_v1 WHERE workflow_cycle_id=?
               ORDER BY declaration_index""",
            (cycle["workflow_cycle_id"],),
        ).fetchall()
        if len(tasks) != len(declared_ids):
            return None
        root = db.execute(
            "SELECT * FROM source_events WHERE event_key=?",
            (cycle["root_input_id"],),
        ).fetchone()
        if root is None and str(cycle["root_input_id"]).startswith("deferred-"):
            root = db.execute(
                """SELECT se.* FROM deferred_followups AS df
                   JOIN source_events AS se ON se.event_key=df.source_event_key
                   WHERE df.deferred_id=?""",
                (cycle["root_input_id"],),
            ).fetchone()
        if root is None and str(cycle["root_input_id"]).startswith("revision-input-"):
            root = db.execute(
                """SELECT se.* FROM revision_inputs_v1 AS ri
                   JOIN source_events AS se ON se.event_key=ri.source_event_key
                   WHERE ri.revision_input_id=?""",
                (cycle["root_input_id"],),
            ).fetchone()
        if root is None:
            return None
        thread = db.execute(
            "SELECT * FROM issue_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if thread is None:
            return None
        expected_approval_mode = (
            "AUTO"
            if thread["interaction_mode"] == InteractionMode.AUTO.value
            else "HUMAN"
        )
        completed_at: str | None = None
        last_plan = last_execution = last_validation = None
        for index, (task, declared_task) in enumerate(zip(tasks, declared)):
            try:
                dependencies = json.loads(task["dependencies_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                task["declaration_index"] != index
                or task["task_id"] != declared_task["id"]
                or task["thread_id"] != cycle["thread_id"]
                or task["cycle_id"] != cycle["cycle_id"]
                or task["workflow_id"] != cycle["workflow_id"]
                or dependencies != declared_task.get("depends_on", [])
                or task["status"] != "DONE"
                or task["phase"] != "DONE"
                or not task["current_plan_id"]
                or task["waiting_from_phase"] is not None
                or task["clarification_occurrence_key"] is not None
            ):
                return None
            plan = db.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task["current_plan_id"],),
            ).fetchone()
            if (
                plan is None
                or plan["task_run_id"] != task["task_run_id"]
                or plan["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or plan["task_id"] != task["task_id"]
                or plan["version"] < 1
                or hashlib.sha256(plan["plan_text"].strip().encode()).hexdigest()
                != plan["plan_digest"]
                or plan["posted_comment_id"] <= 0
                or plan["status"] != "APPROVED"
                or not plan["approved_at"]
                or not plan["approved_by"]
                or not plan["approval_event_key"]
            ):
                return None
            permit = db.execute(
                """SELECT * FROM workflow_task_permits_v1
                   WHERE task_run_id=? AND plan_id=? AND plan_version=?
                     AND plan_digest=? AND invalidated_at IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (
                    task["task_run_id"],
                    plan["plan_id"],
                    plan["version"],
                    plan["plan_digest"],
                ),
            ).fetchone()
            execution = db.execute(
                """SELECT * FROM workflow_task_executions_v1
                   WHERE task_run_id=? AND plan_id=? AND attempt=?
                   ORDER BY completed_at DESC LIMIT 1""",
                (task["task_run_id"], plan["plan_id"], task["execution_attempt"]),
            ).fetchone()
            validation = db.execute(
                """SELECT * FROM workflow_task_validations_v1
                   WHERE task_run_id=? ORDER BY validation_round DESC LIMIT 1""",
                (task["task_run_id"],),
            ).fetchone()
            approval = db.execute(
                "SELECT * FROM source_events WHERE event_key=?",
                (plan["approval_event_key"],),
            ).fetchone()
            result = (
                db.execute(
                    """SELECT * FROM workflow_task_results_v1
                       WHERE task_run_id=? AND plan_id=? AND execution_id=?
                         AND validation_id=?""",
                    (
                        task["task_run_id"],
                        plan["plan_id"],
                        execution["execution_id"] if execution else "",
                        validation["validation_id"] if validation else "",
                    ),
                ).fetchone()
                if execution is not None and validation is not None
                else None
            )
            result_approval = (
                db.execute(
                    """SELECT * FROM workflow_task_result_approvals_v1
                       WHERE result_id=? AND invalidated_at IS NULL""",
                    (result["result_id"],),
                ).fetchone()
                if result is not None
                else None
            )
            human_result_event = (
                db.execute(
                    "SELECT * FROM source_events WHERE event_key=?",
                    (result_approval["approval_event_key"],),
                ).fetchone()
                if result_approval is not None and result_approval["mode"] == "HUMAN"
                else None
            )
            if execution is None or validation is None:
                return None
            try:
                execution_evidence = json.loads(execution["evidence_json"])
                validation_evidence = json.loads(validation["evidence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                permit is None
                or permit["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or permit["approval_event_key"] != plan["approval_event_key"]
                or permit["approved_by"] != plan["approved_by"]
                or permit["approval_mode"] != expected_approval_mode
                or (
                    expected_approval_mode == "HUMAN"
                    and (
                        approval is None
                        or approval["author_login"] != plan["approved_by"]
                        or approval["source_created_at"] != plan["approved_at"]
                        or approval["source_created_at"] <= plan["posted_at"]
                        or not is_exact_agent_approval(approval["body"])
                        or approval["origin_surface"] != root["origin_surface"]
                        or approval["subject_number"] != root["subject_number"]
                    )
                )
                or (
                    expected_approval_mode == "HUMAN"
                    and root["origin_surface"] == "PR_INLINE_REVIEW"
                    and approval is not None
                    and approval["review_thread_root_id"]
                    != root["review_thread_root_id"]
                )
                or (
                    expected_approval_mode == "AUTO"
                    and (
                        permit["approved_by"] != "sweforge:auto-policy"
                        or not permit["approval_event_key"].startswith(
                            "auto-plan-authority:plan-approval:"
                        )
                    )
                )
                or (
                    expected_approval_mode == "HUMAN"
                    and human_result_event is not None
                    and root["origin_surface"] == "PR_INLINE_REVIEW"
                    and human_result_event["review_thread_root_id"]
                    != root["review_thread_root_id"]
                )
                or execution["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or execution["permit_id"] != permit["permit_id"]
                or execution["status"] != "SUCCEEDED"
                or not isinstance(execution_evidence, dict)
                or not isinstance(execution_evidence.get("tool_observations"), list)
                or validation["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or validation["plan_id"] != plan["plan_id"]
                or validation["execution_attempt"] != execution["attempt"]
                or validation["verdict"] != "ACCEPT"
                or not isinstance(validation_evidence, dict)
                or not isinstance(validation_evidence.get("validation_runs"), list)
                or not validation_evidence["validation_runs"]
                or result is None
                or result["result_occurrence_key"].startswith("plan-approval:")
                or result_approval is None
                or result_approval["task_run_id"] != task["task_run_id"]
                or result_approval["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or result_approval["plan_id"] != plan["plan_id"]
                or result_approval["execution_id"] != execution["execution_id"]
                or result_approval["validation_id"] != validation["validation_id"]
                or result_approval["result_occurrence_key"]
                != result["result_occurrence_key"]
                or result_approval["mode"] != expected_approval_mode
                or (
                    expected_approval_mode == "HUMAN"
                    and (
                        human_result_event is None
                        or human_result_event["author_login"]
                        != result_approval["approved_by"]
                        or human_result_event["source_created_at"]
                        != result_approval["approved_at"]
                        or human_result_event["source_created_at"]
                        <= result["posted_at"]
                        or not is_exact_agent_approval(human_result_event["body"])
                        or human_result_event["origin_surface"]
                        != root["origin_surface"]
                        or human_result_event["subject_number"]
                        != root["subject_number"]
                    )
                )
                or (
                    expected_approval_mode == "AUTO"
                    and (
                        result_approval["approval_event_key"] is not None
                        or result_approval["approved_by"] != "sweforge:auto-policy"
                    )
                )
            ):
                return None
            completed_at = max(completed_at or "", execution["completed_at"])
            last_plan, last_execution, last_validation = plan, execution, validation
        workspace = db.execute(
            "SELECT branch_name FROM thread_workspaces WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if root is None or thread is None or workspace is None:
            return None
        return PublicationTarget(
            publication_id=publication_id_for(
                thread_id=thread_id,
                cycle_id=cycle["cycle_id"],
                root_input_id=cycle["root_input_id"],
            ),
            source_event_key=root["event_key"],
            thread_id=thread_id,
            cycle_id=cycle["cycle_id"],
            root_input_id=cycle["root_input_id"],
            repo_id=thread["repo_id"],
            repo_full_name=thread["repo_full_name"],
            issue_number=thread["issue_number"],
            branch_name=workspace["branch_name"],
            plan_id=last_plan["plan_id"],
            plan_version=last_plan["version"],
            attempt_id=last_execution["execution_id"],
            review_id=last_validation["validation_id"],
            execution_completed_at=completed_at,
            workflow_cycle_id=cycle["workflow_cycle_id"],
            declarative=True,
        )

    def publication_target(self, thread_id: str) -> PublicationTarget | None:
        return self._publication_target(self.connection, thread_id)

    def eligible_publication_id(self, thread_id: str) -> str | None:
        """Return the publication identity this thread may publish right now."""
        target = self._publication_target(self.connection, thread_id)
        return target.publication_id if target else None

    def publication_is_eligible(self, publication_id: str) -> bool:
        """True only when this exact publication is the thread's current one."""
        row = self.connection.execute(
            "SELECT thread_id FROM logical_publications WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        if row is None:
            return False
        target = self._publication_target(self.connection, row["thread_id"])
        return target is not None and target.publication_id == publication_id

    def publication_for_id(self, publication_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM logical_publications WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        return self._publication_record(row) if row else None

    def publication_for_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str | None,
    ) -> PublicationRecord | None:
        return self.publication_for_id(
            publication_id_for(
                thread_id=thread_id,
                cycle_id=cycle_id,
                root_input_id=root_input_id or root_event_key,
            )
        )

    def publications_for_event(self, event_key: str) -> list[PublicationRecord]:
        """Diagnostic: every publication that retains this SourceEvent."""
        rows = self.connection.execute(
            """SELECT * FROM logical_publications WHERE source_event_key = ?
               ORDER BY cycle_id, publication_id""",
            (event_key,),
        ).fetchall()
        return [self._publication_record(row) for row in rows]

    def publication_for_event(self, event_key: str) -> PublicationRecord | None:
        """Diagnostic lookup; fails closed when a SourceEvent is ambiguous."""
        records = self.publications_for_event(event_key)
        if len(records) > 1:
            raise AmbiguousLifecycleError(
                f"SourceEvent {event_key} backs {len(records)} publications; "
                "use publication_for_id or publication_for_cycle"
            )
        return records[0] if records else None

    def resolve_publication_id(self, identifier: str) -> str:
        """Accept a publication id or an unambiguous legacy event key."""
        if identifier.startswith("publication-"):
            return identifier
        record = self.publication_for_event(identifier)
        if record is None:
            raise ValueError(f"no publication exists for {identifier}")
        return record.publication_id

    def next_publication(
        self, publication_id: str | None = None
    ) -> PublicationRecord | None:
        rows = self.connection.execute(
            """SELECT thread_id FROM issue_workflow_state WHERE phase = ?
               UNION
               SELECT thread_id FROM workflow_cycles_v1
               WHERE status = 'AWAITING_PUBLICATION'
               ORDER BY thread_id""",
            (WorkflowPhase.AWAITING_PUBLICATION.value,),
        ).fetchall()
        targets: list[PublicationTarget] = []
        for row in rows:
            target = self._publication_target(self.connection, row["thread_id"])
            if target is None:
                continue
            if publication_id is not None and target.publication_id != publication_id:
                continue
            existing = self.publication_for_id(target.publication_id)
            if existing is not None and existing.status not in (
                RESUMABLE_PUBLICATION_STATUSES
            ):
                continue
            targets.append(target)
        if not targets:
            return None
        targets.sort(
            key=lambda item: (item.execution_completed_at or "", item.publication_id)
        )
        return self.ensure_publication(
            thread_id=targets[0].thread_id,
            now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    def ensure_publication(self, *, thread_id: str, now: str) -> PublicationRecord:
        with self.transaction(immediate=True) as db:
            target = self._publication_target(db, thread_id)
            if target is None:
                raise ValueError(
                    "publication is blocked until a matching ACCEPT review"
                )
            existing = db.execute(
                "SELECT 1 FROM logical_publications WHERE publication_id = ?",
                (target.publication_id,),
            ).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO logical_publications(
                       publication_id, source_event_key, thread_id, cycle_id,
                       root_input_id, repo_id, repo_full_name, issue_number,
                       status, branch_name, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target.publication_id,
                        target.source_event_key,
                        target.thread_id,
                        target.cycle_id,
                        target.root_input_id,
                        target.repo_id,
                        target.repo_full_name,
                        target.issue_number,
                        PublicationStatus.PENDING.value,
                        target.branch_name,
                        now,
                        now,
                    ),
                )
        return self.publication_for_id(target.publication_id)  # type: ignore[return-value]

    def update_publication(
        self,
        publication_id: str,
        *,
        status: PublicationStatus,
        now: str,
        **fields: object,
    ) -> PublicationRecord:
        allowed = {
            "local_commit_sha",
            "remote_commit_sha",
            "pr_number",
            "pr_url",
            "comment_id",
            "error_message",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown publication fields: {sorted(unknown)}")
        assignments = ["status = ?", "updated_at = ?"]
        values: list[object] = [status.value, now]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(publication_id)
        with self.transaction() as db:
            if (
                db.execute(
                    "UPDATE logical_publications SET "
                    f"{', '.join(assignments)} WHERE publication_id = ?",
                    values,
                ).rowcount
                != 1
            ):
                raise ValueError("publication does not exist")
        return self.publication_for_id(publication_id)  # type: ignore[return-value]

    def retry_publication(self, publication_id: str, *, now: str) -> PublicationRecord:
        publication = self.publication_for_id(publication_id)
        if publication is None:
            raise ValueError("publication does not exist")
        if publication.status != PublicationStatus.FAILED:
            raise ValueError("only failed publications can be retried")
        return self.update_publication(
            publication_id,
            status=PublicationStatus.PENDING,
            now=now,
            error_message=None,
        )

    @staticmethod
    def _accepted_cycle_material(
        db: sqlite3.Connection, cycle: sqlite3.Row
    ) -> tuple[AcceptedTaskLifecycle, ...] | None:
        """Return a cycle only when every declared task has exact acceptance proof."""
        if cycle["status"] not in {"AWAITING_PUBLICATION", "PUBLISHED"}:
            return None
        try:
            document = json.loads(cycle["workflow_spec_json"])
            canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
            declared = document["tasks"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            document.get("workflow_id") != cycle["workflow_id"]
            or document.get("version") != cycle["workflow_version"]
            or hashlib.sha256(canonical.encode()).hexdigest()
            != cycle["workflow_digest"]
            or not declared
        ):
            return None
        tasks = db.execute(
            """SELECT * FROM workflow_task_runs_v1
               WHERE workflow_cycle_id=? ORDER BY declaration_index""",
            (cycle["workflow_cycle_id"],),
        ).fetchall()
        if len(tasks) != len(declared):
            return None
        accepted: list[AcceptedTaskLifecycle] = []
        for index, (task, declaration) in enumerate(zip(tasks, declared)):
            try:
                dependencies = json.loads(task["dependencies_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                task["declaration_index"] != index
                or task["task_id"] != declaration.get("id")
                or dependencies != declaration.get("depends_on", [])
                or task["thread_id"] != cycle["thread_id"]
                or task["cycle_id"] != cycle["cycle_id"]
                or task["workflow_id"] != cycle["workflow_id"]
                or task["status"] != "DONE"
                or task["phase"] != "DONE"
                or not task["current_plan_id"]
                or task["waiting_from_phase"] is not None
                or task["clarification_occurrence_key"] is not None
            ):
                return None
            plan = db.execute(
                "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task["current_plan_id"],),
            ).fetchone()
            if (
                plan is None
                or plan["task_run_id"] != task["task_run_id"]
                or plan["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or plan["task_id"] != task["task_id"]
                or plan["status"] != "APPROVED"
                or plan["version"] < 1
                or plan["posted_comment_id"] <= 0
                or not plan["approved_at"]
                or not plan["approved_by"]
                or not plan["approval_event_key"]
                or hashlib.sha256(plan["plan_text"].strip().encode()).hexdigest()
                != plan["plan_digest"]
            ):
                return None
            execution = db.execute(
                """SELECT * FROM workflow_task_executions_v1
                   WHERE task_run_id=? AND plan_id=? AND attempt=?""",
                (task["task_run_id"], plan["plan_id"], task["execution_attempt"]),
            ).fetchone()
            validation = db.execute(
                """SELECT * FROM workflow_task_validations_v1
                   WHERE task_run_id=? AND plan_id=? AND execution_attempt=?
                     AND validation_round=?""",
                (
                    task["task_run_id"],
                    plan["plan_id"],
                    task["execution_attempt"],
                    task["validation_round"],
                ),
            ).fetchone()
            if execution is None or validation is None:
                return None
            permit = db.execute(
                """SELECT * FROM workflow_task_permits_v1
                   WHERE permit_id=? AND invalidated_at IS NULL""",
                (execution["permit_id"],),
            ).fetchone()
            result = db.execute(
                """SELECT * FROM workflow_task_results_v1
                   WHERE task_run_id=? AND plan_id=? AND execution_id=?
                     AND validation_id=?""",
                (
                    task["task_run_id"],
                    plan["plan_id"],
                    execution["execution_id"],
                    validation["validation_id"],
                ),
            ).fetchone()
            result_approval = (
                db.execute(
                    """SELECT * FROM workflow_task_result_approvals_v1
                       WHERE result_id=? AND invalidated_at IS NULL""",
                    (result["result_id"],),
                ).fetchone()
                if result is not None
                else None
            )
            try:
                execution_evidence = json.loads(execution["evidence_json"])
                validation_evidence = json.loads(validation["evidence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                permit is None
                or permit["task_run_id"] != task["task_run_id"]
                or permit["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or permit["plan_id"] != plan["plan_id"]
                or permit["plan_version"] != plan["version"]
                or permit["plan_digest"] != plan["plan_digest"]
                or permit["approval_mode"] not in {"HUMAN", "AUTO"}
                or execution["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or execution["status"] != "SUCCEEDED"
                or not isinstance(execution_evidence, dict)
                or not isinstance(execution_evidence.get("tool_observations"), list)
                or validation["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or validation["verdict"] != "ACCEPT"
                or not isinstance(validation_evidence, dict)
                or not isinstance(validation_evidence.get("validation_runs"), list)
                or not validation_evidence["validation_runs"]
                or result is None
                or result["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or result["posted_comment_id"] <= 0
                or result["result_occurrence_key"].startswith("plan-approval:")
                or result_approval is None
                or result_approval["task_run_id"] != task["task_run_id"]
                or result_approval["workflow_cycle_id"] != cycle["workflow_cycle_id"]
                or result_approval["plan_id"] != plan["plan_id"]
                or result_approval["execution_id"] != execution["execution_id"]
                or result_approval["validation_id"] != validation["validation_id"]
                or result_approval["result_occurrence_key"]
                != result["result_occurrence_key"]
                or result_approval["mode"] != permit["approval_mode"]
                or (
                    result_approval["mode"] == "AUTO"
                    and (
                        result_approval["approval_event_key"] is not None
                        or result_approval["approved_by"] != "sweforge:auto-policy"
                        or permit["approved_by"] != "sweforge:auto-policy"
                        or not permit["approval_event_key"].startswith(
                            "auto-plan-authority:plan-approval:"
                        )
                    )
                )
                or (
                    result_approval["mode"] == "HUMAN"
                    and not result_approval["approval_event_key"]
                )
            ):
                return None
            accepted.append(
                AcceptedTaskLifecycle(
                    cycle_id=cycle["cycle_id"],
                    workflow_cycle_id=cycle["workflow_cycle_id"],
                    cycle_kind=cycle["cycle_kind"],
                    revision_sequence=cycle["revision_sequence"],
                    task_id=task["task_id"],
                    declaration_index=task["declaration_index"],
                    plan_id=plan["plan_id"],
                    plan_text=plan["plan_text"],
                    execution_id=execution["execution_id"],
                    execution_summary=execution["summary"],
                    validation_id=validation["validation_id"],
                    validation_summary=validation["summary"],
                )
            )
        return tuple(accepted)

    def accepted_lifecycle_material(
        self,
        thread_id: str,
        *,
        first_cycle_id: int = 1,
        last_cycle_id: int | None = None,
    ) -> list[AcceptedTaskLifecycle]:
        """Read bounded-input lifecycle facts without replaying agent messages."""
        clauses = ["thread_id=?", "cycle_id>=?"]
        values: list[object] = [thread_id, first_cycle_id]
        if last_cycle_id is not None:
            clauses.append("cycle_id<=?")
            values.append(last_cycle_id)
        cycles = self.connection.execute(
            "SELECT * FROM workflow_cycles_v1 WHERE "
            + " AND ".join(clauses)
            + " ORDER BY cycle_id",
            values,
        ).fetchall()
        material: list[AcceptedTaskLifecycle] = []
        for cycle in cycles:
            accepted = self._accepted_cycle_material(self.connection, cycle)
            if accepted is not None:
                material.extend(accepted)
        return material

    def publication_generation(self, publication_id: str) -> PublicationGeneration:
        """Derive exact cycle membership from durable finalization records."""
        publication = self.publication_for_id(publication_id)
        if publication is None:
            raise ValueError("publication does not exist")
        finalized = self.connection.execute(
            """SELECT 1 FROM repo_memory_learning
               WHERE thread_id=? AND cycle_id=? AND root_input_id=?""",
            (
                publication.thread_id,
                publication.cycle_id,
                publication.root_input_id,
            ),
        ).fetchone()
        if finalized is None:
            raise ValueError("publication has not been finalized")
        previous = self.connection.execute(
            """SELECT p.publication_id,p.cycle_id,p.remote_commit_sha
               FROM logical_publications AS p
               JOIN repo_memory_learning AS learning
                 ON learning.thread_id=p.thread_id
                AND learning.cycle_id=p.cycle_id
                AND learning.root_input_id=p.root_input_id
               WHERE p.thread_id=? AND p.cycle_id<?
                 AND p.status IN ('COMPLETED','NO_CHANGES')
               ORDER BY p.cycle_id DESC LIMIT 1""",
            (publication.thread_id, publication.cycle_id),
        ).fetchone()
        boundary = int(previous["cycle_id"]) if previous is not None else 0
        previous_commit_sha = (
            str(previous["remote_commit_sha"])
            if previous is not None and previous["remote_commit_sha"]
            else None
        )
        if previous is not None and previous_commit_sha is None:
            prior_commit = self.connection.execute(
                """SELECT p.remote_commit_sha
                   FROM logical_publications AS p
                   JOIN repo_memory_learning AS learning
                     ON learning.thread_id=p.thread_id
                    AND learning.cycle_id=p.cycle_id
                    AND learning.root_input_id=p.root_input_id
                   WHERE p.thread_id=? AND p.cycle_id<?
                     AND p.remote_commit_sha IS NOT NULL
                   ORDER BY p.cycle_id DESC LIMIT 1""",
                (publication.thread_id, publication.cycle_id),
            ).fetchone()
            previous_commit_sha = (
                str(prior_commit["remote_commit_sha"])
                if prior_commit is not None
                else None
            )
        cycles = self.connection.execute(
            """SELECT * FROM workflow_cycles_v1
               WHERE thread_id=? AND cycle_id>? AND cycle_id<=?
               ORDER BY cycle_id""",
            (publication.thread_id, boundary, publication.cycle_id),
        ).fetchall()
        if cycles:
            cycle_ids = tuple(int(cycle["cycle_id"]) for cycle in cycles)
            expected = tuple(range(boundary + 1, publication.cycle_id + 1))
            if cycle_ids != expected:
                raise ValueError("publication generation has a cycle gap")
            for cycle in cycles:
                if self._accepted_cycle_material(self.connection, cycle) is None:
                    raise ValueError(
                        "publication generation contains an unaccepted cycle"
                    )
        else:
            # Compatibility lifecycles predate the declarative cycle tables.
            cycle_ids = (publication.cycle_id,)
        return PublicationGeneration(
            publication_id=publication_id,
            thread_id=publication.thread_id,
            first_cycle_id=boundary + 1,
            last_cycle_id=publication.cycle_id,
            cycle_ids=cycle_ids,
            previous_publication_id=(
                str(previous["publication_id"]) if previous is not None else None
            ),
            previous_commit_sha=previous_commit_sha,
        )

    def repo_memory_candidates_for_generation(
        self, generation: PublicationGeneration, *, repo_id: int
    ) -> list[RepoMemoryCandidateRecord]:
        """Return only unsettled candidates belonging to this generation."""
        rows = self.connection.execute(
            """SELECT candidate.* FROM repo_memory_candidates AS candidate
               JOIN workflow_cycles_v1 AS cycle
                 ON cycle.thread_id=candidate.thread_id
                AND cycle.cycle_id=candidate.cycle_id
                AND cycle.root_input_id=candidate.root_input_id
               WHERE candidate.repo_id=? AND candidate.thread_id=?
                 AND candidate.cycle_id>=? AND candidate.cycle_id<=?
                 AND candidate.status='PROPOSED'
               ORDER BY candidate.cycle_id,candidate.created_at,
                        candidate.candidate_id""",
            (
                repo_id,
                generation.thread_id,
                generation.first_cycle_id,
                generation.last_cycle_id,
            ),
        ).fetchall()
        return [RepoMemoryCandidateRecord(**dict(row)) for row in rows]

    def revision_inputs_for_generation(
        self, generation: PublicationGeneration
    ) -> list[sqlite3.Row]:
        """Return exact consumed steering incorporated into this publication."""
        return self.connection.execute(
            """SELECT r.*,se.repo_full_name,se.source_kind,se.source_id,
                      se.source_updated_at,se.source_created_at,se.subject_kind,
                      se.subject_number,se.author_login,se.body AS source_body,
                      se.html_url,se.origin_surface,se.path,se.line,se.start_line,
                      se.side,se.start_side,se.diff_hunk,se.commit_id,
                      se.original_commit_id,se.in_reply_to_id,
                      se.pull_request_review_id,se.review_thread_root_id,
                      cycle.cycle_id
               FROM revision_inputs_v1 AS r
               JOIN workflow_cycles_v1 AS cycle
                 ON cycle.workflow_cycle_id=r.revision_workflow_cycle_id
               JOIN source_events AS se ON se.event_key=r.source_event_key
               WHERE r.thread_id=? AND r.status='CONSUMED'
                 AND cycle.cycle_id>=? AND cycle.cycle_id<=?
               ORDER BY cycle.cycle_id,se.source_updated_at,r.queued_at,
                        r.revision_input_id""",
            (
                generation.thread_id,
                generation.first_cycle_id,
                generation.last_cycle_id,
            ),
        ).fetchall()

    def finalize_publication(
        self, publication_id: str, *, now: str
    ) -> RepoMemoryLearningRecord:
        """Close one lifecycle and enqueue both durable learning jobs.

        Fails closed when the publication is not the thread's current one, so a
        stale completion can never finalize a newer cycle.
        """
        with self.transaction(immediate=True) as db:
            publication = db.execute(
                "SELECT * FROM logical_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise ValueError("publication does not exist")
            if publication["status"] not in (
                PublicationStatus.COMPLETED.value,
                PublicationStatus.NO_CHANGES.value,
            ):
                raise ValueError("publication has no publishable outcome yet")
            target = self._publication_target(db, publication["thread_id"])
            if target is None or target.publication_id != publication_id:
                raise ValueError("publication is stale for the current workflow cycle")
            if target.declarative:
                finalized = db.execute(
                    """UPDATE workflow_cycles_v1 SET status='PUBLISHED',updated_at=?
                       WHERE workflow_cycle_id=? AND status='AWAITING_PUBLICATION'
                         AND active_task_id IS NULL""",
                    (now, target.workflow_cycle_id),
                )
                if finalized.rowcount != 1:
                    raise ValueError("workflow state changed during finalization")
                db.execute(
                    """UPDATE workflow_cycles_v1 SET status='PUBLISHED',updated_at=?
                       WHERE thread_id=? AND cycle_id<?
                         AND status='AWAITING_PUBLICATION'""",
                    (now, target.thread_id, target.cycle_id),
                )
                lifecycle = db.execute(
                    """UPDATE thread_workflow_lifecycle_v1
                       SET initial_state='PUBLISHED',updated_at=?
                       WHERE thread_id=? AND initial_state IN ('COMPLETE','PUBLISHED')""",
                    (now, target.thread_id),
                )
                if lifecycle.rowcount != 1:
                    raise ValueError("initial workflow publication state is missing")
            else:
                db.execute(
                    """UPDATE issue_plans SET status = ?
                       WHERE plan_id = ? AND thread_id = ? AND cycle_id = ?
                         AND status IN (?, ?)""",
                    (
                        PlanStatus.EXECUTED.value,
                        target.plan_id,
                        target.thread_id,
                        target.cycle_id,
                        PlanStatus.APPROVED.value,
                        PlanStatus.AUTO_APPROVED.value,
                    ),
                )
            learning_id = memory_learning_id_for(
                thread_id=target.thread_id,
                cycle_id=target.cycle_id,
                root_input_id=target.root_input_id,
            )
            db.execute(
                """INSERT INTO repo_memory_learning(
                   learning_id, source_event_key, thread_id, cycle_id,
                   root_input_id, repo_id, status, accepted_candidates,
                   rejected_candidates, proposal_json, error_message,
                   attempt_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, '{}', NULL, 0, ?, ?)
                   ON CONFLICT(learning_id) DO NOTHING""",
                (
                    learning_id,
                    target.source_event_key,
                    target.thread_id,
                    target.cycle_id,
                    target.root_input_id,
                    target.repo_id,
                    MEMORY_LEARNING_PENDING,
                    now,
                    now,
                ),
            )
            # A historical case exists for every finalized lifecycle, with no
            # model discretion over whether the job was created.  Only
            # finalization reaches here, so failed or blocked work never
            # produces a "solved issue" record.
            metadata = db.execute(
                "SELECT title, body FROM issue_metadata "
                "WHERE repo_id = ? AND issue_number = ?",
                (target.repo_id, target.issue_number),
            ).fetchone()
            db.execute(
                """INSERT INTO issue_resolution_memory(
                   resolution_id, repo_id, thread_id, cycle_id, root_input_id,
                   source_event_key, issue_number, issue_title,
                   issue_description_snapshot, plan_id, publication_id,
                   status, attempt_count, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                   ON CONFLICT(resolution_id) DO NOTHING""",
                (
                    resolution_id_for(
                        thread_id=target.thread_id,
                        cycle_id=target.cycle_id,
                        root_input_id=target.root_input_id,
                    ),
                    target.repo_id,
                    target.thread_id,
                    target.cycle_id,
                    target.root_input_id,
                    target.source_event_key,
                    target.issue_number,
                    (metadata["title"] if metadata else "") or "",
                    (metadata["body"] if metadata else "") or "",
                    target.plan_id,
                    publication_id,
                    IssueResolutionStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            if not target.declarative:
                finalized = db.execute(
                    """UPDATE issue_workflow_state SET phase = ?, updated_at = ?
                       WHERE thread_id = ? AND cycle_id = ? AND phase = ?
                         AND root_event_key = ?
                         AND COALESCE(root_input_id, root_event_key) = ?""",
                    (
                        WorkflowPhase.IDLE.value,
                        now,
                        target.thread_id,
                        target.cycle_id,
                        WorkflowPhase.AWAITING_PUBLICATION.value,
                        target.source_event_key,
                        target.root_input_id,
                    ),
                )
                if finalized.rowcount != 1:
                    raise ValueError("workflow state changed during finalization")
        return self.memory_learning_for_id(learning_id)  # type: ignore[return-value]

    def declarative_publication_summary(self, publication_id: str) -> str | None:
        """Render bounded cumulative task summaries for a declarative PR body."""
        publication = self.publication_for_id(publication_id)
        if publication is None:
            return None
        cycle = self.connection.execute(
            """SELECT workflow_cycle_id,cycle_id FROM workflow_cycles_v1
               WHERE thread_id=? AND cycle_id=? AND root_input_id=?""",
            (publication.thread_id, publication.cycle_id, publication.root_input_id),
        ).fetchone()
        if cycle is None:
            return None
        rows = self.connection.execute(
            """SELECT c.cycle_kind,c.revision_sequence,t.task_id,e.summary
               FROM workflow_cycles_v1 AS c
               JOIN workflow_task_runs_v1 AS t
                 ON t.workflow_cycle_id=c.workflow_cycle_id
               JOIN workflow_task_executions_v1 AS e
                 ON e.task_run_id=t.task_run_id AND e.attempt=t.execution_attempt
               WHERE c.thread_id=? AND c.cycle_id<=?
                 AND c.status IN ('AWAITING_PUBLICATION','PUBLISHED')
               ORDER BY c.cycle_id,t.declaration_index""",
            (publication.thread_id, cycle["cycle_id"]),
        ).fetchall()
        return "\n\n".join(
            [
                "SWEForge completed the declared workflow tasks:",
                *[
                    (
                        f"### {row['task_id']}\n{row['summary']}"
                        if row["cycle_kind"] == "INITIAL"
                        else f"### revision {row['revision_sequence']}\n{row['summary']}"
                    )
                    for row in rows
                ],
            ]
        )[:12_000]

    @staticmethod
    def _publication_record(row: sqlite3.Row) -> PublicationRecord:
        values = dict(row)
        values["status"] = PublicationStatus(values["status"])
        return PublicationRecord(**values)

    def thread_workspace(self, thread_id: str) -> ThreadWorkspaceRecord | None:
        row = self.connection.execute(
            "SELECT * FROM thread_workspaces WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._workspace_record(row) if row else None

    def save_thread_workspace(self, record: ThreadWorkspaceRecord) -> None:
        with self.transaction() as db:
            thread = db.execute(
                "SELECT repo_id, issue_number FROM issue_threads WHERE thread_id = ?",
                (record.thread_id,),
            ).fetchone()
            if thread is None or (
                thread["repo_id"] != record.repo_id
                or thread["issue_number"] != record.issue_number
            ):
                raise ValueError("workspace metadata does not match the IssueThread")
            existing = db.execute(
                "SELECT * FROM thread_workspaces WHERE thread_id = ?",
                (record.thread_id,),
            ).fetchone()
            if existing:
                if self._workspace_record(existing) != record:
                    raise ValueError("workspace metadata conflicts with persisted data")
                return
            db.execute(
                """INSERT INTO thread_workspaces(
                   thread_id, repo_id, repo_full_name, issue_number,
                   source_repository_path, workspace_path, branch_name, base_commit,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.source_repository_path,
                    record.workspace_path,
                    record.branch_name,
                    record.base_commit,
                    record.created_at,
                    record.updated_at,
                ),
            )

    @staticmethod
    def _workspace_record(row: sqlite3.Row) -> ThreadWorkspaceRecord:
        return ThreadWorkspaceRecord(**dict(row))

    def _resolve_thread(
        self,
        db: sqlite3.Connection,
        event: SourceEvent,
        now: str,
        result: RecordBatchResult,
    ) -> str | None:
        if event.subject_kind == SubjectKind.PULL_REQUEST:
            row = db.execute(
                "SELECT thread_id FROM pr_thread_mappings "
                "WHERE repo_id = ? AND pr_number = ?",
                (event.repo_id, event.subject_number),
            ).fetchone()
            if row:
                result.events_routed += 1
                return row[0]
            result.pr_events_unrouted += 1
            return None
        thread_id = f"github:{event.repo_id}:issue:{event.subject_number}"
        existing = db.execute(
            "SELECT 1 FROM issue_threads WHERE repo_id = ? AND issue_number = ?",
            (event.repo_id, event.subject_number),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE issue_threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
        else:
            interaction_mode = (
                InteractionMode.AUTO
                if any(label.casefold() == "auto" for label in event.issue_labels)
                else InteractionMode.MANUAL
            )
            current_config = db.execute(
                "SELECT generation_id FROM repo_config_current_v1 WHERE repo_id=?",
                (event.repo_id,),
            ).fetchone()
            db.execute(
                """INSERT INTO issue_threads(thread_id, repo_id, repo_full_name,
                   issue_number, interaction_mode, config_generation_id,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    thread_id,
                    event.repo_id,
                    event.repo_full_name,
                    event.subject_number,
                    interaction_mode.value,
                    current_config["generation_id"] if current_config else None,
                    now,
                    now,
                ),
            )
            result.threads_created += 1
            result.created_thread_ids.append(thread_id)
        result.events_routed += 1
        return thread_id

    def register_pr_mapping(self, repo_id: int, pr_number: int, thread_id: str) -> None:
        with self.transaction() as db:
            thread = db.execute(
                "SELECT repo_id FROM issue_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if thread is None or thread[0] != repo_id:
                raise ValueError("PR mapping thread must belong to the repository")
            existing = db.execute(
                "SELECT thread_id FROM pr_thread_mappings "
                "WHERE repo_id = ? AND pr_number = ?",
                (repo_id, pr_number),
            ).fetchone()
            if existing and existing[0] != thread_id:
                raise ValueError("PR mapping already points to another thread")
            db.execute(
                "INSERT INTO pr_thread_mappings(repo_id, pr_number, thread_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(repo_id, pr_number) DO NOTHING",
                (repo_id, pr_number, thread_id),
            )
            db.execute(
                "UPDATE source_events SET thread_id = ? WHERE repo_id = ? AND "
                "subject_kind = 'pull_request' AND subject_number = ? "
                "AND thread_id IS NULL",
                (thread_id, repo_id, pr_number),
            )

    def source_event(self, event_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_events WHERE event_key = ?", (event_key,)
        ).fetchone()

    def issue_thread(self, thread_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM issue_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()

    def interaction_mode(self, thread_id: str) -> InteractionMode:
        thread = self.issue_thread(thread_id)
        if thread is None:
            raise ValueError("unknown IssueThread")
        return InteractionMode(thread["interaction_mode"])

    def thread_workflow_lifecycle(self, thread_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM thread_workflow_lifecycle_v1 WHERE thread_id=?",
            (thread_id,),
        ).fetchone()

    def activate_initial_workflow(
        self,
        *,
        thread_id: str,
        workflow_cycle_id: str,
        workflow_digest: str,
        now: str,
    ) -> None:
        with self.transaction(immediate=True) as db:
            changed = db.execute(
                """UPDATE thread_workflow_lifecycle_v1
                   SET initial_state='ACTIVE',initial_workflow_cycle_id=?,
                       initial_workflow_digest=?,updated_at=?
                   WHERE thread_id=? AND initial_state='NOT_STARTED'""",
                (workflow_cycle_id, workflow_digest, now, thread_id),
            )
            if changed.rowcount != 1:
                row = db.execute(
                    "SELECT * FROM thread_workflow_lifecycle_v1 WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if (
                    row is None
                    or row["initial_workflow_cycle_id"] != workflow_cycle_id
                    or row["initial_workflow_digest"] != workflow_digest
                    or row["initial_state"] not in {"ACTIVE", "COMPLETE", "PUBLISHED"}
                ):
                    raise ValueError("initial workflow may only be activated once")

    def complete_initial_workflow(
        self, *, thread_id: str, workflow_cycle_id: str, now: str
    ) -> None:
        with self.transaction(immediate=True) as db:
            changed = db.execute(
                """UPDATE thread_workflow_lifecycle_v1 SET initial_state='COMPLETE',
                   updated_at=? WHERE thread_id=? AND initial_state='ACTIVE'
                   AND initial_workflow_cycle_id=?""",
                (now, thread_id, workflow_cycle_id),
            )
            if changed.rowcount != 1:
                row = db.execute(
                    "SELECT * FROM thread_workflow_lifecycle_v1 WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if row is None or row["initial_state"] not in {"COMPLETE", "PUBLISHED"}:
                    raise ValueError("initial workflow completion identity mismatch")

    def route_unmatched_inputs(self, thread_id: str, *, now: str) -> list[str]:
        """Move non-response inputs to the durable revision inbox exactly once."""
        routed: list[str] = []
        with self.transaction(immediate=True) as db:
            rows = db.execute(
                """SELECT se.* FROM source_events AS se
                   LEFT JOIN thread_input_consumptions AS c
                     ON c.event_key=se.event_key
                   WHERE se.thread_id=? AND c.event_key IS NULL
                   ORDER BY se.source_updated_at,se.discovered_at,se.event_key""",
                (thread_id,),
            ).fetchall()
            for row in rows:
                disposition = self._classify_revision_input(
                    db,
                    event_key=row["event_key"],
                    thread_id=thread_id,
                    queued_at=row["discovered_at"] or now,
                )
                if disposition == "REVISION_QUEUED":
                    routed.append(row["event_key"])
        return routed

    def begin_feedback_review(
        self,
        *,
        event_key: str,
        task_run_id: str,
        feedback_kind: str,
        occurrence_key: str,
        feedback_text: str,
        now: str,
    ) -> FeedbackReviewRecord:
        """Bind one solicited feedback event to the exact open occurrence."""
        if feedback_kind not in {"PLAN", "RESULT"}:
            raise ValueError("feedback review kind is invalid")
        review_id = (
            "feedback-review-"
            + hashlib.sha256(
                f"{event_key}\0{feedback_kind}\0{occurrence_key}".encode()
            ).hexdigest()[:24]
        )
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM workflow_feedback_reviews_v1 WHERE source_event_key=?",
                (event_key,),
            ).fetchone()
            if existing is not None:
                if existing["feedback_review_id"] != review_id:
                    raise ValueError("feedback event is bound to another occurrence")
                return FeedbackReviewRecord(**dict(existing))
            task = db.execute(
                "SELECT * FROM workflow_task_runs_v1 WHERE task_run_id=?",
                (task_run_id,),
            ).fetchone()
            event = db.execute(
                "SELECT * FROM source_events WHERE event_key=? AND thread_id=?",
                (event_key, task["thread_id"] if task is not None else ""),
            ).fetchone()
            expected_phase = (
                "WAITING_FOR_PLAN_APPROVAL"
                if feedback_kind == "PLAN"
                else "WAITING_FOR_RESULT_APPROVAL"
            )
            if task is None or event is None or task["phase"] != expected_phase:
                raise ValueError("feedback occurrence is no longer active")
            cycle = db.execute(
                "SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?",
                (task["workflow_cycle_id"],),
            ).fetchone()
            root = (
                self._cycle_root_event_sql(db, cycle["root_input_id"])
                if cycle is not None
                else None
            )
            if root is None or not self._same_generic_target(event, root):
                raise ValueError("feedback does not target the active interaction")
            if feedback_kind == "PLAN":
                plan = db.execute(
                    "SELECT * FROM workflow_task_plans_v1 WHERE plan_id=?",
                    (task["current_plan_id"],),
                ).fetchone()
                exact = plan and plan["approval_occurrence_key"] == occurrence_key
            else:
                result = db.execute(
                    """SELECT * FROM workflow_task_results_v1
                       WHERE task_run_id=? ORDER BY created_at DESC LIMIT 1""",
                    (task_run_id,),
                ).fetchone()
                exact = result and result["result_occurrence_key"] == occurrence_key
            if not exact:
                raise ValueError("feedback approval occurrence is stale")
            if db.execute(
                "SELECT 1 FROM thread_input_consumptions WHERE event_key=?",
                (event_key,),
            ).fetchone():
                raise ValueError("feedback event was already consumed")
            db.execute(
                """INSERT INTO workflow_feedback_reviews_v1(
                   feedback_review_id,source_event_key,thread_id,
                   workflow_cycle_id,task_run_id,feedback_kind,occurrence_key,
                   feedback_text,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?, 'REVIEWING',?,?)""",
                (
                    review_id,
                    event_key,
                    task["thread_id"],
                    task["workflow_cycle_id"],
                    task_run_id,
                    feedback_kind,
                    occurrence_key,
                    feedback_text.strip()[:4_000],
                    now,
                    now,
                ),
            )
            self._record_disposition_sql(
                db,
                event_key=event_key,
                thread_id=task["thread_id"],
                cycle_id=task["cycle_id"],
                status=f"{feedback_kind}_FEEDBACK_REVIEW",
                recorded_at=now,
            )
        return self.feedback_review(review_id)  # type: ignore[return-value]

    def feedback_review(self, review_id: str) -> FeedbackReviewRecord | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_feedback_reviews_v1 WHERE feedback_review_id=?",
            (review_id,),
        ).fetchone()
        return FeedbackReviewRecord(**dict(row)) if row else None

    def feedback_review_for_task(
        self, task_run_id: str, *, statuses: tuple[str, ...] = ("REVIEWING",)
    ) -> FeedbackReviewRecord | None:
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"""SELECT * FROM workflow_feedback_reviews_v1
                WHERE task_run_id=? AND status IN ({placeholders})
                ORDER BY created_at DESC,feedback_review_id DESC LIMIT 2""",
            (task_run_id, *statuses),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple active feedback reviews are ambiguous")
        return FeedbackReviewRecord(**dict(rows[0])) if rows else None

    def defer_feedback_to_revision(
        self, feedback_review_id: str, *, now: str
    ) -> FeedbackReviewRecord:
        """Queue the review's exact event while preserving plan/result authority."""
        with self.transaction(immediate=True) as db:
            review = db.execute(
                "SELECT * FROM workflow_feedback_reviews_v1 WHERE feedback_review_id=?",
                (feedback_review_id,),
            ).fetchone()
            if review is None:
                raise ValueError("feedback review does not exist")
            if review["status"] == "DEFERRED_WAITING":
                return FeedbackReviewRecord(**dict(review))
            if review["status"] != "REVIEWING":
                raise ValueError("feedback review is no longer deferable")
            task = db.execute(
                "SELECT * FROM workflow_task_runs_v1 WHERE task_run_id=?",
                (review["task_run_id"],),
            ).fetchone()
            expected_phase = (
                "WAITING_FOR_PLAN_APPROVAL"
                if review["feedback_kind"] == "PLAN"
                else "WAITING_FOR_RESULT_APPROVAL"
            )
            if task is None or task["phase"] != expected_phase:
                raise ValueError("feedback review became stale")
            if review["feedback_kind"] == "PLAN":
                exact = db.execute(
                    """SELECT 1 FROM workflow_task_plans_v1
                       WHERE plan_id=? AND approval_occurrence_key=?""",
                    (task["current_plan_id"], review["occurrence_key"]),
                ).fetchone()
                message_kind = "PLAN_FEEDBACK_DEFERRED"
                message = PLAN_DEFERRED_FEEDBACK_MESSAGE
            else:
                exact = db.execute(
                    """SELECT 1 FROM workflow_task_results_v1
                       WHERE task_run_id=? AND result_occurrence_key=?""",
                    (task["task_run_id"], review["occurrence_key"]),
                ).fetchone()
                message_kind = "RESULT_FEEDBACK_DEFERRED"
                message = RESULT_DEFERRED_FEEDBACK_MESSAGE
            if exact is None:
                raise ValueError("feedback approval occurrence became stale")
            revision_input_id = self.revision_input_id_for(review["source_event_key"])
            db.execute(
                """INSERT OR IGNORE INTO revision_inputs_v1(
                   revision_input_id,source_event_key,thread_id,residual_text,
                   classification_reason,status,queued_at)
                   VALUES(?,?,?,NULL,?,'PENDING',?)""",
                (
                    revision_input_id,
                    review["source_event_key"],
                    review["thread_id"],
                    f"DEFERRED_{review['feedback_kind']}_FEEDBACK",
                    now,
                ),
            )
            marker = deferred_feedback_marker(feedback_review_id)
            self._enqueue_workflow_comment_sql(
                db,
                source_event_key=review["source_event_key"],
                thread_id=review["thread_id"],
                message_kind=message_kind,
                marker=marker,
                body=f"{marker}\n{message}",
                now=now,
            )
            db.execute(
                """UPDATE workflow_feedback_reviews_v1
                   SET status='DEFERRED_WAITING',updated_at=?
                   WHERE feedback_review_id=? AND status='REVIEWING'""",
                (now, feedback_review_id),
            )
        return self.feedback_review(feedback_review_id)  # type: ignore[return-value]

    def pending_workflow_comments(
        self, thread_id: str, *, due_at: str | None = None
    ) -> list[WorkflowCommentOutboxRecord]:
        if due_at is None:
            rows = self.connection.execute(
                """SELECT * FROM workflow_comment_outbox_v1
                   WHERE thread_id=? AND status='PENDING'
                   ORDER BY created_at,outbox_id""",
                (thread_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT * FROM workflow_comment_outbox_v1
                   WHERE thread_id=? AND status='PENDING' AND next_attempt_at<=?
                   ORDER BY created_at,outbox_id""",
                (thread_id, due_at),
            ).fetchall()
        return [WorkflowCommentOutboxRecord(**dict(row)) for row in rows]

    def update_workflow_comment(
        self,
        outbox_id: str,
        *,
        status: str,
        now: str,
        comment_id: int | None = None,
        error_message: str | None = None,
        next_attempt_at: str | None = None,
    ) -> WorkflowCommentOutboxRecord:
        if status not in {"PENDING", "DELIVERED", "AMBIGUOUS"}:
            raise ValueError("workflow comment status is invalid")
        with self.transaction(immediate=True) as db:
            changed = db.execute(
                """UPDATE workflow_comment_outbox_v1
                   SET status=?,comment_id=?,error_message=?,updated_at=?,
                       next_attempt_at=COALESCE(?,next_attempt_at),
                       delivered_at=CASE WHEN ?='DELIVERED' THEN ? ELSE delivered_at END
                   WHERE outbox_id=?""",
                (
                    status,
                    comment_id,
                    error_message,
                    now,
                    next_attempt_at,
                    status,
                    now,
                    outbox_id,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("workflow comment does not exist")
        row = self.connection.execute(
            "SELECT * FROM workflow_comment_outbox_v1 WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        return WorkflowCommentOutboxRecord(**dict(row))

    def pending_revision_inputs(self, thread_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT r.*,se.repo_full_name,se.source_kind,se.source_id,
                      se.source_updated_at,se.source_created_at,se.subject_kind,
                      se.subject_number,se.author_login,se.body AS source_body,
                      se.html_url,se.origin_surface,se.path,se.line,se.start_line,
                      se.side,se.start_side,se.diff_hunk,se.commit_id,
                      se.original_commit_id,se.in_reply_to_id,
                      se.pull_request_review_id,se.review_thread_root_id
               FROM revision_inputs_v1 AS r
               JOIN source_events AS se ON se.event_key=r.source_event_key
               WHERE r.thread_id=? AND r.status='PENDING'
               ORDER BY se.source_updated_at,r.queued_at,r.revision_input_id""",
            (thread_id,),
        ).fetchall()

    def revision_inputs_for_cycle(
        self, workflow_cycle_id: str, *, include_consumed: bool = True
    ) -> list[sqlite3.Row]:
        status = "" if include_consumed else " AND r.status='BATCHED'"
        return self.connection.execute(
            """SELECT r.*,se.repo_full_name,se.source_kind,se.source_id,
                      se.source_updated_at,se.source_created_at,se.subject_kind,
                      se.subject_number,se.author_login,se.body AS source_body,
                      se.html_url,se.origin_surface,se.path,se.line,se.start_line,
                      se.side,se.start_side,se.diff_hunk,se.commit_id,
                      se.original_commit_id,se.in_reply_to_id,
                      se.pull_request_review_id,se.review_thread_root_id
               FROM revision_inputs_v1 AS r
               JOIN source_events AS se ON se.event_key=r.source_event_key
               WHERE r.revision_workflow_cycle_id=?"""
            + status
            + " ORDER BY se.source_updated_at,r.queued_at,r.revision_input_id",
            (workflow_cycle_id,),
        ).fetchall()

    def revision_input(self, revision_input_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT r.*,se.* FROM revision_inputs_v1 AS r
               JOIN source_events AS se ON se.event_key=r.source_event_key
               WHERE r.revision_input_id=?""",
            (revision_input_id,),
        ).fetchone()

    def batch_revision_inputs(
        self, *, thread_id: str, workflow_cycle_id: str, now: str
    ) -> list[sqlite3.Row]:
        with self.transaction(immediate=True) as db:
            cycle = db.execute(
                """SELECT * FROM workflow_cycles_v1 WHERE workflow_cycle_id=?
                   AND thread_id=? AND cycle_kind='REVISION' AND status='ACTIVE'""",
                (workflow_cycle_id, thread_id),
            ).fetchone()
            if cycle is None:
                raise ValueError("revision input batch target is not active")
            db.execute(
                """UPDATE revision_inputs_v1 SET status='BATCHED',
                   revision_workflow_cycle_id=?,batched_at=?
                   WHERE thread_id=? AND status='PENDING'""",
                (workflow_cycle_id, now, thread_id),
            )
            db.execute(
                """UPDATE deferred_followups SET status='CONSUMED',
                   consumed_cycle_id=?,consumed_at=?
                   WHERE thread_id=? AND status='QUEUED' AND EXISTS(
                     SELECT 1 FROM revision_inputs_v1 AS r
                     WHERE r.revision_workflow_cycle_id=?
                       AND r.source_event_key=deferred_followups.source_event_key
                       AND COALESCE(r.residual_text,'')=
                           COALESCE(deferred_followups.residual_text,''))""",
                (cycle["cycle_id"], now, thread_id, workflow_cycle_id),
            )
        return self.revision_inputs_for_cycle(workflow_cycle_id)

    def consume_revision_inputs(
        self, *, thread_id: str, workflow_cycle_id: str, now: str
    ) -> int:
        with self.transaction(immediate=True) as db:
            changed = db.execute(
                """UPDATE revision_inputs_v1 SET status='CONSUMED',consumed_at=?
                   WHERE thread_id=? AND revision_workflow_cycle_id=?
                     AND status='BATCHED'""",
                (now, thread_id, workflow_cycle_id),
            )
            return changed.rowcount

    def source_events_for_thread(self, thread_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_events WHERE thread_id = ? "
            "ORDER BY source_updated_at, discovered_at, event_key",
            (thread_id,),
        ).fetchall()

    def workflow_state(self, thread_id: str) -> WorkflowStateRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_workflow_state WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._workflow_state_record(row) if row else None

    # ------------------------------------------------------------------
    # Repository-memory learning identity
    #
    # Learning belongs to one completed lifecycle.  A record is only ever
    # written through its own `learning_id`, so an unfinished record from an
    # older cycle can never overwrite or block a newer cycle's learning.
    # ------------------------------------------------------------------

    def memory_learning_for_id(
        self, learning_id: str
    ) -> RepoMemoryLearningRecord | None:
        row = self.connection.execute(
            "SELECT * FROM repo_memory_learning WHERE learning_id = ?", (learning_id,)
        ).fetchone()
        return RepoMemoryLearningRecord(**dict(row)) if row else None

    def memory_learning_for_cycle(
        self,
        *,
        thread_id: str,
        cycle_id: int,
        root_event_key: str,
        root_input_id: str | None,
    ) -> RepoMemoryLearningRecord | None:
        return self.memory_learning_for_id(
            memory_learning_id_for(
                thread_id=thread_id,
                cycle_id=cycle_id,
                root_input_id=root_input_id or root_event_key,
            )
        )

    def memory_learnings_for_event(
        self, event_key: str
    ) -> list[RepoMemoryLearningRecord]:
        """Diagnostic: every learning lifecycle that retains this SourceEvent."""
        rows = self.connection.execute(
            """SELECT * FROM repo_memory_learning WHERE source_event_key = ?
               ORDER BY cycle_id, learning_id""",
            (event_key,),
        ).fetchall()
        return [RepoMemoryLearningRecord(**dict(row)) for row in rows]

    def repo_memory_learning(self, event_key: str) -> RepoMemoryLearningRecord | None:
        """Diagnostic lookup; fails closed when a SourceEvent is ambiguous."""
        records = self.memory_learnings_for_event(event_key)
        if len(records) > 1:
            raise AmbiguousLifecycleError(
                f"SourceEvent {event_key} backs {len(records)} learning "
                "lifecycles; use memory_learning_for_cycle or _for_id"
            )
        return records[0] if records else None

    def pending_memory_learning(
        self, thread_id: str
    ) -> RepoMemoryLearningRecord | None:
        """Oldest unfinished learning lifecycle for a thread, if any."""
        row = self.connection.execute(
            """SELECT * FROM repo_memory_learning
               WHERE thread_id = ? AND status IN ('PENDING', 'FAILED')
                 AND attempt_count < ?
               ORDER BY cycle_id, created_at, learning_id LIMIT 1""",
            (thread_id, MAX_MEMORY_LEARNING_ATTEMPTS),
        ).fetchone()
        return RepoMemoryLearningRecord(**dict(row)) if row else None

    def save_repo_memory_learning(
        self, record: RepoMemoryLearningRecord
    ) -> RepoMemoryLearningRecord:
        expected = memory_learning_id_for(
            thread_id=record.thread_id,
            cycle_id=record.cycle_id,
            root_input_id=record.root_input_id,
        )
        if record.learning_id != expected:
            raise ValueError("memory learning id does not match its lifecycle")
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO repo_memory_learning(
                   learning_id, source_event_key, thread_id, cycle_id,
                   root_input_id, repo_id, status, accepted_candidates,
                   rejected_candidates, error_message, proposal_json,
                   attempt_count, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(learning_id) DO UPDATE SET status=excluded.status,
                   accepted_candidates=excluded.accepted_candidates,
                   rejected_candidates=excluded.rejected_candidates,
                   error_message=excluded.error_message,
                   proposal_json=excluded.proposal_json,
                   attempt_count=excluded.attempt_count,
                   updated_at=excluded.updated_at""",
                (
                    record.learning_id,
                    record.source_event_key,
                    record.thread_id,
                    record.cycle_id,
                    record.root_input_id,
                    record.repo_id,
                    record.status,
                    record.accepted_candidates,
                    record.rejected_candidates,
                    record.error_message,
                    record.proposal_json,
                    record.attempt_count,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return self.memory_learning_for_id(record.learning_id)  # type: ignore[return-value]

    def save_workflow_state(self, record: WorkflowStateRecord) -> WorkflowStateRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO issue_workflow_state(
                   thread_id, repo_id, repo_full_name, issue_number, phase,
                   cycle_id, root_event_key, current_plan_id, mode, created_at,
                   root_input_id,
                   updated_at, response_surface, response_subject_number,
                   response_comment_id, response_url, review_thread_root_id,
                   planning_feedback_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET phase=excluded.phase,
                   cycle_id=excluded.cycle_id, root_event_key=excluded.root_event_key,
                   root_input_id=excluded.root_input_id,
                   current_plan_id=excluded.current_plan_id, mode=excluded.mode,
                   updated_at=excluded.updated_at,
                   response_surface=excluded.response_surface,
                   response_subject_number=excluded.response_subject_number,
                   response_comment_id=excluded.response_comment_id,
                   response_url=excluded.response_url,
                   review_thread_root_id=excluded.review_thread_root_id,
                   planning_feedback_event_key=excluded.planning_feedback_event_key""",
                (
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.phase.value,
                    record.cycle_id,
                    record.root_event_key,
                    record.current_plan_id,
                    record.mode.value,
                    record.created_at,
                    record.root_input_id,
                    record.updated_at,
                    record.response_surface,
                    record.response_subject_number,
                    record.response_comment_id,
                    record.response_url,
                    record.review_thread_root_id,
                    record.planning_feedback_event_key,
                ),
            )
        return self.workflow_state(record.thread_id)  # type: ignore[return-value]

    def begin_workflow_cycle(
        self, plan: PlanRecord, state: WorkflowStateRecord, *, claimed_at: str
    ) -> PlanRecord:
        """Atomically consume the root and create its plan/state."""
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (state.thread_id,),
            ).fetchone()
            if existing is not None and existing["phase"] != WorkflowPhase.IDLE.value:
                raise ValueError("IssueThread already has an active workflow")
            if existing is not None and state.cycle_id != existing["cycle_id"] + 1:
                raise ValueError("workflow cycle must advance exactly one step")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    plan.root_event_key,
                    plan.thread_id,
                    plan.cycle_id,
                    InputPurpose.CYCLE_ROOT.value,
                    "CONSUMED",
                    claimed_at,
                    claimed_at,
                ),
            )
            if plan.root_input_id and plan.root_input_id.startswith("deferred-"):
                db.execute(
                    """UPDATE deferred_followups SET status='CONSUMED',
                       consumed_cycle_id=?, consumed_at=?
                       WHERE deferred_id=? AND source_event_key=? AND status='QUEUED'""",
                    (
                        plan.cycle_id,
                        claimed_at,
                        plan.root_input_id,
                        plan.root_event_key,
                    ),
                )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, root_input_id, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.root_input_id,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                """INSERT INTO issue_workflow_state(
                   thread_id, repo_id, repo_full_name, issue_number, phase,
                   cycle_id, root_event_key, root_input_id, current_plan_id, mode, created_at,
                   updated_at, response_surface, response_subject_number,
                   response_comment_id, response_url, review_thread_root_id,
                   planning_feedback_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET
                   repo_id=excluded.repo_id, repo_full_name=excluded.repo_full_name,
                   issue_number=excluded.issue_number, phase=excluded.phase,
                   cycle_id=excluded.cycle_id, root_event_key=excluded.root_event_key,
                   root_input_id=excluded.root_input_id,
                   current_plan_id=excluded.current_plan_id, mode=excluded.mode,
                   updated_at=excluded.updated_at,
                   response_surface=excluded.response_surface,
                   response_subject_number=excluded.response_subject_number,
                   response_comment_id=excluded.response_comment_id,
                   response_url=excluded.response_url,
                   review_thread_root_id=excluded.review_thread_root_id,
                   planning_feedback_event_key=excluded.planning_feedback_event_key""",
                (
                    state.thread_id,
                    state.repo_id,
                    state.repo_full_name,
                    state.issue_number,
                    state.phase.value,
                    state.cycle_id,
                    state.root_event_key,
                    state.root_input_id,
                    state.current_plan_id,
                    state.mode.value,
                    state.created_at,
                    state.updated_at,
                    state.response_surface,
                    state.response_subject_number,
                    state.response_comment_id,
                    state.response_url,
                    state.review_thread_root_id,
                    state.planning_feedback_event_key,
                ),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def approve_current_plan(
        self,
        *,
        event_key: str,
        author_login: str | None,
        permit: ExecutionPermit,
        approved_at: str,
    ) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (permit.thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit.plan_id,)
            ).fetchone()
            event = db.execute(
                "SELECT thread_id FROM source_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            if (
                state is None
                or plan is None
                or event is None
                or event["thread_id"] != permit.thread_id
                or state["phase"] != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value
                or state["current_plan_id"] != permit.plan_id
                or plan["status"] != PlanStatus.POSTED.value
            ):
                raise ValueError("current plan cannot be approved")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_key,
                    permit.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_APPROVAL.value,
                    "CONSUMED",
                    approved_at,
                    approved_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=event_key,
                thread_id=permit.thread_id,
                claimed_at=approved_at,
                purpose=InputPurpose.PLAN_APPROVAL,
            )
            db.execute(
                """UPDATE issue_plans SET status = ?, approved_at = ?,
                   approved_by = ?, approval_event_key = ? WHERE plan_id = ?""",
                (
                    PlanStatus.APPROVED.value,
                    approved_at,
                    author_login,
                    event_key,
                    permit.plan_id,
                ),
            )
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source, source_event_key, created_at,
                   consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    permit.permit_id,
                    permit.thread_id,
                    permit.cycle_id,
                    permit.plan_id,
                    permit.plan_version,
                    permit.root_event_key,
                    permit.source.value,
                    permit.source_event_key,
                    permit.created_at,
                    None,
                    None,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTION_READY.value, approved_at, permit.thread_id),
            )
        return self.permit(permit.permit_id)  # type: ignore[return-value]

    def mark_current_plan_posted(
        self,
        plan_id: str,
        *,
        comment_id: int,
        posted_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT p.*, w.phase AS workflow_phase, w.current_plan_id
                   FROM issue_plans p JOIN issue_workflow_state w
                   ON w.thread_id = p.thread_id WHERE p.plan_id = ?""",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown plan")
            if row["current_plan_id"] != plan_id:
                raise ValueError("plan is no longer current")
            if row["workflow_phase"] == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value:
                if row["status"] != PlanStatus.POSTED.value:
                    raise ValueError("posted workflow has inconsistent plan status")
                return self.plan(plan_id)  # type: ignore[return-value]
            if (
                row["workflow_phase"] == WorkflowPhase.PLANNING.value
                and row["status"] == PlanStatus.POSTED.value
            ):
                db.execute(
                    "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                    "WHERE thread_id = ?",
                    (
                        WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                        posted_at,
                        row["thread_id"],
                    ),
                )
                return self.plan(plan_id)  # type: ignore[return-value]
            if (
                row["workflow_phase"] != WorkflowPhase.PLANNING.value
                or row["status"] != PlanStatus.DRAFT.value
            ):
                raise ValueError("plan is not ready for publication")
            db.execute(
                "UPDATE issue_plans SET status = ?, posted_at = ?, "
                "posted_comment_id = ? WHERE plan_id = ?",
                (PlanStatus.POSTED.value, posted_at, comment_id, plan_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    posted_at,
                    row["thread_id"],
                ),
            )
        return self.plan(plan_id)  # type: ignore[return-value]

    def begin_plan_revision(self, event_key: str, *, now: str) -> WorkflowStateRecord:
        with self.transaction(immediate=True) as db:
            event = db.execute(
                "SELECT thread_id FROM source_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            if event is None:
                raise ValueError("unknown workflow input")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (event["thread_id"],),
            ).fetchone()
            if state is None or (
                state["phase"]
                not in (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    WorkflowPhase.EXECUTION_READY.value,
                )
                and not (
                    state["phase"] == WorkflowPhase.PLANNING.value
                    and state["planning_feedback_event_key"] == event_key
                )
            ):
                raise ValueError("feedback is not currently accepted for planning")
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, event["thread_id"], state["cycle_id"]),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, "
                "planning_feedback_event_key = ?, updated_at = ? WHERE thread_id = ?",
                (
                    WorkflowPhase.PLANNING.value,
                    event_key,
                    now,
                    event["thread_id"],
                ),
            )
        return self.workflow_state(event["thread_id"])  # type: ignore[return-value]

    def finish_plan_revision(
        self,
        *,
        plan: PlanRecord,
        feedback_event_key: str,
        finished_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (plan.thread_id,),
            ).fetchone()
            current = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?",
                (state["current_plan_id"],) if state else (None,),
            ).fetchone()
            if (
                state is None
                or current is None
                or state["phase"] != WorkflowPhase.PLANNING.value
                or state["planning_feedback_event_key"] != feedback_event_key
            ):
                raise ValueError("revision is not the active planning operation")
            db.execute(
                "UPDATE issue_plans SET status = ? WHERE plan_id = ?",
                (PlanStatus.SUPERSEDED.value, current["plan_id"]),
            )
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    feedback_event_key,
                    plan.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_FEEDBACK.value,
                    "CONSUMED",
                    finished_at,
                    finished_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=feedback_event_key,
                thread_id=plan.thread_id,
                claimed_at=finished_at,
                purpose=InputPurpose.PLAN_FEEDBACK,
            )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, root_input_id, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.root_input_id,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET planning_feedback_event_key = NULL, "
                "current_plan_id = ?, updated_at = ? WHERE thread_id = ?",
                (plan.plan_id, finished_at, plan.thread_id),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def revise_current_plan(
        self,
        *,
        event_key: str,
        plan: PlanRecord,
        revised_at: str,
    ) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (plan.thread_id,),
            ).fetchone()
            current = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?",
                (state["current_plan_id"],) if state else (None,),
            ).fetchone()
            if (
                state is None
                or current is None
                or state["phase"]
                not in (
                    WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value,
                    WorkflowPhase.EXECUTION_READY.value,
                )
            ):
                raise ValueError("current plan cannot be revised")
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_key,
                    plan.thread_id,
                    state["cycle_id"],
                    InputPurpose.PLAN_FEEDBACK.value,
                    "CONSUMED",
                    revised_at,
                    revised_at,
                ),
            )
            self._resolve_workflow_control(
                db,
                event_key=event_key,
                thread_id=plan.thread_id,
                claimed_at=revised_at,
                purpose=InputPurpose.PLAN_FEEDBACK,
            )
            db.execute(
                "UPDATE issue_plans SET status = ? WHERE plan_id = ?",
                (PlanStatus.SUPERSEDED.value, current["plan_id"]),
            )
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (revised_at, plan.thread_id, state["cycle_id"]),
            )
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, root_input_id, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.thread_id,
                    plan.repo_id,
                    plan.repo_full_name,
                    plan.issue_number,
                    plan.cycle_id,
                    plan.version,
                    plan.root_event_key,
                    plan.root_input_id,
                    plan.plan_text,
                    plan.status.value,
                    plan.created_at,
                    plan.posted_at,
                    plan.posted_comment_id,
                    plan.approved_at,
                    plan.approved_by,
                    plan.approval_event_key,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, current_plan_id = ?, "
                "updated_at = ? WHERE thread_id = ?",
                (
                    WorkflowPhase.PLANNING.value,
                    plan.plan_id,
                    revised_at,
                    plan.thread_id,
                ),
            )
        return self.plan(plan.plan_id)  # type: ignore[return-value]

    def authorize_current_plan(
        self,
        *,
        permit: ExecutionPermit,
        authorized_at: str,
    ) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (permit.thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit.plan_id,)
            ).fetchone()
            if (
                state is None
                or plan is None
                or state["phase"] != WorkflowPhase.WAITING_FOR_PLAN_APPROVAL.value
                or state["current_plan_id"] != permit.plan_id
                or plan["status"] != PlanStatus.POSTED.value
            ):
                raise ValueError("current plan cannot be authorized")
            db.execute(
                "UPDATE issue_plans SET status = ?, approved_at = ? WHERE plan_id = ?",
                (PlanStatus.AUTO_APPROVED.value, authorized_at, permit.plan_id),
            )
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source, source_event_key, created_at,
                   consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    permit.permit_id,
                    permit.thread_id,
                    permit.cycle_id,
                    permit.plan_id,
                    permit.plan_version,
                    permit.root_event_key,
                    permit.source.value,
                    permit.source_event_key,
                    permit.created_at,
                    None,
                    None,
                ),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTION_READY.value, authorized_at, permit.thread_id),
            )
        return self.permit(permit.permit_id)  # type: ignore[return-value]

    @staticmethod
    def _workflow_state_record(row: sqlite3.Row) -> WorkflowStateRecord:
        values = dict(row)
        values["phase"] = WorkflowPhase(values["phase"])
        values["mode"] = WorkflowMode(values["mode"])
        return WorkflowStateRecord(**values)

    def plans_for_thread(self, thread_id: str) -> list[PlanRecord]:
        rows = self.connection.execute(
            "SELECT * FROM issue_plans WHERE thread_id = ? ORDER BY cycle_id, version",
            (thread_id,),
        ).fetchall()
        return [self._plan_record(row) for row in rows]

    def plan(self, plan_id: str) -> PlanRecord | None:
        row = self.connection.execute(
            "SELECT * FROM issue_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return self._plan_record(row) if row else None

    def plan_for_cycle(self, thread_id: str, cycle_id: int) -> PlanRecord | None:
        """Latest plan version of one exact cycle, independent of the current one."""
        row = self.connection.execute(
            """SELECT * FROM issue_plans WHERE thread_id = ? AND cycle_id = ?
               ORDER BY version DESC LIMIT 1""",
            (thread_id, cycle_id),
        ).fetchone()
        return self._plan_record(row) if row else None

    def current_plan(self, thread_id: str) -> PlanRecord | None:
        state = self.workflow_state(thread_id)
        return (
            self.plan(state.current_plan_id)
            if state and state.current_plan_id
            else None
        )

    def insert_plan(self, record: PlanRecord) -> PlanRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO issue_plans(
                   plan_id, thread_id, repo_id, repo_full_name, issue_number,
                   cycle_id, version, root_event_key, root_input_id, plan_text, status,
                   created_at, posted_at, posted_comment_id, approved_at,
                   approved_by, approval_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.plan_id,
                    record.thread_id,
                    record.repo_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.cycle_id,
                    record.version,
                    record.root_event_key,
                    record.root_input_id,
                    record.plan_text,
                    record.status.value,
                    record.created_at,
                    record.posted_at,
                    record.posted_comment_id,
                    record.approved_at,
                    record.approved_by,
                    record.approval_event_key,
                ),
            )
        return self.plan(record.plan_id)  # type: ignore[return-value]

    def update_plan(self, plan_id: str, **fields: object) -> PlanRecord:
        allowed = {
            "plan_text",
            "status",
            "posted_at",
            "posted_comment_id",
            "approved_at",
            "approved_by",
            "approval_event_key",
        }
        if set(fields) - allowed:
            raise ValueError(f"unknown plan fields: {sorted(set(fields) - allowed)}")
        assignments: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value.value if isinstance(value, StrEnum) else value)
        if not assignments:
            return self.plan(plan_id)  # type: ignore[return-value]
        values.append(plan_id)
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    f"UPDATE issue_plans SET {', '.join(assignments)} "
                    "WHERE plan_id = ?",
                    values,
                ).rowcount
                != 1
            ):
                raise ValueError("unknown plan")
        return self.plan(plan_id)  # type: ignore[return-value]

    def permit(self, permit_id: str) -> ExecutionPermit | None:
        row = self.connection.execute(
            "SELECT * FROM execution_permits WHERE permit_id = ?", (permit_id,)
        ).fetchone()
        return self._permit_record(row) if row else None

    def permit_for_plan(self, plan_id: str) -> ExecutionPermit | None:
        row = self.connection.execute(
            "SELECT * FROM execution_permits WHERE plan_id = ? "
            "AND invalidated_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return self._permit_record(row) if row else None

    def insert_permit(self, record: ExecutionPermit) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO execution_permits(
                   permit_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, source,
                   source_event_key, created_at, consumed_at, invalidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.permit_id,
                    record.thread_id,
                    record.cycle_id,
                    record.plan_id,
                    record.plan_version,
                    record.root_event_key,
                    record.source.value,
                    record.source_event_key,
                    record.created_at,
                    record.consumed_at,
                    record.invalidated_at,
                ),
            )
        return self.permit(record.permit_id)  # type: ignore[return-value]

    def consume_permit(self, permit_id: str, *, consumed_at: str) -> ExecutionPermit:
        with self.transaction(immediate=True) as db:
            if (
                db.execute(
                    "UPDATE execution_permits SET consumed_at = ? "
                    "WHERE permit_id = ? AND consumed_at IS NULL "
                    "AND invalidated_at IS NULL",
                    (consumed_at, permit_id),
                ).rowcount
                != 1
            ):
                raise ValueError("permit is missing, consumed, or invalidated")
        return self.permit(permit_id)  # type: ignore[return-value]

    def bind_authorized_execution(
        self,
        permit_id: str,
        *,
        expected_thread_id: str,
        now: str,
    ) -> ClaimedEvent:
        """Atomically validate a permit, claim its exact root, and enter EXECUTING."""
        with self.transaction(immediate=True) as db:
            permit = db.execute(
                "SELECT * FROM execution_permits WHERE permit_id = ?", (permit_id,)
            ).fetchone()
            if permit is None or permit["invalidated_at"]:
                raise ValueError("execution permit is unavailable")
            if permit["thread_id"] != expected_thread_id:
                raise ValueError("execution permit belongs to another thread")
            state = db.execute(
                "SELECT * FROM issue_workflow_state WHERE thread_id = ?",
                (expected_thread_id,),
            ).fetchone()
            plan = db.execute(
                "SELECT * FROM issue_plans WHERE plan_id = ?", (permit["plan_id"],)
            ).fetchone()
            if state is None or plan is None:
                raise ValueError("execution permit references missing workflow data")
            if (
                state["phase"] != WorkflowPhase.EXECUTION_READY.value
                or state["cycle_id"] != permit["cycle_id"]
                or state["current_plan_id"] != permit["plan_id"]
                or plan["version"] != permit["plan_version"]
                or plan["status"]
                not in (PlanStatus.APPROVED.value, PlanStatus.AUTO_APPROVED.value)
                or plan["root_event_key"] != permit["root_event_key"]
            ):
                raise ValueError("execution permit is stale")
            row = db.execute(
                """SELECT se.*, thread.issue_number,
                          ee.status AS execution_status
                   FROM source_events se
                   JOIN issue_threads thread ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions ee ON ee.event_key = se.event_key
                   WHERE se.event_key = ?""",
                (permit["root_event_key"],),
            ).fetchone()
            if row is None or row["thread_id"] != expected_thread_id:
                raise ValueError("permit root event does not match the thread")
            root_input_id = plan["root_input_id"] or plan["root_event_key"]
            logical_execution_id = execution_id_for(
                thread_id=expected_thread_id,
                cycle_id=permit["cycle_id"],
                root_input_id=root_input_id,
            )
            logical = root_input_id != permit["root_event_key"]
            pending = db.execute(
                """SELECT event_key, body FROM source_events
                   WHERE thread_id = ? AND event_key != ?
                   AND event_key NOT IN (
                     SELECT event_key FROM thread_input_consumptions
                   ) ORDER BY source_updated_at, discovered_at, event_key""",
                (expected_thread_id, permit["root_event_key"]),
            ).fetchall()
            for candidate in pending:
                if is_exact_agent_approval(candidate["body"]):
                    db.execute(
                        """INSERT INTO thread_input_consumptions(
                           event_key, thread_id, cycle_id, purpose, status,
                           claimed_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            candidate["event_key"],
                            expected_thread_id,
                            state["cycle_id"],
                            InputPurpose.STALE_PLAN_APPROVAL.value,
                            "CONSUMED",
                            now,
                            now,
                        ),
                    )
                    self._resolve_workflow_control(
                        db,
                        event_key=candidate["event_key"],
                        thread_id=expected_thread_id,
                        claimed_at=now,
                        purpose=InputPurpose.STALE_PLAN_APPROVAL,
                    )
                    continue
                if starts_with_agent_invocation(candidate["body"]):
                    raise PendingWorkflowInputError(candidate["event_key"])
            if not logical and row["execution_status"] not in (
                None,
                ExecutionStatus.RETRY_PENDING.value,
            ):
                raise ValueError("permit root event is not claimable")
            if (
                permit["consumed_at"]
                and row["execution_status"] != ExecutionStatus.RETRY_PENDING.value
            ):
                raise ValueError("execution permit was already consumed")
            self._assert_event_ordering(db, row)
            logical_row = db.execute(
                "SELECT status FROM logical_executions WHERE execution_id=?",
                (logical_execution_id,),
            ).fetchone()
            retrying = logical_row is not None or (
                not logical
                and row["execution_status"] == ExecutionStatus.RETRY_PENDING.value
            )
            if logical:
                if logical_row and logical_row["status"] not in (
                    ExecutionStatus.RETRY_PENDING.value,
                    ExecutionStatus.INTERRUPTED.value,
                ):
                    raise ValueError("logical execution is not claimable")
                if logical_row:
                    db.execute(
                        """UPDATE logical_executions SET status=?, attempt_count=attempt_count+1,
                           started_at=?, completed_at=NULL, error_message=NULL
                           WHERE execution_id=?""",
                        (ExecutionStatus.RUNNING.value, now, logical_execution_id),
                    )
                    retrying = True
                else:
                    db.execute(
                        """INSERT INTO logical_executions(
                           execution_id, source_event_key, thread_id, cycle_id,
                           root_input_id, status, attempt_count, started_at)
                           VALUES(?,?,?,?,?,?,1,?)""",
                        (
                            logical_execution_id,
                            permit["root_event_key"],
                            expected_thread_id,
                            permit["cycle_id"],
                            root_input_id,
                            ExecutionStatus.RUNNING.value,
                            now,
                        ),
                    )
                    retrying = False
            elif retrying:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, row["event_key"]),
                )
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (
                        row["event_key"],
                        expected_thread_id,
                        ExecutionStatus.RUNNING.value,
                        now,
                    ),
                )
            db.execute(
                "UPDATE execution_permits SET consumed_at = ? "
                "WHERE permit_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, permit_id),
            )
            db.execute(
                "UPDATE issue_workflow_state SET phase = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (WorkflowPhase.EXECUTING.value, now, expected_thread_id),
            )
            db.execute(
                """INSERT OR IGNORE INTO execution_attempts(
                   attempt_id, thread_id, cycle_id, plan_id, plan_version,
                   root_event_key, attempt_number, kind, repair_round,
                   authorization_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"attempt-{permit_id}",
                    expected_thread_id,
                    permit["cycle_id"],
                    permit["plan_id"],
                    permit["plan_version"],
                    permit["root_event_key"],
                    1,
                    AttemptKind.INITIAL.value,
                    0,
                    permit_id,
                    AttemptStatus.RUNNING.value,
                    now,
                ),
            )
            return self._claimed_event(
                row,
                retrying=retrying,
                execution_id=logical_execution_id if logical else row["event_key"],
            )

    def invalidate_permits(self, thread_id: str, cycle_id: int, *, now: str) -> None:
        with self.transaction(immediate=True) as db:
            db.execute(
                "UPDATE execution_permits SET invalidated_at = ? "
                "WHERE thread_id = ? AND cycle_id = ? AND consumed_at IS NULL "
                "AND invalidated_at IS NULL",
                (now, thread_id, cycle_id),
            )

    def input_consumption(self, event_key: str) -> WorkflowInputRecord | None:
        row = self.connection.execute(
            "SELECT * FROM thread_input_consumptions WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return self._input_record(row) if row else None

    def clarification_for_thread(
        self, thread_id: str, *, status: str = ClarificationStatus.OPEN.value
    ) -> ClarificationRequestRecord | None:
        ordering = "DESC" if status == ClarificationStatus.ANSWERED.value else "ASC"
        row = self.connection.execute(
            "SELECT * FROM clarification_requests WHERE thread_id = ? AND status = ? "
            f"ORDER BY created_at {ordering}, clarification_id {ordering} LIMIT 1",
            (thread_id, status),
        ).fetchone()
        return ClarificationRequestRecord(**dict(row)) if row else None

    def answered_clarification_for_occurrence(
        self, *, thread_id: str, cycle_id: int, occurrence_key: str
    ) -> ClarificationRequestRecord | None:
        """Return the answer for one exact interrupt occurrence.

        Clarification identity is (thread, cycle, occurrence_key), so this is
        the only lookup that may drive a checkpoint resume.  Selecting by
        thread/cycle alone would hand one interrupt another interrupt's answer.
        """
        if not occurrence_key:
            return None
        row = self.connection.execute(
            """SELECT * FROM clarification_requests
               WHERE thread_id = ? AND cycle_id = ? AND occurrence_key = ?
                 AND status = ?""",
            (thread_id, cycle_id, occurrence_key, ClarificationStatus.ANSWERED.value),
        ).fetchone()
        return ClarificationRequestRecord(**dict(row)) if row else None

    def sole_answered_legacy_clarification(
        self, *, thread_id: str, cycle_id: int
    ) -> ClarificationRequestRecord | None:
        """Pre-occurrence-key compatibility for one in-flight legacy answer.

        Rows written before occurrence keys existed carry an empty key and can
        never match a live interrupt.  They are only resumable while exactly
        one such answer exists for the cycle; anything ambiguous fails closed.
        """
        rows = self.connection.execute(
            """SELECT * FROM clarification_requests
               WHERE thread_id = ? AND cycle_id = ? AND occurrence_key = ''
                 AND status = ?""",
            (thread_id, cycle_id, ClarificationStatus.ANSWERED.value),
        ).fetchall()
        if len(rows) != 1:
            return None
        return ClarificationRequestRecord(**dict(rows[0]))

    def clarification(self, clarification_id: str) -> ClarificationRequestRecord | None:
        row = self.connection.execute(
            "SELECT * FROM clarification_requests WHERE clarification_id = ?",
            (clarification_id,),
        ).fetchone()
        return ClarificationRequestRecord(**dict(row)) if row else None

    def save_clarification(
        self, record: ClarificationRequestRecord
    ) -> ClarificationRequestRecord:
        with self.transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM clarification_requests WHERE clarification_id=?",
                (record.clarification_id,),
            ).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO clarification_requests(
                       clarification_id, thread_id, cycle_id, root_event_key,
                       occurrence_key, requested_from_phase, question, reason, answer_type,
                       choices_json, origin_surface, response_subject_number,
                       response_comment_id, response_url, review_thread_root_id,
                       status, created_at, answered_at, answer_event_key, answer_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(record.__dict__.values()),
                )
            else:
                immutable = (
                    "thread_id",
                    "cycle_id",
                    "root_event_key",
                    "occurrence_key",
                    "requested_from_phase",
                    "question",
                    "reason",
                    "answer_type",
                    "choices_json",
                    "origin_surface",
                    "response_subject_number",
                    "review_thread_root_id",
                )
                if any(existing[name] != getattr(record, name) for name in immutable):
                    raise ValueError("conflicting clarification occurrence data")
                # Request state is monotonic.  A replay can add posting metadata,
                # but can never reopen an answered or cancelled occurrence.
                db.execute(
                    """UPDATE clarification_requests SET
                       response_comment_id=COALESCE(?, response_comment_id),
                       response_url=COALESCE(?, response_url)
                       WHERE clarification_id=?""",
                    (
                        record.response_comment_id,
                        record.response_url,
                        record.clarification_id,
                    ),
                )
        return self.clarification(record.clarification_id)  # type: ignore[return-value]

    def defer_followup(
        self,
        *,
        source_event_key: str,
        thread_id: str,
        originating_cycle_id: int,
        queued_at: str,
        residual_text: str | None = None,
        disposition_status: str | None = None,
    ) -> DeferredFollowupRecord:
        deferred_id = (
            "deferred-"
            + hashlib.sha256(
                f"{source_event_key}\0{residual_text or ''}".encode()
            ).hexdigest()[:24]
        )
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO deferred_followups(
                   deferred_id, source_event_key, thread_id, originating_cycle_id,
                   status, residual_text, queued_at)
                   VALUES(?,?,?,?,'QUEUED',?,?) ON CONFLICT(deferred_id) DO NOTHING""",
                (
                    deferred_id,
                    source_event_key,
                    thread_id,
                    originating_cycle_id,
                    residual_text,
                    queued_at,
                ),
            )
            revision_input_id = (
                "revision-input-"
                + hashlib.sha256(deferred_id.encode()).hexdigest()[:24]
            )
            db.execute(
                """INSERT OR IGNORE INTO revision_inputs_v1(
                   revision_input_id,source_event_key,thread_id,residual_text,
                   classification_reason,status,queued_at)
                   VALUES(?,?,?,?,?,'PENDING',?)""",
                (
                    revision_input_id,
                    source_event_key,
                    thread_id,
                    residual_text,
                    "CLARIFICATION_RESIDUAL",
                    queued_at,
                ),
            )
            if disposition_status is not None:
                db.execute(
                    """INSERT INTO thread_input_consumptions(
                       event_key, thread_id, cycle_id, purpose, status,
                       claimed_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_key) DO NOTHING""",
                    (
                        source_event_key,
                        thread_id,
                        originating_cycle_id,
                        InputPurpose.CLARIFICATION_ROUTED.value,
                        disposition_status,
                        queued_at,
                        queued_at,
                    ),
                )
        return self.deferred_followup(source_event_key, deferred_id=deferred_id)  # type: ignore[return-value]

    def record_input_disposition(
        self,
        event_key: str,
        *,
        thread_id: str,
        cycle_id: int,
        status: str,
        recorded_at: str,
    ) -> WorkflowInputRecord:
        """Durably remove one routed human input from current-wait candidates."""
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    event_key,
                    thread_id,
                    cycle_id,
                    InputPurpose.CLARIFICATION_ROUTED.value,
                    status,
                    recorded_at,
                    recorded_at,
                ),
            )
        return self.input_consumption(event_key)  # type: ignore[return-value]

    def deferred_followup(
        self, source_event_key: str, *, deferred_id: str | None = None
    ) -> DeferredFollowupRecord | None:
        if deferred_id is None:
            row = self.connection.execute(
                "SELECT * FROM deferred_followups WHERE source_event_key = ? "
                "ORDER BY queued_at, deferred_id LIMIT 1",
                (source_event_key,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM deferred_followups WHERE deferred_id = ? "
                "AND source_event_key = ?",
                (deferred_id, source_event_key),
            ).fetchone()
        return DeferredFollowupRecord(**dict(row)) if row else None

    def deferred_followup_by_id(self, deferred_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT df.deferred_id, df.source_event_key AS event_key,
                      df.thread_id, df.status, df.residual_text
               FROM deferred_followups df WHERE df.deferred_id=?""",
            (deferred_id,),
        ).fetchone()

    def deferred_text_for_event(
        self, source_event_key: str, *, deferred_id: str | None = None
    ) -> str | None:
        condition = (
            "deferred_id = ? AND source_event_key = ?"
            if deferred_id
            else "source_event_key = ?"
        )
        params = (deferred_id, source_event_key) if deferred_id else (source_event_key,)
        row = self.connection.execute(
            "SELECT residual_text FROM deferred_followups WHERE "
            + condition
            + " AND residual_text IS NOT NULL "
            + ("" if deferred_id else "ORDER BY queued_at, deferred_id DESC LIMIT 1"),
            params,
        ).fetchone()
        return str(row["residual_text"]) if row else None

    def deferred_followups(self, thread_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT se.*, df.deferred_id AS deferred_id,
                      COALESCE(df.residual_text, se.body) AS body FROM deferred_followups df
               JOIN source_events se ON se.event_key = df.source_event_key
               WHERE df.thread_id = ? AND df.status = 'QUEUED'
               ORDER BY se.source_updated_at, se.discovered_at, se.event_key""",
            (thread_id,),
        ).fetchall()

    def consume_deferred_followup(
        self,
        source_event_key: str,
        *,
        cycle_id: int,
        consumed_at: str,
        deferred_id: str | None = None,
    ) -> None:
        with self.transaction(immediate=True) as db:
            predicate = (
                "deferred_id=? AND source_event_key=?"
                if deferred_id
                else "source_event_key=?"
            )
            params = (
                (cycle_id, consumed_at, deferred_id, source_event_key)
                if deferred_id
                else (cycle_id, consumed_at, source_event_key)
            )
            db.execute(
                """UPDATE deferred_followups SET status='CONSUMED',
                   consumed_cycle_id=?, consumed_at=?
                   WHERE """
                + predicate
                + " AND status='QUEUED'",
                params,
            )

    def consume_input(
        self,
        event_key: str,
        *,
        thread_id: str,
        cycle_id: int,
        purpose: InputPurpose,
        claimed_at: str,
        status: str = "CONSUMED",
    ) -> WorkflowInputRecord:
        with self.transaction(immediate=True) as db:
            db.execute(
                """INSERT INTO thread_input_consumptions(
                   event_key, thread_id, cycle_id, purpose, status, claimed_at,
                   consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO NOTHING""",
                (
                    event_key,
                    thread_id,
                    cycle_id,
                    purpose.value,
                    status,
                    claimed_at,
                    claimed_at,
                ),
            )
            if purpose != InputPurpose.CYCLE_ROOT:
                self._resolve_workflow_control(
                    db,
                    event_key=event_key,
                    thread_id=thread_id,
                    claimed_at=claimed_at,
                    purpose=purpose,
                )
        return self.input_consumption(event_key)  # type: ignore[return-value]

    @staticmethod
    def _resolve_workflow_control(
        db: sqlite3.Connection,
        *,
        event_key: str,
        thread_id: str,
        claimed_at: str,
        purpose: InputPurpose,
    ) -> None:
        existing = db.execute(
            "SELECT status FROM event_executions WHERE event_key = ?", (event_key,)
        ).fetchone()
        if existing is not None and existing[0] not in (ExecutionStatus.SKIPPED.value,):
            raise ValueError("workflow control input already has code execution state")
        db.execute(
            """INSERT INTO event_executions(
               event_key, thread_id, status, attempt_count, started_at,
               completed_at, error_message)
               VALUES (?, ?, ?, 0, ?, ?, ?)
               ON CONFLICT(event_key) DO UPDATE SET status = excluded.status,
               completed_at = excluded.completed_at,
               error_message = excluded.error_message""",
            (
                event_key,
                thread_id,
                ExecutionStatus.SKIPPED.value,
                claimed_at,
                claimed_at,
                f"workflow control input: {purpose.value}",
            ),
        )

    def claim_event_for_execution(
        self,
        event_key: str,
        *,
        expected_thread_id: str,
        now: str,
    ) -> ClaimedEvent:
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """SELECT se.*, thread.issue_number,
                          ee.status AS execution_status
                   FROM source_events se
                   JOIN issue_threads thread ON thread.thread_id = se.thread_id
                   LEFT JOIN event_executions ee ON ee.event_key = se.event_key
                   WHERE se.event_key = ?""",
                (event_key,),
            ).fetchone()
            if row is None:
                raise ValueError("target execution event does not exist")
            if row["thread_id"] != expected_thread_id:
                raise ValueError("target execution event belongs to another thread")
            status = row["execution_status"]
            if status not in (None, ExecutionStatus.RETRY_PENDING.value):
                raise ValueError("target execution event is not claimable")
            self._assert_event_ordering(db, row)
            if status == ExecutionStatus.RETRY_PENDING.value:
                db.execute(
                    """UPDATE event_executions SET status = ?, attempt_count =
                       attempt_count + 1, started_at = ?, completed_at = NULL,
                       error_message = NULL WHERE event_key = ?""",
                    (ExecutionStatus.RUNNING.value, now, event_key),
                )
                retrying = True
            else:
                db.execute(
                    """INSERT INTO event_executions(
                       event_key, thread_id, status, attempt_count, started_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (event_key, expected_thread_id, ExecutionStatus.RUNNING.value, now),
                )
                retrying = False
            return self._claimed_event(row, retrying=retrying)

    @staticmethod
    def _assert_event_ordering(db: sqlite3.Connection, row: sqlite3.Row) -> None:
        earlier = db.execute(
            """SELECT se.event_key, ee.status, EXISTS (
                   SELECT 1 FROM logical_publications ep
                   WHERE ep.source_event_key = se.event_key
                     AND ep.root_input_id = se.event_key
                     AND ep.status IN ('COMPLETED', 'NO_CHANGES')
               ) AS publication_resolved
               FROM source_events se
               LEFT JOIN event_executions ee ON ee.event_key = se.event_key
               WHERE se.thread_id = ? AND (
                 se.source_updated_at < ? OR
                 (se.source_updated_at = ? AND se.discovered_at < ?) OR
                 (se.source_updated_at = ? AND se.discovered_at = ? AND
                  se.event_key < ?)
               ) ORDER BY se.source_updated_at, se.discovered_at, se.event_key""",
            (
                row["thread_id"],
                row["source_updated_at"],
                row["source_updated_at"],
                row["discovered_at"],
                row["source_updated_at"],
                row["discovered_at"],
                row["event_key"],
            ),
        ).fetchall()
        for item in earlier:
            if item["status"] == ExecutionStatus.SKIPPED.value:
                continue
            if (
                item["status"] == ExecutionStatus.SUCCEEDED.value
                and item["publication_resolved"]
            ):
                continue
            raise ValueError("earlier IssueThread event is unresolved")

    @staticmethod
    def _claimed_event(
        row: sqlite3.Row, *, retrying: bool, execution_id: str | None = None
    ) -> ClaimedEvent:
        return ClaimedEvent(
            event_key=row["event_key"],
            thread_id=row["thread_id"],
            repo_id=row["repo_id"],
            repo_full_name=row["repo_full_name"],
            issue_number=row["issue_number"],
            body=row["body"],
            workspace_path=None,
            retrying=retrying,
            origin_surface=row["origin_surface"],
            path=row["path"],
            line=row["line"],
            start_line=row["start_line"],
            side=row["side"],
            start_side=row["start_side"],
            diff_hunk=row["diff_hunk"],
            commit_id=row["commit_id"],
            original_commit_id=row["original_commit_id"],
            in_reply_to_id=row["in_reply_to_id"],
            pull_request_review_id=row["pull_request_review_id"],
            review_thread_root_id=row["review_thread_root_id"],
            review_state=row["review_state"],
            execution_id=execution_id,
        )

    def unconsumed_inputs(self, thread_id: str, *, after_event_key: str | None = None):
        rows = self.source_events_for_thread(thread_id)
        if after_event_key is not None:
            keys = [row["event_key"] for row in rows]
            if after_event_key in keys:
                rows = rows[keys.index(after_event_key) + 1 :]
        return [
            row
            for row in rows
            if self.input_consumption(row["event_key"]) is None
            and self.deferred_followup(row["event_key"]) is None
        ]

    @staticmethod
    def _plan_record(row: sqlite3.Row) -> PlanRecord:
        values = dict(row)
        values["status"] = PlanStatus(values["status"])
        return PlanRecord(**values)

    @staticmethod
    def _permit_record(row: sqlite3.Row) -> ExecutionPermit:
        values = dict(row)
        if values.get("root_event_key") is None:
            raise ValueError("execution permit migration could not recover root event")
        values["source"] = PermitSource(values["source"])
        return ExecutionPermit(**values)

    @staticmethod
    def _input_record(row: sqlite3.Row) -> WorkflowInputRecord:
        values = dict(row)
        values["purpose"] = InputPurpose(values["purpose"])
        return WorkflowInputRecord(**values)

    def events(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_events ORDER BY discovered_at, event_key"
        ).fetchall()

    def threads(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM issue_threads ORDER BY repo_id, issue_number"
        ).fetchall()

    def dispatcher_failure(self, thread_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM dispatcher_failures WHERE thread_id = ?", (thread_id,)
        ).fetchone()

    def record_dispatcher_failure(
        self, thread_id: str, *, now: str, error: str, base_seconds: int = 5
    ) -> None:
        """Persist bounded worker backoff so restarts cannot hot-loop a failure."""
        with self.transaction(immediate=True) as db:
            row = db.execute(
                "SELECT failure_count FROM dispatcher_failures WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            count = int(row[0]) + 1 if row else 1
            delay = min(3600, base_seconds * (2 ** min(count - 1, 10)))
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            eligible = (
                (current + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            )
            db.execute(
                "INSERT INTO dispatcher_failures "
                "(thread_id, failure_count, next_eligible_at, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET failure_count=excluded.failure_count, "
                "next_eligible_at=excluded.next_eligible_at, last_error=excluded.last_error, "
                "updated_at=excluded.updated_at",
                (thread_id, count, eligible, error[:1000], now),
            )

    def clear_dispatcher_failure(self, thread_id: str) -> None:
        with self.transaction(immediate=True) as db:
            db.execute(
                "DELETE FROM dispatcher_failures WHERE thread_id = ?", (thread_id,)
            )

    def runnable_thread_ids(self, *, now: str, limit: int | None = None) -> list[str]:
        """Return durable work candidates in stable update-time order.

        Waiting states are selected only when an unconsumed @agent input can
        change them.  This is deliberately a conservative query: a worker may
        still decide that an input is stale or irrelevant, but unchanged waits
        are never repeatedly submitted to the model.
        """
        rows = self.connection.execute(
            "SELECT t.thread_id, t.updated_at, s.phase, s.mode "
            "FROM issue_threads AS t LEFT JOIN issue_workflow_state AS s "
            "ON s.thread_id = t.thread_id "
            "LEFT JOIN dispatcher_failures AS f ON f.thread_id = t.thread_id "
            "WHERE f.thread_id IS NULL OR f.next_eligible_at <= ? "
            "ORDER BY COALESCE(s.updated_at, t.updated_at), t.thread_id",
            (now,),
        ).fetchall()
        selected: list[str] = []
        for row in rows:
            if self.is_thread_runnable(row["thread_id"], now=now):
                selected.append(row["thread_id"])
                if limit is not None and len(selected) >= limit:
                    break
        return selected

    def is_thread_runnable(self, thread_id: str, *, now: str) -> bool:
        """Authoritative durable predicate used by selection and draining."""
        failure = self.dispatcher_failure(thread_id)
        if failure is not None and failure["next_eligible_at"] > now:
            return False
        thread = self.issue_thread(thread_id)
        if thread is None:
            return False
        state = self.workflow_state(thread_id)
        pending = self.unconsumed_inputs(thread_id)
        actionable = any(
            is_actionable_source_event(item["source_kind"], item["body"])
            for item in pending
        )
        generic = self.connection.execute(
            """SELECT * FROM workflow_cycles_v1 WHERE thread_id=?
               ORDER BY cycle_id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if generic is not None:
            return self._declarative_thread_runnable(
                generic=generic, pending=pending, actionable=actionable, now=now
            )
        if state is None:
            return actionable
        phase = state.phase
        if phase in {
            WorkflowPhase.PLANNING,
            WorkflowPhase.EXECUTION_READY,
            WorkflowPhase.EXECUTING,
            WorkflowPhase.REVIEW_EXECUTION,
            WorkflowPhase.REPAIR_READY,
        }:
            return True
        if phase == WorkflowPhase.IDLE:
            return bool(
                actionable
                or self.deferred_followups(thread_id)
                or self.pending_memory_learning(thread_id)
                or self.pending_issue_resolution(thread_id)
            )
        if phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL:
            if state.mode == WorkflowMode.AUTO:
                return True
            plan = self.current_plan(thread_id)
            if plan is None or not plan.posted_at:
                return False
            return any(self._current_plan_input(item, state, plan) for item in pending)
        if phase == WorkflowPhase.WAITING_FOR_INPUT:
            return any(self._current_wait_input(item, state) for item in pending)
        if phase == WorkflowPhase.AWAITING_PUBLICATION:
            publication = self.publication_for_cycle(
                thread_id=thread_id,
                cycle_id=state.cycle_id,
                root_event_key=state.root_event_key,
                root_input_id=state.root_input_id,
            )
            if publication is None:
                return self.publication_target(thread_id) is not None
            return publication.status in RESUMABLE_PUBLICATION_STATUSES
        return False

    def _declarative_thread_runnable(
        self,
        *,
        generic: sqlite3.Row,
        pending: list[sqlite3.Row],
        actionable: bool,
        now: str,
    ) -> bool:
        status = generic["status"]
        if self.pending_workflow_comments(generic["thread_id"], due_at=now):
            return True
        pending_revision = bool(self.pending_revision_inputs(generic["thread_id"]))
        if status == "FAILED":
            return False
        if status == "PUBLISHED":
            return bool(
                self.pending_memory_learning(generic["thread_id"])
                or self.pending_issue_resolution(generic["thread_id"])
                or self.deferred_followups(generic["thread_id"])
                or pending_revision
                or actionable
            )
        if status == "AWAITING_PUBLICATION":
            if pending_revision:
                return True
            publication_id = publication_id_for(
                thread_id=generic["thread_id"],
                cycle_id=generic["cycle_id"],
                root_input_id=generic["root_input_id"],
            )
            publication = self.publication_for_id(publication_id)
            if publication is None:
                return (
                    self._publication_target(self.connection, generic["thread_id"])
                    is not None
                )
            return publication.status in RESUMABLE_PUBLICATION_STATUSES
        task = self.connection.execute(
            """SELECT * FROM workflow_task_runs_v1
               WHERE workflow_cycle_id=? AND task_id=?""",
            (generic["workflow_cycle_id"], generic["active_task_id"]),
        ).fetchone()
        if task is None:
            return True
        if task["phase"] in {"PLANNING", "EXECUTING", "VALIDATING"}:
            return True
        if self.feedback_review_for_task(task["task_run_id"]) is not None:
            # A semantic feedback review is still in flight. Beginning one
            # consumes its triggering input, so no unconsumed input remains to
            # select this thread; without this clause a review interrupted by a
            # restart or tick boundary is stranded and the user's feedback is
            # silently lost. The controller already reconstructs the exact
            # resume payload from the stored review.
            return True
        if (
            task["phase"]
            in {
                "WAITING_FOR_PLAN_APPROVAL",
                "WAITING_FOR_RESULT_APPROVAL",
            }
            and self.interaction_mode(generic["thread_id"]) == InteractionMode.AUTO
        ):
            return True
        root = self._cycle_root_event_sql(self.connection, generic["root_input_id"])
        if root is None:
            return False
        if task["phase"] == "WAITING_FOR_PLAN_APPROVAL":
            plan = self.connection.execute(
                "SELECT posted_at FROM workflow_task_plans_v1 WHERE plan_id=?",
                (task["current_plan_id"],),
            ).fetchone()
            if plan is None:
                return False
            return any(
                self._same_generic_target(item, root)
                and self._after_posted_at(item, plan["posted_at"])
                and starts_with_agent_invocation(item["body"])
                for item in pending
            )
        if task["phase"] == "WAITING_FOR_RESULT_APPROVAL":
            result = self.connection.execute(
                """SELECT r.posted_at FROM workflow_task_results_v1 AS r
                   JOIN workflow_task_validations_v1 AS v
                     ON v.validation_id=r.validation_id
                   WHERE r.task_run_id=?
                   ORDER BY v.validation_round DESC,r.result_id DESC LIMIT 1""",
                (task["task_run_id"],),
            ).fetchone()
            if result is None:
                return False
            return any(
                self._same_generic_target(item, root)
                and self._after_posted_at(item, result["posted_at"])
                and starts_with_agent_invocation(item["body"])
                for item in pending
            )
        if task["phase"] == "WAITING_FOR_INPUT":
            return any(
                self._same_generic_target(item, root)
                and self._after_posted_at(item, task["updated_at"])
                and starts_with_agent_invocation(item["body"])
                for item in pending
            )
        return False

    @staticmethod
    def _same_generic_target(item: sqlite3.Row, root: sqlite3.Row) -> bool:
        if (
            item["origin_surface"] != root["origin_surface"]
            or item["subject_number"] != root["subject_number"]
        ):
            return False
        return root["origin_surface"] != "PR_INLINE_REVIEW" or (
            item["review_thread_root_id"] == root["review_thread_root_id"]
        )

    @staticmethod
    def _same_conversation_target(item, state) -> bool:
        if item["origin_surface"] != state.response_surface:
            return False
        if item["subject_number"] != state.response_subject_number:
            return False
        if state.response_surface == "PR_INLINE_REVIEW":
            return item["review_thread_root_id"] == state.review_thread_root_id
        return True

    @staticmethod
    def _after_posted_at(item, posted_at: str) -> bool:
        try:
            created = datetime.fromisoformat(
                (item["source_created_at"] or "").replace("Z", "+00:00")
            )
            posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return created > posted

    def _current_plan_input(self, item, state, plan) -> bool:
        return (
            self._same_conversation_target(item, state)
            and self._after_posted_at(item, plan.posted_at)
            and starts_with_agent_invocation(item["body"])
        )

    def _current_wait_input(self, item, state) -> bool:
        return self._same_conversation_target(
            item, state
        ) and starts_with_agent_invocation(item["body"])
