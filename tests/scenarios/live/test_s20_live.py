"""S20 LIVE_GITHUB: an @agent mention on an unmapped PR attaches to nothing.

A pull request SWEForge never opened has no thread. A mention inside its review
comments must fail closed: no thread attachment, no durable identity, no
permit, no publication. Proving it live matters because the routing decision
depends on how GitHub actually classifies the subject, not on how the fake
labels a synthetic event.
"""

import subprocess

import pytest
from harness.live import (
    LiveCredentialsUnavailable,
    live_client,
    live_repository,
    live_token,
    poll_until,
    unique_marker,
)
from harness.observation import Observation, RestGitHubFacts
from harness.scenario import Layer, run, scenario
from harness.world import World

from acceptance.runner.allowlist import check_live_target
from sweforge.github_poller import GitHubPoller


def _run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@scenario(
    "S20",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="A live @agent mention on an unmapped PR creates no thread.",
)
def s20_live_unmapped_pr_mention(root_dir) -> Observation:
    full_name = live_repository()
    check_live_target(full_name)
    client = live_client()
    token = live_token()
    marker = unique_marker("S20")
    url = f"https://x-access-token:{token}@github.com/{full_name}.git"

    world = World.build(
        root_dir,
        repo_id=client.repository(full_name).repo_id,
        full_name=full_name,
        client=client,
        source_clone_url=url,
    )
    source = world.root / "source"
    branch = f"acceptance/{marker}"
    _run("git", "checkout", "-q", "-b", branch, cwd=source)
    (source / f"{marker}.txt").write_text("unmapped pr subject\n")
    _run("git", "add", "-A", cwd=source)
    _run("git", "commit", "-qm", f"acceptance subject {marker}", cwd=source)
    _run("git", "push", "-q", "origin", branch, cwd=source)

    repo = client.repository(full_name)
    pull = client.create_pull_request(
        repo,
        head=branch,
        base=repo.default_branch,
        title=f"[acceptance] unmapped PR {marker}",
        body="Opened by the acceptance campaign; SWEForge never created this.",
    )
    number = int(pull["number"])

    # A review comment, not an issue comment: this is the stream whose subject
    # classification the routing decision depends on.
    head_sha = _run("git", "rev-parse", "HEAD", cwd=source).stdout.strip()
    client._request(
        "POST",
        f"/repos/{full_name}/pulls/{number}/comments",
        token_scope=full_name,
        json={
            "body": f"@agent please look at this\n\nmarker: {marker}",
            "commit_id": head_sha,
            "path": f"{marker}.txt",
            "line": 1,
            "side": "RIGHT",
        },
    )

    # Facts must come from REST: the fake's ledger cannot read a real client,
    # and INV-NO-PUBLICATION would report itself unevaluable rather than pass.
    world.github_facts = RestGitHubFacts(client, repo, number, repo.default_branch)

    with world.activate():
        poller = GitHubPoller(client, world.store)

        def mention_stored():
            row = world.store.connection.execute(
                "SELECT event_key, thread_id FROM source_events "
                "WHERE body LIKE ? ORDER BY rowid DESC LIMIT 1",
                (f"%{marker}%",),
            ).fetchone()
            return row["event_key"] if row else None

        event_key = poll_until(poller, full_name, mention_stored)
        row = world.store.source_event(event_key)
        assert row is not None, "the mention was not recorded at all"
        assert not row["thread_id"], "an unmapped PR mention was routed to a thread"
        attached = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_threads WHERE issue_number=?", (number,)
        ).fetchone()[0]
        assert attached == 0, "an unmapped PR mention created durable identity"
    # No thread of our own: declare none rather than borrowing another's.
    return world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S20", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s20_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S20", tmp_path / "s20-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
