"""One isolated universe per scenario.

Every scenario gets its own origin repository, checkout, three SQLite
databases, workspace and lock roots, and event log. Nothing is shared, so a
scenario cannot contaminate the next and failures stay attributable.
"""

import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acceptance.probes.ledger import ProbeLedger
from harness.github_fake import FakeGitHub
from harness.observation import (
    GitFacts,
    LedgerGitHubFacts,
    Observation,
    plant_outside_markers,
)
from sweforge.events import LOG_ENV, STRICT_ENV, read_log
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import SQLiteGitHubStore
from sweforge.workflow import WorkflowEngine


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class World:
    """An isolated SWEForge universe rooted at one temporary directory."""

    root: Path
    repo: RepositoryRef
    store: SQLiteGitHubStore
    github: FakeGitHub
    engine: WorkflowEngine
    source: Path
    origin: Path
    event_log: Path
    thread_ids: set[str] = field(default_factory=set)
    probe_ledger_path: Path | None = None
    outside_markers: tuple[tuple[str, str], ...] = ()
    # A live body sets this to RestGitHubFacts once its issue exists; the
    # issue number is not known until then. Left unset, observation() reads
    # the fake's recorded calls as every L1 scenario does.
    github_facts: Any = None
    # A cross-repository scenario legitimately touches more than the world's
    # own repository; declaring them keeps INV-REPO-ISOLATION able to catch a
    # repository this world never saw.
    extra_repo_ids: frozenset[int] = frozenset()
    _clock_tick: int = 0

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        root: Path,
        *,
        repo_id: int = 1,
        full_name: str = "example/repo",
        with_origin: bool = False,
        planner=None,
        reviewer=None,
        memory_learner=None,
        clarification_classifier=None,
        permissions: dict[str, str] | None = None,
        client=None,
        source_clone_url: str | None = None,
    ) -> "World":
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source"
        if source_clone_url is not None:
            # A live body clones the real repository as its source, so the
            # thread workspace the workflow creates already points at the real
            # code. Cloning elsewhere and redirecting execution later produces
            # "repository mapping conflicts with thread workspace".
            _git("clone", "--quiet", source_clone_url, str(source), cwd=root)
            _git("config", "user.email", "harness@example.com", cwd=source)
            _git("config", "user.name", "Harness", cwd=source)
        else:
            source.mkdir()
            _git("init", "-q", "-b", "main", cwd=source)
            _git("config", "user.email", "harness@example.com", cwd=source)
            _git("config", "user.name", "Harness", cwd=source)
            (source / "README.md").write_text("base\n")
            _git("add", "README.md", cwd=source)
            _git("commit", "-qm", "base", cwd=source)

        origin = root / "origin.git"
        if with_origin:
            _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=root)
            # Bare repositories disable reflog, which is the only way a local
            # force-push is observable. See GitFacts.forced_updates_available.
            _git("config", "core.logAllRefUpdates", "true", cwd=origin)
            _git("remote", "add", "origin", str(origin), cwd=source)
            _git("push", "-q", "origin", "main", cwd=source)

        store = SQLiteGitHubStore(root / "state.db")
        repo = RepositoryRef(repo_id, full_name)
        store.upsert_repository(repo.repo_id, repo.full_name, "now")
        world_clock_holder: dict = {}
        # A LIVE_GITHUB scenario injects a real GitHubClient here; every other
        # layer gets the fake. The seam is the client alone, so a live body
        # reuses the same store, engine and invariants as its L1 twin and the
        # environment is the only thing that differs.
        if client is not None:
            if permissions is not None:
                raise ValueError(
                    "permissions apply to the fake only; a live client's "
                    "permissions come from GitHub"
                )
            github = client
        else:
            github = FakeGitHub(
                default_repo_id=repo_id,
                clock=lambda: world_clock_holder["clock"](),
                # Approval requires repository write access; scenarios that are
                # not about authorization get a writer by default.
                permissions=permissions,
            )

        world = cls(
            root=root,
            repo=repo,
            store=store,
            github=github,
            engine=None,  # type: ignore[arg-type]
            source=source,
            origin=origin if with_origin else None,  # type: ignore[arg-type]
            event_log=root / "events.jsonl",
        )
        # Model roles are injected at engine construction, not per advance()
        # call, so a scenario chooses its doubles when it builds its world.
        world_clock_holder["clock"] = world._clock
        world.engine = WorkflowEngine(
            store=store,
            client=github,
            clock=world._clock,
            planner=planner,
            reviewer=reviewer,
            memory_learner=memory_learner,
            clarification_classifier=clarification_classifier,
        )
        return world

    def _clock(self) -> str:
        """Monotonic, deterministic timestamps — never the wall clock.

        Scenario assertions depend on ordering, and a real clock makes a run
        depend on how fast the machine is.
        """
        self._clock_tick += 1
        # Roll properly into minutes and hours: a naive tick//60 formatting
        # produces "00:60:00" past 3600 ticks, which is not a valid timestamp
        # and compares wrongly against real event times.
        return self._stamp(self._clock_tick)

    @staticmethod
    def _stamp(total: int) -> str:
        return (
            f"2026-01-01T{total // 3600 % 24:02d}:"
            f"{total // 60 % 60:02d}:{total % 60:02d}Z"
        )

    def later(self, seconds: int = 3600) -> str:
        """A timestamp safely after everything the clock has produced so far.

        Scenarios must not hardcode times relative to a synthetic clock: how
        many ticks a drive consumes is an implementation detail.
        """
        return self._stamp(self._clock_tick + max(1, seconds))

    # -- environment --------------------------------------------------------

    @contextmanager
    def activate(self):
        """Point the event log at this world, strictly, and restore after."""
        previous = {key: os.environ.get(key) for key in (LOG_ENV, STRICT_ENV)}
        os.environ[LOG_ENV] = str(self.event_log)
        os.environ[STRICT_ENV] = "1"
        try:
            yield self
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # -- ingestion ----------------------------------------------------------

    def event(
        self,
        source_id: str,
        body: str,
        when: str,
        *,
        issue_number: int = 7,
        subject_kind: SubjectKind = SubjectKind.ISSUE,
        author_login: str = "octocat",
    ) -> SourceEvent:
        return SourceEvent(
            repo_id=self.repo.repo_id,
            repo_full_name=self.repo.full_name,
            source_kind=SourceKind.ISSUE_COMMENT,
            source_id=source_id,
            source_updated_at=when,
            source_created_at=when,
            subject_kind=subject_kind,
            subject_number=issue_number,
            author_login=author_login,
            body=body,
            html_url=None,
        )

    def ingest(self, *events: SourceEvent, stream: str = "issue_comments") -> None:
        self.store.record_batch(
            self.repo.repo_id,
            stream,
            list(events),
            since="now",
            etag=None,
            polled_at="now",
        )
        for item in events:
            row = self.store.source_event(item.event_key)
            if row and row["thread_id"]:
                self.thread_ids.add(row["thread_id"])

    # -- driving ------------------------------------------------------------

    def tick(self, thread_id: str, **kwargs: Any):
        """Advance one durable worker tick.

        execute_authorized() receives ONLY execute_kwargs, so model, repo_paths
        and workspace_root must appear there as well as at advance()'s top
        level. A caller's execute_kwargs is merged over the defaults rather
        than replacing them, so overriding `runner` does not silently drop the
        rest.
        """
        self.thread_ids.add(thread_id)
        execute_defaults = {
            "model": "scripted",
            "repo_paths": {self.repo.full_name: self.source},
            "workspace_root": self.root / "workspaces",
            "lock_root": self.root / "locks",
        }
        overrides = dict(kwargs.pop("execute_kwargs", {}) or {})
        defaults = {
            "model": "scripted",
            "repo_paths": {self.repo.full_name: self.source},
            "workspace_root": self.root / "workspaces",
            "execute_kwargs": {**execute_defaults, **overrides},
        }
        return self.engine.advance(thread_id=thread_id, **{**defaults, **kwargs})

    def drive(
        self, thread_id: str, *, until=None, max_ticks: int = 20, **kwargs: Any
    ) -> list:
        """Tick until a phase is reached, refusing to spin forever.

        Exhausting max_ticks raises rather than returning what it managed: a
        scenario that silently stopped short would assert against a half-built
        state and could pass for the wrong reason.
        """
        results = []
        for _ in range(max_ticks):
            result = self.tick(thread_id, **kwargs)
            results.append(result)
            if until is not None and result.phase == until:
                return results
        if until is not None:
            phases = [str(item.phase) for item in results]
            raise RuntimeError(
                f"drive() never reached {until} for {thread_id} in {max_ticks} "
                f"ticks; saw {phases}"
            )
        return results

    # -- observation --------------------------------------------------------

    def plant_markers(self, *paths: Path) -> None:
        """Declare paths outside the worktree that must remain untouched."""
        self.outside_markers = plant_outside_markers(*paths)

    def observation(self) -> Observation:
        events = read_log(self.event_log) if self.event_log.exists() else []
        return Observation(
            events=events,
            store=self.store,
            thread_ids=frozenset(self.thread_ids),
            repo_ids=frozenset({self.repo.repo_id}) | self.extra_repo_ids,
            git=GitFacts(repo=self.source, origin=self.origin),
            github=self.github_facts or LedgerGitHubFacts(self.github),
            outside_markers=self.outside_markers,
            probes=(
                ProbeLedger(self.probe_ledger_path)
                if self.probe_ledger_path is not None
                else None
            ),
        )
