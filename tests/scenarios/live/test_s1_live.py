"""S1 LIVE_GITHUB: issue to accepted review against the real API.

The L1 twin proves the state machine. This proves the same machine against
GitHub itself: a real issue, events arriving by polling rather than by direct
insertion, and a real approval comment. Per ROADMAP section J the environment
is the only variable, so the models stay scripted and this run neither needs
nor contends for the model provider.
"""

from pathlib import Path

import pytest
from harness.live import (
    LiveCredentialsUnavailable,
    comment,
    create_issue,
    live_client,
    live_repository,
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

PLAN = "1. widen the bulk discount test\n2. run the tests"


def _writing_runner(**kwargs):
    target = Path(kwargs["worktree"]) / "NOTES.md"
    target.write_text("acceptance run\n")
    return "added NOTES.md"


@scenario(
    "S1",
    layer=Layer.LIVE_GITHUB,
    invariants=[
        "INV-ONE-ROOT",
        "INV-PLAN-CANONICAL",
        "INV-PERMIT-BOUND",
        "INV-ONE-INITIAL",
        "INV-ATTEMPT-TERMINAL",
        "INV-PROVENANCE",
        "INV-THREAD-ISOLATION",
        "INV-REPO-ISOLATION",
    ],
    description="Live issue to accepted review: one plan, one permit, one INITIAL.",
)
def s1_live_happy_path(root_dir) -> Observation:
    full_name = live_repository()
    client = live_client()
    token = live_token()
    marker = unique_marker("S1")

    issue, repo = create_issue(
        client,
        full_name,
        title=f"[acceptance] {marker}",
        body=f"@agent fix the bulk discount boundary\n\nmarker: {marker}",
    )
    number = int(issue["number"])

    # The world's source is the real repository, so the thread workspace the
    # workflow creates already points at the real code.
    world = World.build(
        root_dir,
        repo_id=repo.repo_id,
        full_name=full_name,
        client=client,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
        source_clone_url=(f"https://x-access-token:{token}@github.com/{full_name}.git"),
    )

    with world.activate():
        poller = GitHubPoller(client, world.store)

        def thread_for_issue():
            row = world.store.connection.execute(
                "SELECT thread_id FROM issue_threads "
                "WHERE repo_id=? AND issue_number=?",
                (repo.repo_id, number),
            ).fetchone()
            return row["thread_id"] if row else None

        thread_id = poll_until(poller, full_name, thread_for_issue)
        # Polling is repository-wide, so a shared sandbox legitimately yields
        # threads from earlier acceptance issues. Declare what polling actually
        # discovered: INV-THREAD-ISOLATION then still catches an event naming a
        # thread this world never saw, while the single-plan, single-permit and
        # single-INITIAL invariants remain scoped to this run's own cycle.
        for row in world.store.connection.execute(
            "SELECT thread_id FROM issue_threads WHERE repo_id=?", (repo.repo_id,)
        ):
            world.thread_ids.add(row["thread_id"])
        assert thread_id in world.thread_ids
        world.github_facts = RestGitHubFacts(client, repo, number, repo.default_branch)

        world.drive(
            thread_id,
            until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL,
            max_ticks=10,
        )

        comment(client, full_name, number, "@agent approve")

        def approval_event():
            row = world.store.connection.execute(
                "SELECT event_key FROM source_events WHERE thread_id=? "
                "AND body LIKE '%@agent approve%' ORDER BY rowid DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            return row["event_key"] if row else None

        event_key = poll_until(poller, full_name, approval_event)
        permit = world.engine.approve(event_key=event_key)
        assert permit.thread_id == thread_id

        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=12,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": _writing_runner,
                "checkpointer": object(),
            },
        )
        review = world.store.execution_review_for_attempt(
            world.store.latest_attempt(thread_id, 1).attempt_id
        )
        assert review is not None and review.verdict == "ACCEPT"
    return world.observation()


def test_scenario_is_registered_for_the_live_layer():
    """Registration is checkable without touching GitHub."""
    from harness.scenario import SCENARIOS

    assert SCENARIOS[("S1", Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
def test_s1_live(tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run("S1", tmp_path / "s1-live")
    assert result.ok, "\n" + result.report()
