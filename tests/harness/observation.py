"""The single bundle every invariant predicate reads.

The point of this module is that one predicate serves both layers. Offline it
reads a FakeGitHub call ledger; live it reads back over REST. A predicate never
learns which, so an invariant proven deterministically is the same invariant
checked against real GitHub -- there is no second implementation to drift.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@runtime_checkable
class GitHubFacts(Protocol):
    """What predicates may ask about GitHub, in either layer."""

    def comments(self) -> list[dict]:
        """Every comment currently visible on the thread's surfaces."""

    def comments_matching(self, token: str) -> list[dict]:
        """Comments whose body contains a marker token."""

    def pull_requests(self) -> list[dict]:
        """Every pull request visible for the repository."""

    def pull_requests_for_branch(self, head: str) -> list[dict]:
        """Pull requests whose head branch matches."""

    def create_comment_calls(self) -> int:
        """How many comment creations were *attempted*."""

    def create_pull_request_calls(self) -> int:
        """How many pull-request creations were *attempted*."""


class LedgerGitHubFacts:
    """Offline facts, read from a FakeGitHub call ledger.

    Attempt counts come from the ledger rather than from final state, because
    idempotency invariants must distinguish reusing an existing PR from
    creating a second one -- both leave exactly one PR behind.
    """

    def __init__(self, fake: Any) -> None:
        self._fake = fake

    def comments(self) -> list[dict]:
        return list(self._fake.created)

    def comments_matching(self, token: str) -> list[dict]:
        return [
            item for item in self._fake.created if token in (item.get("body") or "")
        ]

    def pull_requests(self) -> list[dict]:
        return list(self._fake.pulls)

    def pull_requests_for_branch(self, head: str) -> list[dict]:
        return [item for item in self._fake.pulls if item.get("head") == head]

    def create_comment_calls(self) -> int:
        return len(self._fake.calls("create_comment")) + len(
            self._fake.calls("create_review_comment_reply")
        )

    def create_pull_request_calls(self) -> int:
        return len(self._fake.calls("create_pull_request"))


class RestGitHubFacts:
    """Live facts, read back over a real GitHubClient.

    Attempt counts are not recoverable from REST: GitHub reports what exists,
    not how many times creation was tried. Live scenarios must therefore assert
    idempotency from the event log, which does record every attempt.
    """

    def __init__(self, client: Any, repo: Any, issue_number: int, base: str) -> None:
        self._client = client
        self._repo = repo
        self._issue_number = issue_number
        self._base = base

    def comments(self) -> list[dict]:
        return list(self._client.comments(self._repo, self._issue_number))

    def comments_matching(self, token: str) -> list[dict]:
        return [item for item in self.comments() if token in (item.get("body") or "")]

    def pull_requests(self) -> list[dict]:
        return list(self._client.pull_requests(self._repo, head="", base=self._base))

    def pull_requests_for_branch(self, head: str) -> list[dict]:
        return list(self._client.pull_requests(self._repo, head=head, base=self._base))

    def create_comment_calls(self) -> int:
        raise NotImplementedError(
            "REST cannot report creation attempts; assert idempotency from the "
            "event log in live scenarios"
        )

    def create_pull_request_calls(self) -> int:
        raise NotImplementedError(
            "REST cannot report creation attempts; assert idempotency from the "
            "event log in live scenarios"
        )


@dataclass(frozen=True, slots=True)
class GitFacts:
    """Repository facts read from a real git checkout, never inferred."""

    repo: Path
    origin: Path | None = None

    def branches(self) -> list[str]:
        out = _git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=self.repo
        )
        return [line for line in out.splitlines() if line]

    def commits(self, ref: str = "HEAD") -> list[str]:
        out = _git("rev-list", ref, cwd=self.repo)
        return [line for line in out.splitlines() if line]

    def commit_messages(self, ref: str = "HEAD") -> list[str]:
        out = _git("log", "--format=%s", ref, cwd=self.repo)
        return [line for line in out.splitlines() if line]

    def is_clean(self) -> bool:
        return not _git("status", "--porcelain", cwd=self.repo)

    def forced_updates_available(self) -> bool:
        """Whether force-pushes are observable here at all.

        Bare repositories disable ``core.logAllRefUpdates`` by default, and a
        real GitHub origin exposes no reflog whatsoever.
        """
        if self.origin is None:
            return False
        try:
            return (
                _git("config", "--get", "core.logAllRefUpdates", cwd=self.origin)
                == "true"
            )
        except subprocess.CalledProcessError:
            return False

    def forced_updates(self, ref: str) -> list[str]:
        """Reflog entries on the origin recording a non-fast-forward update.

        Raises rather than returning an empty list when the reflog is
        unavailable: an empty list reads as "no force-push happened", and
        reporting absence of evidence as evidence of absence is exactly the
        failure this campaign keeps finding. Live scenarios must assert force
        from the event log's BRANCH_PUSHED.forced field instead.
        """
        if not self.forced_updates_available():
            raise RuntimeError(
                "force-push is not observable here (no origin, or reflog "
                "disabled); assert from BRANCH_PUSHED.forced in the event log"
            )
        # The "forced-update" text only appears in a client-side fetch reflog;
        # a receiving repository just records "push". Detect it structurally
        # instead: a force-push is a non-fast-forward move, so the new commit
        # is not a descendant of the old one.
        out = _git("reflog", "show", ref, "--format=%gd %H %gs", cwd=self.origin)
        forced: list[str] = []
        entries = [line.split(" ", 2) for line in out.splitlines() if line]
        shas = [item[1] for item in entries]
        # reflog is newest-first; each entry's predecessor is the next one down
        for index, (selector, new_sha, _subject) in enumerate(entries):
            if index + 1 >= len(shas):
                continue  # oldest entry: ref creation has no predecessor
            old_sha = shas[index + 1]
            try:
                _git("merge-base", "--is-ancestor", old_sha, new_sha, cwd=self.origin)
            except subprocess.CalledProcessError:
                forced.append(f"{selector} {old_sha[:12]}..{new_sha[:12]}")
        return forced


@dataclass(frozen=True, slots=True)
class Observation:
    """Everything an invariant may read, assembled identically per layer."""

    events: list[dict] = field(default_factory=list)
    store: Any = None
    thread_ids: frozenset[str] = frozenset()
    repo_ids: frozenset[int] = frozenset()
    git: GitFacts | None = None
    github: GitHubFacts | None = None
    # Paths outside the worktree whose content must not change, mapped to the
    # sha256 recorded before the scenario ran. Confinement is only checkable
    # against something planted deliberately.
    outside_markers: tuple[tuple[str, str], ...] = ()
    # Filled in by M1c (probe MCP server) and M1b (fault registry). Left
    # untyped rather than guessing an interface that does not exist yet.
    probes: Any = None
    faults: Any = None
    # Bounded failure paths this scenario actually drove to their limit, as
    # {path_id: {"observed": bool, "actual": int, "expected": int}}. E4 reads
    # this. A bound is only evidence when the run reached it, so a scenario
    # records what it saw rather than what the constant says.
    bounded_paths: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record_bound(self, path_id: str, *, actual: int, expected: int) -> None:
        """Record that a bounded path was driven to a measured limit."""
        self.bounded_paths[path_id] = {
            "observed": True,
            "actual": actual,
            "expected": expected,
        }

    def events_of(self, kind: str) -> list[dict]:
        return [item for item in self.events if item.get("kind") == kind]

    def events_for_thread(self, thread_id: str) -> list[dict]:
        return [item for item in self.events if item.get("thread_id") == thread_id]


def plant_outside_markers(*paths: Path) -> tuple[tuple[str, str], ...]:
    """Record the current digest of paths that a confined run must not touch."""
    import hashlib

    recorded = []
    for path in paths:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        recorded.append((str(path), digest))
    return tuple(recorded)
