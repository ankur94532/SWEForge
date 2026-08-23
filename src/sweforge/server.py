"""Single-process durable SWEForge dispatcher."""

from __future__ import annotations

import fcntl
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .capabilities import load_capability_registry
from .clarification import build_clarification_classifier
from .execution import SQLiteCheckpointer
from .execution_security import resolve_sandbox_provider
from .github_auth import (
    DEFAULT_API_VERSION,
    GitHubAppAuthenticator,
    StaticGitHubTokenProvider,
)
from .github_client import GitHubClient, HttpxGitHubClient
from .github_poller import GitHubPoller
from .github_publisher import GitHubPublisher
from .github_store import SQLiteGitHubStore
from .repo_memory import SQLiteMemoryStore
from .workflow import WorkflowEngine, WorkflowPhase


@dataclass(frozen=True)
class ServerConfig:
    repositories: tuple[str, ...]
    repo_paths: dict[str, Path]
    db: Path = Path("~/.sweforge/state.db")
    checkpoints: Path = Path("~/.sweforge/checkpoints.sqlite")
    memory_db: Path = Path("~/.sweforge/memory.sqlite")
    workspace_root: Path = Path("~/.sweforge/workspaces")
    lock_root: Path = Path("~/.sweforge/locks")
    model: str = ""
    planning_model: str | None = None
    execution_model: str | None = None
    review_model: str | None = None
    memory_model: str | None = None
    resolution_model: str | None = None
    clarification_model: str | None = None
    capabilities_config: Path | None = None
    sandbox_provider: str | None = None
    unsafe_local_shell: bool = False
    api_url: str = "https://api.github.com"
    api_version: str = DEFAULT_API_VERSION
    workers: int = 4
    poll_interval: float = 30.0
    max_ticks: int = 20
    initial_lookback_minutes: int = 10
    once: bool = False

    @property
    def planning(self) -> str:
        return self.planning_model or self.model

    @property
    def execution(self) -> str:
        return self.execution_model or self.model

    @property
    def review(self) -> str:
        return self.review_model or self.model

    @property
    def memory(self) -> str:
        return self.memory_model or self.review

    @property
    def resolution(self) -> str:
        return self.resolution_model or self.memory

    @property
    def clarification(self) -> str:
        return self.clarification_model or self.execution


class ServerInstanceLock:
    """Non-blocking host-local singleton lock for one state database."""

    def __init__(self, db: str | Path) -> None:
        path = Path(db).expanduser().resolve()
        self.path = path.with_name(path.name + ".server.lock")
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(
                f"SWEForge server already running for {self.path}"
            ) from exc
        self._handle = handle

    def close(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> ServerInstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _credentials(config: ServerConfig):
    client_id = os.getenv("SWEFORGE_GITHUB_APP_CLIENT_ID") or os.getenv(
        "SWEFORGE_GITHUB_CLIENT_ID"
    )
    app_id = os.getenv("SWEFORGE_GITHUB_APP_ID")
    key_path = os.getenv("SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH")
    legacy_token = os.getenv("SWEFORGE_GITHUB_TOKEN")
    if client_id or app_id or key_path:
        if not (client_id or app_id) or not key_path:
            raise ValueError("both GitHub App credentials are required")
        authenticator = GitHubAppAuthenticator(
            app_id,
            key_path,
            client_id=client_id,
            api_url=config.api_url,
            api_version=config.api_version,
        )
        client = HttpxGitHubClient(
            token_provider=authenticator,
            api_url=config.api_url,
            api_version=config.api_version,
        )
        return client, authenticator
    if legacy_token:
        return (
            HttpxGitHubClient(
                legacy_token, api_url=config.api_url, api_version=config.api_version
            ),
            StaticGitHubTokenProvider(legacy_token),
        )
    raise ValueError(
        "GitHub App credentials are required (legacy "
        "SWEFORGE_GITHUB_TOKEN is also supported)"
    )


def _safe_dispatch_error(exc: BaseException) -> str:
    """Persist only a bounded, redacted diagnostic string."""
    message = f"{type(exc).__name__}: {exc}"
    for name in (
        "SWEFORGE_GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        value = os.getenv(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    message = re.sub(
        r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        message,
        flags=re.DOTALL,
    )
    message = re.sub(r"(https?://)([^/\s:@]+):([^@\s]+)@", r"\1[REDACTED]@", message)
    return message[:1000]


class SWEForgeServer:
    """Poll, fairly dispatch, and drain durable workflows in one process."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        client_factory: Callable[[ServerConfig], tuple[GitHubClient, object]]
        | None = None,
        poller_factory: Callable[..., GitHubPoller] | None = None,
        worker_runner: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if config.workers < 1 or config.max_ticks < 1 or config.poll_interval < 0:
            raise ValueError(
                "workers and max-ticks must be positive; "
                "poll interval cannot be negative"
            )
        if not config.repo_paths or set(config.repositories) != set(config.repo_paths):
            raise ValueError("repositories and repo-path mappings must match")
        self.config = config
        self.client_factory = client_factory or _credentials
        self.poller_factory = poller_factory or GitHubPoller
        self.worker_runner = worker_runner
        self.now = now or (lambda: datetime.now(UTC))
        self.stop_event = threading.Event()
        self._futures: dict[Future, str] = {}
        self._futures_lock = threading.Lock()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _poll(self, client: GitHubClient, store: SQLiteGitHubStore) -> None:
        poller = self.poller_factory(
            client,
            store,
            now=self.now,
            initial_lookback=timedelta(minutes=self.config.initial_lookback_minutes),
        )
        poller.poll(self.config.repositories)

    def _submit(self, executor: ThreadPoolExecutor, store: SQLiteGitHubStore) -> None:
        now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        with self._futures_lock:
            active = set(self._futures.values())
            for thread_id in store.runnable_thread_ids(now=now):
                if len(self._futures) >= self.config.workers:
                    break
                if thread_id in active:
                    continue
                future = executor.submit(self._worker_entry, thread_id)
                self._futures[future] = thread_id

    def _reap(self) -> None:
        with self._futures_lock:
            done = [future for future in self._futures if future.done()]
            for future in done:
                self._futures.pop(future)
                # _worker_entry persists the failure; retrieving the exception
                # prevents silent Future warnings without crashing the server.
                future.exception()

    def _worker_entry(self, thread_id: str) -> None:
        store = SQLiteGitHubStore(self.config.db)
        client = authenticator = checkpoints = memory = None
        try:
            if self.worker_runner is not None:
                self.worker_runner(thread_id)
                return
            client, authenticator = self.client_factory(self.config)
            checkpoints = SQLiteCheckpointer(self.config.checkpoints)
            memory = SQLiteMemoryStore(self.config.memory_db)
            engine = WorkflowEngine(
                store=store,
                client=client,
                clarification_classifier=build_clarification_classifier(
                    self.config.clarification
                ),
            )
            capability_registry = (
                load_capability_registry(self.config.capabilities_config)
                if self.config.capabilities_config
                else None
            )
            execute_kwargs = {
                "model": self.config.execution,
                "repo_paths": self.config.repo_paths,
                "workspace_root": self.config.workspace_root,
                "lock_root": self.config.lock_root,
                "checkpointer": checkpoints.saver,
                "memory_store": memory.store,
                "capability_registry": capability_registry,
                "secure_execution": not self.config.unsafe_local_shell,
                "unsafe_local_shell": self.config.unsafe_local_shell,
                "sandbox_backend_provider": resolve_sandbox_provider(
                    self.config.sandbox_provider
                ),
            }
            self._drain_workflow(
                thread_id=thread_id,
                store=store,
                engine=engine,
                client=client,
                token_provider=authenticator,
                memory_store=memory.store,
                execute_kwargs=execute_kwargs,
            )
            store.clear_dispatcher_failure(thread_id)
        except Exception as exc:
            now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
            store.record_dispatcher_failure(
                thread_id, now=now, error=_safe_dispatch_error(exc)
            )
            raise
        finally:
            for resource in (memory, checkpoints, store, client, authenticator):
                if resource is not None and hasattr(resource, "close"):
                    resource.close()

    def _drain_workflow(
        self,
        *,
        thread_id: str,
        store: SQLiteGitHubStore,
        engine: WorkflowEngine,
        client: GitHubClient,
        token_provider: object,
        memory_store: object,
        execute_kwargs: dict,
    ) -> None:
        """Drain bounded authoritative workflow ticks for one IssueThread."""
        for _ in range(self.config.max_ticks):
            result = engine.advance(
                thread_id=thread_id,
                model=self.config.planning,
                review_model=self.config.review,
                memory_model=self.config.memory,
                resolution_model=self.config.resolution,
                repo_paths=self.config.repo_paths,
                workspace_root=self.config.workspace_root,
                memory_store=memory_store,
                execute_kwargs=execute_kwargs,
            )
            if result.phase == WorkflowPhase.AWAITING_PUBLICATION:
                publication_id = store.eligible_publication_id(thread_id)
                if not publication_id:
                    break
                publication_record = store.publication_for_id(publication_id)
                if (
                    publication_record is not None
                    and publication_record.status.value == "FAILED"
                ):
                    break
                publication = GitHubPublisher(
                    store=store,
                    client=client,
                    token_provider=token_provider,
                    lock_root=self.config.lock_root,
                    api_url=self.config.api_url,
                ).publish_one(publication_id)
                if publication.status not in {"COMPLETED", "NO_CHANGES"}:
                    raise RuntimeError(f"publication {publication.status.lower()}")
            if result.message == "busy":
                break
            if result.phase != WorkflowPhase.AWAITING_PUBLICATION:
                now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
                if not store.is_thread_runnable(thread_id, now=now):
                    break

    def run(self) -> None:
        instance_lock = ServerInstanceLock(self.config.db)
        instance_lock.acquire()
        poll_client = poll_auth = poll_store = None
        executor = ThreadPoolExecutor(
            max_workers=self.config.workers, thread_name_prefix="sweforge"
        )
        try:
            poll_client, poll_auth = self.client_factory(self.config)
            poll_store = SQLiteGitHubStore(self.config.db)
            try:
                self._poll(poll_client, poll_store)
            except Exception:
                if self.config.once:
                    raise
                # A transient GitHub outage must not terminate the dispatcher.
                pass
            self._submit(executor, poll_store)
            if self.config.once:
                while True:
                    self._reap()
                    self._submit(executor, poll_store)
                    with self._futures_lock:
                        active = bool(self._futures)
                    now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
                    if not active and not poll_store.runnable_thread_ids(now=now):
                        break
                    self.stop_event.wait(0.05)
                return
            last_poll = time.monotonic()
            while not self.stop_event.wait(
                min(0.25, max(self.config.poll_interval, 0.25))
            ):
                self._reap()
                if time.monotonic() - last_poll >= self.config.poll_interval:
                    try:
                        self._poll(poll_client, poll_store)
                    except Exception:
                        pass
                    last_poll = time.monotonic()
                self._submit(executor, poll_store)
        finally:
            self.stop_event.set()
            executor.shutdown(wait=True, cancel_futures=False)
            for resource in (poll_store, poll_client, poll_auth):
                if resource is not None and hasattr(resource, "close"):
                    resource.close()
            instance_lock.close()
