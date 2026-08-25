"""World must give each scenario a genuinely isolated universe."""

import pytest
from harness.observation import GitHubFacts
from harness.world import World

from sweforge.events import LOG_ENV, EventKind, emit
from sweforge.github_store import WorkflowPhase


@pytest.fixture
def world(tmp_path):
    return World.build(tmp_path / "w1")


@pytest.fixture
def planning_world(tmp_path):
    """A world whose planner is a scripted double rather than a real model."""
    return World.build(
        tmp_path / "w2", planner=lambda **kwargs: "1. edit README\n2. run tests"
    )


def test_builds_a_real_repository_with_a_base_commit(world):
    assert (world.source / "README.md").read_text() == "base\n"
    assert world.observation().git.commit_messages() == ["base"]


def test_each_world_is_isolated_from_the_next(tmp_path):
    first = World.build(tmp_path / "a")
    second = World.build(tmp_path / "b")
    assert first.store.connection is not second.store.connection
    assert first.source != second.source
    assert first.event_log != second.event_log


def test_clock_is_deterministic_not_wall_clock(world):
    assert [world._clock() for _ in range(3)] == [
        "2026-01-01T00:00:01Z",
        "2026-01-01T00:00:02Z",
        "2026-01-01T00:00:03Z",
    ]


def test_activate_points_the_event_log_at_this_world_and_restores(world, monkeypatch):
    monkeypatch.setenv(LOG_ENV, "/somewhere/else")
    with world.activate():
        assert emit(EventKind.PLAN_CREATED, thread_id="t", plan_id="p") is not None
        assert world.event_log.exists()
    import os

    assert os.environ[LOG_ENV] == "/somewhere/else"


def test_activate_restores_an_unset_variable(world, monkeypatch):
    import os

    monkeypatch.delenv(LOG_ENV, raising=False)
    with world.activate():
        pass
    assert LOG_ENV not in os.environ


def test_ingest_records_events_and_tracks_their_threads(world):
    root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    world.ingest(root)
    assert world.store.source_event(root.event_key) is not None
    assert world.thread_ids


def test_observation_assembles_every_fact_source(world):
    root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    world.ingest(root)
    with world.activate():
        emit(EventKind.ROOT_INGESTED, thread_id=next(iter(world.thread_ids)))
    observation = world.observation()
    assert observation.store is world.store
    assert observation.repo_ids == frozenset({1})
    assert observation.thread_ids
    assert isinstance(observation.github, GitHubFacts)
    assert observation.events and observation.events[0]["kind"] == "ROOT_INGESTED"


def test_observation_has_no_events_before_anything_is_emitted(world):
    assert world.observation().events == []


def test_planted_markers_reach_the_observation(world, tmp_path):
    marker = tmp_path / "outside.txt"
    marker.write_text("keep\n")
    world.plant_markers(marker)
    assert world.observation().outside_markers


def test_origin_is_optional_and_enables_force_detection(tmp_path):
    without = World.build(tmp_path / "no-origin")
    assert not without.observation().git.forced_updates_available()
    with_origin = World.build(tmp_path / "with-origin", with_origin=True)
    assert with_origin.observation().git.forced_updates_available()


def test_drive_raises_rather_than_returning_a_half_built_state(planning_world):
    """Silently stopping short would let a scenario assert on partial state."""
    world = planning_world
    root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    world.ingest(root)
    thread_id = next(iter(world.thread_ids))
    with pytest.raises(RuntimeError, match="never reached"):
        world.drive(thread_id, until=WorkflowPhase.AWAITING_PUBLICATION, max_ticks=2)


def test_drive_returns_once_the_target_phase_is_reached(planning_world):
    world = planning_world
    root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    world.ingest(root)
    thread_id = next(iter(world.thread_ids))
    results = world.drive(
        thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=5
    )
    assert results[-1].phase == WorkflowPhase.WAITING_FOR_PLAN_APPROVAL
