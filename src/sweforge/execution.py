"""Durable one-shot execution for routed GitHub source events."""

import fcntl
import hashlib
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.base import BaseStore

from .agent import run_task
from .github_models import format_source_context
from .github_store import (
    ClaimedEvent,
    ExecutionStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
)
from .repo_memory import ensure_repo_memory, repo_memory_namespace
from .workspace import ThreadWorkspace, WorkspaceError

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
    ) -> str: ...


class EmptyTaskError(ValueError):
    """Raised when an @agent event has no task after mention removal."""


class ThreadLockUnavailable(RuntimeError):
    """Raised when another process currently owns a thread lock."""


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


@contextmanager
def thread_lock(root: str | Path, thread_id: str) -> Iterator[None]:
    """Acquire a non-blocking cross-process lock for exactly one thread."""
    lock_root = Path(root).expanduser().resolve()
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{hashlib.sha256(thread_id.encode()).hexdigest()}.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ThreadLockUnavailable(
                "IssueThread is already being executed"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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

    @property
    def has_work(self) -> bool:
        return self.event is not None


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
                now=clock,
            )
    except ThreadLockUnavailable:
        store.release_execution_claim(event.event_key, retrying=event.retrying)
        return ExecutionResult(status="BUSY", event=event)
    except Exception as exc:
        error = _safe_error(exc)
        store.mark_execution_failed(
            event.event_key,
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
    now: Callable[[], datetime],
    persist_execution: bool = True,
    allow_dirty_workspace: bool = False,
    message_id: str | None = None,
    task_override: str | None = None,
) -> ExecutionResult:
    workspace: ThreadWorkspace | None = None
    try:
        task = task_override or normalize_task(event.body)
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
        runner_kwargs = {
            "model": model,
            "worktree": str(workspace.path),
            "task": task,
            "thread_id": event.thread_id,
            "checkpointer": checkpointer,
            "message_id": message_id or event_message_id(event.event_key),
            "resume_if_present": True,
            "memory_store": memory_store,
            "memory_namespace": memory_namespace if memory_store is not None else None,
        }
        if live_input_provider is not None:
            runner_kwargs["live_input_provider"] = live_input_provider
        if live_delivered_event_keys is not None:
            runner_kwargs["live_delivered_event_keys"] = live_delivered_event_keys
        response = runner(**runner_kwargs)
        changed = tuple(workspace.changed_files())
        diff = workspace.diff()
        end_head_sha = workspace.head_sha()
        end_dirty = not workspace.is_clean()
        if persist_execution:
            store.mark_execution_succeeded(
                event.event_key,
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
