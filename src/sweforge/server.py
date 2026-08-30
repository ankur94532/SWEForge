"""Single-process durable SWEForge dispatcher."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .agent_trace import (
    AgentTracer,
    AgentTraceSink,
    TerminalTraceSink,
    TraceContext,
    bounded_text,
)
from .capabilities import load_capability_registry
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
from .workflow_controller import DeclarativeWorkflowController
from .workflow_driver import DeepAgentWorkflowDriver
from .workflow_learning import WorkflowLearningService
from .workflow_spec import (
    BUILTIN_WORKFLOW_TOOLS,
    DEFAULT_WORKFLOW,
    WorkflowSpec,
    load_workflow_spec,
)

MAX_DECLARATIVE_DRIVER_FAILURES = 3


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
    workflow_spec: Path | None = None
    sandbox_provider: str | None = None
    # Written once the singleton lock is held and polling is wired, so a test
    # harness can wait on a real signal instead of sleeping and hoping.
    ready_file: Path | None = None
    unsafe_local_shell: bool = False
    api_url: str = "https://api.github.com"
    api_version: str = DEFAULT_API_VERSION
    workers: int = 4
    poll_interval: float = 30.0
    max_ticks: int = 20
    initial_lookback_minutes: int = 10
    once: bool = False
    debug_agent: bool = False
    debug_agent_tools: bool = False

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
    return bounded_text(f"{type(exc).__name__}: {exc}", 1_000)


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
        driver_factory: Callable[..., object] | None = None,
        publisher_factory: Callable[..., object] | None = None,
        learning_factory: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
        trace_sink: AgentTraceSink | None = None,
    ) -> None:
        if config.workers < 1 or config.max_ticks < 1 or config.poll_interval < 0:
            raise ValueError(
                "workers and max-ticks must be positive; "
                "poll interval cannot be negative"
            )
        if not config.repo_paths or set(config.repositories) != set(config.repo_paths):
            raise ValueError("repositories and repo-path mappings must match")
        self.config = config
        self.capability_registry = (
            load_capability_registry(config.capabilities_config)
            if config.capabilities_config
            else None
        )
        self.workflow_spec: WorkflowSpec = (
            load_workflow_spec(
                config.workflow_spec,
                known_tools=(
                    set(BUILTIN_WORKFLOW_TOOLS)
                    | set(self.capability_registry.known_tool_names())
                    if self.capability_registry
                    else BUILTIN_WORKFLOW_TOOLS
                ),
            )
            if config.workflow_spec is not None
            else DEFAULT_WORKFLOW
        )
        self.client_factory = client_factory or _credentials
        self.poller_factory = poller_factory or GitHubPoller
        self.worker_runner = worker_runner
        self.driver_factory = driver_factory
        self.publisher_factory = publisher_factory or GitHubPublisher
        self._production_learning = learning_factory is None
        self.learning_factory = learning_factory or WorkflowLearningService
        self.now = now or (lambda: datetime.now(UTC))
        debug_enabled = config.debug_agent or config.debug_agent_tools
        self.tracer = (
            AgentTracer(
                trace_sink or TerminalTraceSink(),
                include_tool_payloads=config.debug_agent_tools,
            )
            if debug_enabled
            else None
        )
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
        if self.tracer is not None:
            self.tracer.emit(
                "POLL",
                f"start repositories={len(self.config.repositories)}",
                TraceContext(),
            )
        result = poller.poll(self.config.repositories)
        if self.tracer is not None:
            self.tracer.emit(
                "POLL",
                (
                    f"end discovered={getattr(result, 'events_discovered', '?')} "
                    f"persisted={getattr(result, 'events_persisted', '?')} "
                    f"routed={getattr(result, 'events_routed', '?')}"
                ),
                TraceContext(),
            )

    def _submit(self, executor: ThreadPoolExecutor, store: SQLiteGitHubStore) -> None:
        now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        with self._futures_lock:
            active = set(self._futures.values())
            for thread_id in store.runnable_thread_ids(now=now):
                if len(self._futures) >= self.config.workers:
                    break
                if thread_id in active:
                    continue
                if self.tracer is not None:
                    self.tracer.emit(
                        "DISPATCH", "queued", self._trace_context(store, thread_id)
                    )
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
        controller = None
        trace_context = (
            self._trace_context(store, thread_id) if self.tracer is not None else None
        )
        if self.tracer is not None:
            assert trace_context is not None
            self.tracer.emit("WORKER START", "running", trace_context)
        try:
            if self.worker_runner is not None:
                self.worker_runner(thread_id)
                return
            client, authenticator = self.client_factory(self.config)
            checkpoints = SQLiteCheckpointer(self.config.checkpoints)
            memory = SQLiteMemoryStore(self.config.memory_db)
            capability_registry = self.capability_registry
            sandbox = resolve_sandbox_provider(self.config.sandbox_provider)

            def production_driver_factory(
                runtime, workflow_cycle_id, worktree, cycle_spec
            ):
                return DeepAgentWorkflowDriver(
                    runtime=runtime,
                    workflow_cycle_id=workflow_cycle_id,
                    spec=cycle_spec,
                    store=store,
                    client=client,
                    worktree=worktree,
                    planning_model=self.config.planning,
                    execution_model=self.config.execution,
                    validation_model=self.config.review,
                    checkpointer=checkpoints.saver,
                    memory_store=memory.store,
                    capability_registry=capability_registry,
                    sandbox_backend_provider=sandbox,
                    secure_execution=not self.config.unsafe_local_shell,
                    unsafe_local_shell=self.config.unsafe_local_shell,
                    tracer=self.tracer,
                )

            controller = DeclarativeWorkflowController(
                store=store,
                client=client,
                spec=self.workflow_spec,
                spec_ref=(
                    str(self.config.workflow_spec.resolve())
                    if self.config.workflow_spec
                    else "builtin:default"
                ),
                repo_paths=self.config.repo_paths,
                workspace_root=self.config.workspace_root,
                lock_root=self.config.lock_root,
                driver_factory=self.driver_factory or production_driver_factory,
                clock=self._timestamp,
                tracer=self.tracer,
            )
            learning_kwargs = {
                "store": store,
                "memory_store": memory.store,
                "memory_model": self.config.memory,
                "resolution_model": self.config.resolution,
                "lock_root": self.config.lock_root,
                "clock": self._timestamp,
            }
            if self.tracer is not None and self._production_learning:
                learning_kwargs["tracer"] = self.tracer
            learning = self.learning_factory(**learning_kwargs)
            self._drain_workflow(
                thread_id=thread_id,
                store=store,
                controller=controller,
                client=client,
                token_provider=authenticator,
                learning=learning,
            )
            store.clear_dispatcher_failure(thread_id)
        except Exception as exc:
            if self.tracer is not None:
                assert trace_context is not None
                self.tracer.emit(
                    "WORKER ERROR", _safe_dispatch_error(exc), trace_context
                )
            now = self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
            store.record_dispatcher_failure(
                thread_id, now=now, error=_safe_dispatch_error(exc)
            )
            failure = store.dispatcher_failure(thread_id)
            if (
                controller is not None
                and failure is not None
                and failure["failure_count"] >= MAX_DECLARATIVE_DRIVER_FAILURES
            ):
                cycle = controller.runtime.cycle_for_thread(thread_id)
                if cycle is not None and cycle.status.value == "ACTIVE":
                    controller.runtime.fail_active_task(
                        cycle.workflow_cycle_id,
                        reason=_safe_dispatch_error(exc),
                    )
            raise
        finally:
            if self.tracer is not None:
                assert trace_context is not None
                self.tracer.emit("WORKER END", "stopped", trace_context)
            for resource in (memory, checkpoints, store, client, authenticator):
                if resource is not None and hasattr(resource, "close"):
                    resource.close()

    def _drain_workflow(
        self,
        *,
        thread_id: str,
        store: SQLiteGitHubStore,
        controller: DeclarativeWorkflowController,
        client: GitHubClient,
        token_provider: object,
        learning: WorkflowLearningService,
    ) -> None:
        """Drain bounded generic-runtime ticks for one IssueThread."""
        for _ in range(self.config.max_ticks):
            if learning.process_one(thread_id):
                continue
            result = controller.advance(thread_id)
            if result.status == "AWAITING_PUBLICATION":
                publication_id = store.eligible_publication_id(thread_id)
                if not publication_id:
                    break
                publication_record = store.publication_for_id(publication_id)
                if (
                    publication_record is not None
                    and publication_record.status.value == "FAILED"
                ):
                    break
                publication = self.publisher_factory(
                    store=store,
                    client=client,
                    token_provider=token_provider,
                    lock_root=self.config.lock_root,
                    api_url=self.config.api_url,
                ).publish_one(publication_id)
                if publication.status not in {"COMPLETED", "NO_CHANGES"}:
                    raise RuntimeError(f"publication {publication.status.lower()}")
                store.finalize_publication(publication_id, now=self._timestamp())
                continue
            if result.status in {"BUSY", "FAILED", "IDLE"}:
                break
            if not store.is_thread_runnable(thread_id, now=self._timestamp()):
                break

    def _timestamp(self) -> str:
        return self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _trace_context(store: SQLiteGitHubStore, thread_id: str) -> TraceContext:
        try:
            thread = store.issue_thread(thread_id)
        except Exception:
            return TraceContext(thread_id=thread_id)
        if thread is None:
            return TraceContext(thread_id=thread_id)
        return TraceContext(
            thread_id=thread_id,
            repo=str(thread["repo_full_name"]),
            issue_number=int(thread["issue_number"]),
        )

    def _signal_ready(self) -> None:
        """Announce readiness only after the lock is held and polling ran once."""
        if self.config.ready_file is None:
            return
        target = Path(self.config.ready_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{os.getpid()}\n", encoding="utf-8")

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
            self._signal_ready()
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
