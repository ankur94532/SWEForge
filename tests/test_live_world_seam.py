"""A LIVE_GITHUB scenario differs from its L1 twin only in the client.

The environment axis is independent of the model axis (ROADMAP §J), so a live
body must reuse the same store, engine and invariants and swap nothing but the
GitHub client. Without an injection seam every live scenario would have to
rebuild the world, and the two layers would drift.
"""

import pytest
from harness.github_fake import FakeGitHub
from harness.world import World


def test_the_default_world_uses_the_fake(tmp_path):
    world = World.build(tmp_path / "fake")
    assert isinstance(world.github, FakeGitHub)
    assert world.engine.client is world.github


def test_an_injected_client_replaces_the_fake(tmp_path):
    sentinel = FakeGitHub(default_repo_id=99)
    world = World.build(tmp_path / "live", client=sentinel)
    assert world.github is sentinel
    assert world.engine.client is sentinel, "the engine kept a different client"


def test_permissions_and_a_live_client_are_refused_together(tmp_path):
    """A live client's permissions come from GitHub. Accepting both would let
    a scenario believe it had scripted an authorization it cannot control."""
    with pytest.raises(ValueError, match="permissions apply to the fake"):
        World.build(
            tmp_path / "both",
            client=FakeGitHub(),
            permissions={"octocat": "read"},
        )


def test_the_injected_world_still_builds_a_working_store(tmp_path):
    """Positive control: the seam must not produce a half-built world."""
    world = World.build(tmp_path / "store", client=FakeGitHub())
    assert world.store is not None
    assert world.engine.store is world.store
