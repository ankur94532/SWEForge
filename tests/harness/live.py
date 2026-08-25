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


def live_repository() -> str:
    """The single allowlisted live target, or a refusal.

    Reading the allowlist rather than taking a caller's argument keeps a
    scenario from naming its own target.
    """
    raw = os.environ.get("SWEFORGE_ACCEPTANCE_REPOS", "")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        raise LiveCredentialsUnavailable(
            "SWEFORGE_ACCEPTANCE_REPOS is unset; source acceptance/campaign.env"
        )
    if len(names) > 1:
        raise LiveCredentialsUnavailable(
            f"the live target must be unambiguous; allowlist names {names}"
        )
    check_live_target(names[0])
    return names[0]


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
