"""S11 LIVE_GITHUB: two real repositories keep separate state.

Work in one repository must not serialize behind work in another. Two distinct
allowlisted repositories make that observable: separate repo ids, separate
threads, separate worktrees, and no shared lock between them.
"""

from pathlib import Path

import pytest
from harness.live import (
    LiveCredentialsUnavailable,
    create_issue,
    live_client,
    live_repository_pair,
    live_token,
    poll_until,
    unique_marker,
)
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation, RestGitHubFacts
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_poller import GitHubPoller
from sweforge.github_store import WorkflowPhase

PLAN = "1. touch the notes file\n2. run the tests"


def _writing_runner(**kwargs):
    (Path(kwargs["worktree"]) / "NOTES.md").write_text("cross repo\n")
    return "wrote NOTES.md"


@scenario(
    "S11",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-REPO-ISOLATION"],
    description="Two live repositories keep separate state and never serialize.",
)
def s11_live_cross_repo_concurrency(root_dir) -> Observation:
    first_name, second_name = live_repository_pair()
    client = live_client()
    token = live_token()
    first_repo = client.repository(first_name)
    second_repo = client.repository(second_name)
    assert first_repo.repo_id != second_repo.repo_id, "the pair resolved to one repo"

    world = World.build(
        root_dir,
        repo_id=first_repo.repo_id,
        full_name=first_name,
        client=client,
        planner=ScriptedPlanner(plans=[PLAN, PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT", "ACCEPT"]),
        source_clone_url=(
            f"https://x-access-token:{token}@github.com/{first_name}.git"
        ),
    )
    # The second repository is durable state too, so the store must know it.
    world.store.upsert_repository(second_repo.repo_id, second_repo.full_name, "now")

    threads: dict[str, str] = {}
    numbers: dict[str, int] = {}
    with world.activate():
        poller = GitHubPoller(client, world.store)
        for name, repo in ((first_name, first_repo), (second_name, second_repo)):
            marker = unique_marker("S11")
            issue, _ = create_issue(
                client,
                name,
                title=f"[acceptance] {marker}",
                body=f"@agent update the notes\n\nmarker: {marker}",
            )
            numbers[name] = int(issue["number"])

            def thread_for(repo_id=repo.repo_id, number=numbers[name]):
                row = world.store.connection.execute(
                    "SELECT thread_id FROM issue_threads "
                    "WHERE repo_id=? AND issue_number=?",
                    (repo_id, number),
                ).fetchone()
                return row["thread_id"] if row else None

            threads[name] = poll_until(poller, name, thread_for)

        for row in world.store.connection.execute(
            "SELECT thread_id FROM issue_threads"
        ):
            world.thread_ids.add(row["thread_id"])
        world.extra_repo_ids = frozenset({second_repo.repo_id})
        world.github_facts = RestGitHubFacts(
            client, first_repo, numbers[first_name], first_repo.default_branch
        )

        assert threads[first_name] != threads[second_name], (
            "two repositories shared a thread"
        )
        # Only the first repository has a cloned source here, so only it is
        # driven to execution; the property under test is that the second
        # repository's durable state exists independently and never blocks it.
        world.drive(
            threads[first_name],
            until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
            max_ticks=10,
        )
        from harness.live import comment

        comment(client, first_name, numbers[first_name], "@agent approve")

        def approval():
            row = world.store.connection.execute(
                "SELECT event_key FROM source_events WHERE thread_id=? "
                "AND body LIKE '%@agent approve%' ORDER BY rowid DESC LIMIT 1",
                (threads[first_name],),
            ).fetchone()
            return row["event_key"] if row else None

        world.engine.approve(event_key=poll_until(poller, first_name, approval))
        world.drive(
            threads[first_name],
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=12,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _writing_runner,
                "checkpointer": object(),
            },
        )
        repos = {
            row["repo_id"]
            for row in world.store.connection.execute(
                "SELECT DISTINCT repo_id FROM issue_threads"
            )
        }
        assert repos == {first_repo.repo_id, second_repo.repo_id}, (
            f"the two repositories did not both persist: {repos}"
        )
    return world.observation()


def test_registered_for_the_live_layer():
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S11", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s11_live(tmp_path):
    try:
        live_repository_pair()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live pair unavailable: {exc}")
    result = run("S11", tmp_path / "s11-live", layer=Layer.LIVE_GITHUB)
    assert result.ok, "\n" + result.report()
