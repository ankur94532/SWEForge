"""Durable one-shot execution for routed GitHub source events."""

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.base import BaseStore

from .agent import run_task
from .capabilities import RepoCapabilityRegistry
from .context import RepoAgentContext
from .execution_locks import (
    LockOrderError,
    ThreadLockUnavailable,
    held_repo_git_locks,
    repo_git_lock,
    thread_lock,
)
from .execution_security import SandboxBackendProvider
from .github_models import format_source_context
from .github_store import (
    ClaimedEvent,
    ExecutionStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
)
from .repo_memory import ensure_repo_memory, repo_memory_namespace
from .workspace import ThreadWorkspace, WorkspaceError

__all__ = [
    "LockOrderError",
    "ThreadLockUnavailable",
    "held_repo_git_locks",
    "repo_git_lock",
    "thread_lock",
]

_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@agent(?![A-Za-z0-9_])", re.IGNORECASE)


class TaskRunner(Protocol):
    def __call__(
        self,
        *,
        model: str,
        worktree: str,
        task: str,
        thread_id: str,
        checkpointer: object,
        message_id: str,
        resume_if_present: bool,
        memory_store: BaseStore | None,
        memory_namespace: tuple[str, ...] | None,
        live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
        live_delivered_event_keys: set[str] | None = None,
        repo_context: RepoAgentContext,
        capability_registry: RepoCapabilityRegistry | None = None,
        sandbox_backend_provider: SandboxBackendProvider | None = None,
        secure_execution: bool = True,
        unsafe_local_shell: bool = False,
        clarification_request_sink: Callable[[dict], None] | None = None,
        resume_value: object | None = None,
        resume_resolver: Callable[[tuple[dict, ...]], object] | None = None,
        repo_memory_proposal_sink: Callable[..., str] | None = None,
        issue_memory_search: Callable[[str, int], str] | None = None,
        execution_evidence_sink: Callable[..., Any] | None = None,
    ) -> str: ...


class EmptyTaskError(ValueError):
    """Raised when an @agent event has no task after mention removal."""


def normalize_task(body: str) -> str:
    """Remove invocation mentions while preserving the user's remaining text."""
    task = re.sub(r"[ \t]{2,}", " ", _MENTION_RE.sub("", body)).strip()
    if not task:
        raise EmptyTaskError("@agent mention did not contain a task")
    return task


def event_message_id(event_key: str) -> str:
    """Return the stable LangGraph message ID for one SourceEvent."""
    return f"sweforge:event:{hashlib.sha256(event_key.encode()).hexdigest()}"


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution clock must return an aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _review_context(worktree: Path, event: ClaimedEvent) -> str | None:
    """Return bounded current code context for an inline review anchor."""
    path = getattr(event, "path", None)
    line = getattr(event, "line", None) or getattr(event, "start_line", None)
    if not path or not line or Path(path).is_absolute() or ".." in Path(path).parts:
        return None
    target = (worktree / path).resolve()
    try:
        target.relative_to(worktree.resolve())
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, ValueError):
        return None
    start = max(0, int(line) - 6)
    end = min(len(lines), int(line) + 5)
    return "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))


class SQLiteCheckpointer:
    """Strictly serialized, file-backed LangGraph checkpoint lifecycle."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.saver = SqliteSaver(
            self.connection,
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_msgpack_modules=None,
            ),
        )
        try:
            self.saver.setup()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SqliteSaver:
        return self.saver

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    event: ClaimedEvent | None = None
    workspace: ThreadWorkspace | None = None
    response: str = ""
    changed_files: tuple[str, ...] = ()
    diff: str = ""
    error: str | None = None
    workspace_created: bool = False
    clarification: "ClarificationRequestProposal | None" = None

    @property
    def has_work(self) -> bool:
        return self.event is not None


@dataclass(frozen=True)
class ClarificationRequestProposal:
    question: str
    reason: str
    answer_type: str = "TEXT"
    choices: tuple[str, ...] = ()
    occurrence_key: str = ""


def recover_stale(
    *,
    store: SQLiteGitHubStore,
    lock_root: str | Path,
    older_than_seconds: int,
    now: datetime | None = None,
) -> list[str]:
    """Mark old RUNNING rows interrupted only when their local lock is free."""
    current = now or datetime.now(UTC)
    current = datetime.fromisoformat(utc_timestamp(current).replace("Z", "+00:00"))
    cutoff_text = utc_timestamp(current - timedelta(seconds=older_than_seconds))
    recovered: list[str] = []
    for record in store.running_executions_before(cutoff_text):
        try:
            with thread_lock(lock_root, record.thread_id):
                store.mark_execution_interrupted(
                    record.event_key,
                    execution_id=record.execution_id,
                    completed_at=utc_timestamp(current),
                    error_message="executor lock was free after stale RUNNING claim",
                )
                recovered.append(record.event_key)
        except ThreadLockUnavailable:
            continue
    return recovered


def execute_one(
    *,
    store: SQLiteGitHubStore,
    model: str,
    repo_paths: dict[str, str | Path],
    workspace_root: str | Path,
    lock_root: str | Path,
    checkpointer: object,
    runner: TaskRunner = run_task,
    memory_store: BaseStore | None = None,
    now: Callable[[], datetime] | None = None,
    live_input_provider: Callable[[], list[tuple[str, str]]] | None = None,
    live_delivered_event_keys: set[str] | None = None,
    approved_plan_text: str | None = None,
    approved_plan_id: str | None = None,
    approved_plan_version: int | None = None,
    capability_registry: RepoCapabilityRegistry | None = None,
    sandbox_backend_provider: SandboxBackendProvider | None = None,
    secure_execution: bool = True,
    unsafe_local_shell: bool = False,
    clarification_request_sink: Callable[[dict], None] | None = None,
    resume_value: object | None = None,
    resume_resolver: Callable[[tuple[dict, ...]], object] | None = None,
    repo_memory_proposal_sink: Callable[..., str] | None = None,
    issue_memory_search: Callable[[str, int], str] | None = None,
    clarification_enabled: bool = True,
    execution_evidence_sink: Callable[..., Any] | None = None,
) -> ExecutionResult:
    clock = now or (lambda: datetime.now(UTC))
    event = store.claim_next_event(now=utc_timestamp(clock()))
    if event is None:
        return ExecutionResult(status="NO_WORK")

    try:
        with thread_lock(lock_root, event.thread_id):
            return _execute_claim(
                store=store,
                event=event,
                lock_root=lock_root,
                model=model,
                repo_paths=repo_paths,
                workspace_root=workspace_root,
                checkpointer=checkpointer,
                runner=runner,
                memory_store=memory_store,
                live_input_provider=live_input_provider,
                live_delivered_event_keys=live_delivered_event_keys,
                approved_plan_text=approved_plan_text,
                approved_plan_id=approved_plan_id,
                approved_plan_version=approved_plan_version,
                capability_registry=capability_registry,
                sandbox_backend_provider=sandbox_backend_provider,
                secure_execution=secure_execution,
                unsafe_local_shell=unsafe_local_shell,
                clarification_request_sink=clarification_request_sink,
                resume_value=resume_value,
                resume_resolver=resume_resolver,
                repo_memory_proposal_sink=repo_memory_proposal_sink,
                issue_memory_search=issue_memory_search,
                clarification_enabled=clarification_enabled,
                execution_evidence_sink=execution_evidence_sink,
                now=clock,
            )
    except ThreadLockUnavailable:
        store.release_execution_claim(event.event_key, retrying=event.retrying)
        return ExecutionResult(status="BUSY", event=event)
    except Exception as exc:
        error = _safe_error(exc)
        store.mark_execution_failed(
            event.event_key,
            execution_id=event.execution_id,
            completed_at=utc_timestamp(clock()),
            error_message=error,
            workspace_path=None,
        )
        return ExecutionResult(
            status=ExecutionStatus.FAILED.value, event=event, error=error
        )


def _execute_claim(
    *,
    store: SQLiteGitHubStore,
    event: ClaimedEvent,
    model: str,
    repo_paths: dict[str, str | Path],
    workspace_root: str | Path,
    checkpointer: object,
    runner: TaskRunner,
    memory_store: BaseStore | None,
    live_input_provider: Callable[[], list[tuple[str, str]]] | None,
    live_delivered_event_keys: set[str] | None,
    approved_plan_text: str | None,
    approved_plan_id: str | None,
    approved_plan_version: int | None,
    deferred_id: str | None = None,
    now: Callable[[], datetime],
    lock_root: str | Path | None = None,
    persist_execution: bool = True,
    allow_dirty_workspace: bool = False,
    message_id: str | None = None,
    prepared_task: str | None = None,
    capability_registry: RepoCapabilityRegistry | None = None,
    sandbox_backend_provider: SandboxBackendProvider | None = None,
    secure_execution: bool = True,
    unsafe_local_shell: bool = False,
    clarification_request_sink: Callable[[dict], None] | None = None,
    resume_value: object | None = None,
    resume_resolver: Callable[[tuple[dict, ...]], object] | None = None,
    repo_memory_proposal_sink: Callable[..., str] | None = None,
    issue_memory_search: Callable[[str, int], str] | None = None,
    clarification_enabled: bool = True,
    execution_evidence_sink: Callable[..., Any] | None = None,
) -> ExecutionResult:
    workspace: ThreadWorkspace | None = None
    try:
        deferred_text = store.deferred_text_for_event(
            event.event_key, deferred_id=deferred_id
        )
        task = (
            prepared_task
            if prepared_task is not None
            else deferred_text or normalize_task(event.body)
        )
        repository_path = repo_paths.get(event.repo_full_name)
        if repository_path is None:
            raise WorkspaceError(
                f"no trusted local checkout configured for {event.repo_full_name}"
            )
        source_path = Path(repository_path).expanduser().resolve()
        metadata = store.thread_workspace(event.thread_id)
        if metadata and Path(metadata.source_repository_path).resolve() != source_path:
            raise WorkspaceError("repository mapping conflicts with thread workspace")
        workspace = ThreadWorkspace.create(
            repository=source_path,
            workspace_root=workspace_root,
            repo_id=event.repo_id,
            issue_number=event.issue_number,
            existing_path=metadata.workspace_path if metadata else None,
            expected_branch=metadata.branch_name if metadata else None,
            expected_base=metadata.base_commit if metadata else None,
            lock_root=lock_root,
        )
        if metadata is None:
            timestamp = utc_timestamp(now())
            store.save_thread_workspace(
                ThreadWorkspaceRecord(
                    thread_id=event.thread_id,
                    repo_id=event.repo_id,
                    repo_full_name=event.repo_full_name,
                    issue_number=event.issue_number,
                    source_repository_path=str(source_path),
                    workspace_path=str(workspace.path),
                    branch_name=workspace.branch_name,
                    base_commit=workspace.base_commit,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        elif (
            not event.retrying
            and not allow_dirty_workspace
            and not workspace.is_clean()
        ):
            raise WorkspaceError(
                "existing workspace is dirty before a new IssueThread event"
            )
        start_head_sha = workspace.head_sha()
        if prepared_task is None:
            if event.path:
                task = format_source_context(
                    event, task, _review_context(workspace.path, event)
                )
            else:
                task = format_source_context(event, task)
        if approved_plan_text is not None:
            task += (
                f"\n\n[Approved SWEForge Plan v{approved_plan_version} "
                f"{approved_plan_id}]\n{approved_plan_text[:12_000]}\n"
                "SWEForge application code authorized this exact plan. Execute it; "
                "do not decide whether approval is valid."
            )
        memory_namespace = repo_memory_namespace(event.repo_id)
        if memory_store is not None:
            ensure_repo_memory(memory_store, memory_namespace)
        clarification_holder: dict[str, dict] = {}

        def capture_interrupt(proposal: dict) -> None:
            clarification_holder["proposal"] = proposal
            if clarification_request_sink is not None:
                clarification_request_sink(proposal)

        runner_kwargs = {
            "model": model,
            "worktree": str(workspace.path),
            "task": task,
            "thread_id": event.thread_id,
            "checkpointer": checkpointer,
            "message_id": message_id or event_message_id(event.event_key),
            "resume_if_present": True,
            "memory_store": memory_store,
            "memory_namespace": None,
            "repo_context": RepoAgentContext(
                repo_id=event.repo_id,
                repo_full_name=event.repo_full_name,
                thread_id=event.thread_id,
            ),
            "capability_registry": capability_registry,
            "sandbox_backend_provider": sandbox_backend_provider,
            "secure_execution": secure_execution,
            "unsafe_local_shell": unsafe_local_shell,
            "execution_evidence_sink": execution_evidence_sink,
            "resume_value": resume_value,
            "resume_resolver": resume_resolver,
        }
        if clarification_enabled:
            # The sink is what registers request_clarification, so withholding
            # it is what actually keeps the tool out of a repair run.
            runner_kwargs["interrupt_result_sink"] = capture_interrupt
        if resume_value is None:
            runner_kwargs.pop("resume_value", None)
        if resume_resolver is None:
            runner_kwargs.pop("resume_resolver", None)
        if repo_memory_proposal_sink is not None:
            runner_kwargs["repo_memory_proposal_sink"] = repo_memory_proposal_sink
        if issue_memory_search is not None:
            runner_kwargs["issue_memory_search"] = issue_memory_search
        if live_input_provider is not None:
            runner_kwargs["live_input_provider"] = live_input_provider
        if live_delivered_event_keys is not None:
            runner_kwargs["live_delivered_event_keys"] = live_delivered_event_keys
        response = runner(**runner_kwargs)
        changed = tuple(workspace.changed_files())
        diff = workspace.diff()
        end_head_sha = workspace.head_sha()
        end_dirty = not workspace.is_clean()
        if clarification_holder.get("proposal"):
            proposal = clarification_holder["proposal"]
            return ExecutionResult(
                status="CLARIFICATION",
                event=event,
                workspace=workspace,
                response=str(response),
                changed_files=changed,
                diff=diff,
                clarification=ClarificationRequestProposal(
                    question=proposal["question"],
                    reason=proposal["reason"],
                    answer_type=proposal["answer_type"],
                    choices=tuple(proposal["choices"]),
                    occurrence_key=proposal.get("occurrence_key", ""),
                ),
                workspace_created=workspace.created,
            )
        if persist_execution:
            store.mark_execution_succeeded(
                event.event_key,
                execution_id=event.execution_id,
                completed_at=utc_timestamp(now()),
                response_text=response,
                workspace_path=str(workspace.path),
                start_head_sha=start_head_sha,
                end_head_sha=end_head_sha,
                end_dirty=end_dirty,
            )
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED.value,
            event=event,
            workspace=workspace,
            response=response,
            changed_files=changed,
            diff=diff,
            workspace_created=workspace.created,
        )
    except Exception as exc:
        error = _safe_error(exc)
        if persist_execution:
            store.mark_execution_failed(
                event.event_key,
                execution_id=event.execution_id,
                completed_at=utc_timestamp(now()),
                error_message=error,
                workspace_path=str(workspace.path) if workspace else None,
            )
        return ExecutionResult(
            status=ExecutionStatus.FAILED.value,
            event=event,
            workspace=workspace,
            error=error,
            workspace_created=workspace.created if workspace else False,
        )


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for value in os.environ.values():
        if value and len(value) >= 4:
            message = message.replace(value, "[REDACTED]")
    return message
