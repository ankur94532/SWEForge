"""Support for LIVE_GITHUB scenarios: a real repository, a real API.

A live body reuses its L1 twin's store, engine and invariants and swaps only
the GitHub client, so the environment is the single variable (ROADMAP section
J). What differs operationally is that events arrive by polling GitHub rather
than being handed to the store directly, which is the integration this layer
exists to prove.

Every mutating entry point here calls check_live_target first. The allowlist is
fail-closed and PRIMARY is checked independently, so a misconfigured run is
refused rather than misdirected.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from acceptance.runner.allowlist import check_live_target
from sweforge.github_client import HttpxGitHubClient
from sweforge.github_poller import GitHubPoller


class LiveCredentialsUnavailable(RuntimeError):
    """No usable GitHub token; a live scenario must not silently degrade."""


def live_token() -> str:
    """Return a GitHub token, preferring the environment over the gh CLI."""
    for name in ("SWEFORGE_GITHUB_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveCredentialsUnavailable("could not run `gh auth token`") from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise LiveCredentialsUnavailable(
            "no GitHub token: set SWEFORGE_GITHUB_TOKEN or run `gh auth login`"
        )
    return token


def live_client() -> HttpxGitHubClient:
    return HttpxGitHubClient(live_token())


def allowlisted_repositories() -> list[str]:
    """Every repository the allowlist permits, in declared order."""
    raw = os.environ.get("SWEFORGE_ACCEPTANCE_REPOS", "")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        raise LiveCredentialsUnavailable(
            "SWEFORGE_ACCEPTANCE_REPOS is unset; source acceptance/campaign.env"
        )
    for name in names:
        check_live_target(name)
    return names


def live_repository() -> str:
    """The one repository a single-target scenario acts on.

    SWEFORGE_ACCEPTANCE_REPO names it explicitly. Without that, a sole
    allowlist entry is unambiguous and is used; several entries are refused
    rather than guessed, so widening the allowlist for a cross-repo scenario
    cannot silently redirect every other scenario.
    """
    names = allowlisted_repositories()
    designated = os.environ.get("SWEFORGE_ACCEPTANCE_REPO", "").strip()
    if designated:
        if designated not in names:
            raise LiveCredentialsUnavailable(
                f"SWEFORGE_ACCEPTANCE_REPO={designated} is not allowlisted {names}"
            )
        check_live_target(designated)
        return designated
    if len(names) > 1:
        raise LiveCredentialsUnavailable(
            f"the live target must be unambiguous; allowlist names {names}. "
            "Set SWEFORGE_ACCEPTANCE_REPO to choose."
        )
    return names[0]


def live_repository_pair() -> tuple[str, str]:
    """Two distinct allowlisted repositories, for cross-repository scenarios."""
    names = allowlisted_repositories()
    if len(names) < 2:
        raise LiveCredentialsUnavailable(
            f"a cross-repository scenario needs two allowlisted repositories; "
            f"the allowlist names {names}"
        )
    return names[0], names[1]


def unique_marker(scenario_id: str) -> str:
    """A per-run token, so a rerun never adopts a previous run's issue."""
    return f"sweforge-acceptance-{scenario_id.lower()}-{uuid.uuid4().hex[:12]}"


def create_issue(client: HttpxGitHubClient, full_name: str, *, title: str, body: str):
    """Open a real issue on the allowlisted repository."""
    check_live_target(full_name)
    repo = client.repository(full_name)
    return client._request(
        "POST",
        f"/repos/{full_name}/issues",
        token_scope=full_name,
        json={"title": title, "body": body},
    ).json(), repo


def comment(client: HttpxGitHubClient, full_name: str, number: int, body: str) -> dict:
    check_live_target(full_name)
    repo = client.repository(full_name)
    return client.create_comment(repo, number, body)


def clone_source(full_name: str, destination: Path, token: str) -> Path:
    """Clone the live repository so execution has real code to change."""
    check_live_target(full_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    subprocess.run(
        ["git", "clone", "--quiet", url, str(destination)],
        check=True,
        capture_output=True,
    )
    return destination


def poll_until(
    poller: GitHubPoller,
    full_name: str,
    predicate,
    *,
    attempts: int = 10,
    delay: float = 3.0,
):
    """Poll until `predicate` holds, then return its value.

    GitHub is eventually consistent and its `since` filter has second
    granularity, so a single poll can legitimately miss a just-created event.
    Raises rather than returning a falsy value: a live scenario that cannot
    observe its own event must fail, not quietly assert nothing.
    """
    last = None
    for _ in range(attempts):
        poller.poll([full_name])
        last = predicate()
        if last:
            return last
        time.sleep(delay)
    raise AssertionError(
        f"polled {attempts} times over ~{attempts * delay:.0f}s without "
        f"observing the expected state on {full_name}"
    )


def open_live_thread(
    scenario_id: str,
    root_dir,
    *,
    body: str,
    planner=None,
    reviewer=None,
    clarification_classifier=None,
):
    """Open a real issue and return a world already bound to its thread.

    Every live body repeats this preamble: create the issue, build a world
    whose source is a clone of the real repository, poll until the thread
    exists, and point the observation at REST rather than the fake's ledger.
    """
    from harness.observation import RestGitHubFacts
    from harness.world import World
    from sweforge.github_poller import GitHubPoller

    full_name = live_repository()
    client = live_client()
    token = live_token()
    marker = unique_marker(scenario_id)

    issue, repo = create_issue(
        client,
        full_name,
        title=f"[acceptance] {marker}",
        body=f"{body}\n\nmarker: {marker}",
    )
    number = int(issue["number"])

    world = World.build(
        root_dir,
        repo_id=repo.repo_id,
        full_name=full_name,
        client=client,
        planner=planner,
        reviewer=reviewer,
        clarification_classifier=clarification_classifier,
        source_clone_url=f"https://x-access-token:{token}@github.com/{full_name}.git",
    )
    poller = GitHubPoller(client, world.store)

    def thread_for_issue():
        row = world.store.connection.execute(
            "SELECT thread_id FROM issue_threads WHERE repo_id=? AND issue_number=?",
            (repo.repo_id, number),
        ).fetchone()
        return row["thread_id"] if row else None

    thread_id = poll_until(poller, full_name, thread_for_issue)
    # Polling is repository-wide, so a shared sandbox legitimately yields
    # threads from earlier acceptance issues. Declaring what polling found
    # keeps INV-THREAD-ISOLATION able to catch a thread this world never saw.
    for row in world.store.connection.execute(
        "SELECT thread_id FROM issue_threads WHERE repo_id=?", (repo.repo_id,)
    ):
        world.thread_ids.add(row["thread_id"])
    world.github_facts = RestGitHubFacts(client, repo, number, repo.default_branch)
    return LiveThread(
        world=world,
        client=client,
        repo=repo,
        full_name=full_name,
        issue_number=number,
        thread_id=thread_id,
        poller=poller,
        marker=marker,
    )


class LiveThread:
    """One live issue plus everything a body needs to drive it."""

    def __init__(
        self, *, world, client, repo, full_name, issue_number, thread_id, poller, marker
    ) -> None:
        self.world = world
        self.client = client
        self.repo = repo
        self.full_name = full_name
        self.issue_number = issue_number
        self.thread_id = thread_id
        self.poller = poller
        self.marker = marker

    def say(self, text: str) -> None:
        """Post a real comment and poll until it is stored."""
        comment(self.client, self.full_name, self.issue_number, text)
        store = self.world.store

        def stored():
            row = store.connection.execute(
                "SELECT event_key FROM source_events WHERE thread_id=? AND body=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self.thread_id, text),
            ).fetchone()
            return row["event_key"] if row else None

        self.last_event_key = poll_until(self.poller, self.full_name, stored)

    def approve(self):
        self.say("@agent approve")
        return self.world.engine.approve(event_key=self.last_event_key)


def open_additional_issue(live: LiveThread, body: str) -> str:
    """Open a second real issue in the same world and return its thread id.

    Concurrency scenarios need two threads in one repository, sharing one
    store so their isolation is observable.
    """
    marker = unique_marker("extra")
    issue, _repo = create_issue(
        live.client,
        live.full_name,
        title=f"[acceptance] {marker}",
        body=f"{body}\n\nmarker: {marker}",
    )
    number = int(issue["number"])
    store = live.world.store

    def thread_for_issue():
        row = store.connection.execute(
            "SELECT thread_id FROM issue_threads WHERE repo_id=? AND issue_number=?",
            (live.repo.repo_id, number),
        ).fetchone()
        return row["thread_id"] if row else None

    thread_id = poll_until(live.poller, live.full_name, thread_for_issue)
    live.world.thread_ids.add(thread_id)
    return thread_id
