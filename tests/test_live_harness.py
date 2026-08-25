"""The live harness must refuse before it acts, never after.

These cover the paths that keep a misconfigured live run from reaching the
wrong repository. They need no network: every case is a refusal.
"""

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, unique_marker

from acceptance.runner.allowlist import (
    PrimaryRepositoryRefused,
    RepositoryNotAllowlisted,
)

SANDBOX = "ankur94532/sweforge-acceptance-sandbox"
PRIMARY = "ankur94532/SWEForge"


def test_an_unset_allowlist_refuses_rather_than_defaulting(monkeypatch):
    """Fail-closed: no target is not the same as any target."""
    monkeypatch.delenv("SWEFORGE_ACCEPTANCE_REPOS", raising=False)
    with pytest.raises(LiveCredentialsUnavailable, match="unset"):
        live_repository()


def test_an_ambiguous_allowlist_refuses(monkeypatch):
    """Two targets means a scenario could reach the wrong one."""
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", f"{SANDBOX},owner/other")
    monkeypatch.delenv("SWEFORGE_PRIMARY_REPOS", raising=False)
    with pytest.raises(LiveCredentialsUnavailable, match="unambiguous"):
        live_repository()


def test_a_primary_target_is_refused_even_when_allowlisted(monkeypatch):
    """The property that protects the real repository."""
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", PRIMARY)
    monkeypatch.setenv("SWEFORGE_PRIMARY_REPOS", PRIMARY)
    with pytest.raises(PrimaryRepositoryRefused):
        live_repository()


def test_an_unlisted_target_is_refused(monkeypatch):
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", " , ")
    with pytest.raises(LiveCredentialsUnavailable):
        live_repository()


def test_a_single_allowlisted_target_is_returned(monkeypatch):
    """Positive control: the guard must not refuse everything."""
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", SANDBOX)
    monkeypatch.setenv("SWEFORGE_PRIMARY_REPOS", PRIMARY)
    assert live_repository() == SANDBOX


def test_markers_are_unique_per_run(monkeypatch):
    """A rerun must not adopt a previous run's issue."""
    assert unique_marker("S1") != unique_marker("S1")
    assert unique_marker("S1").startswith("sweforge-acceptance-s1-")


def test_a_non_primary_unlisted_repo_raises_the_allowlist_error(monkeypatch):
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", "owner/not-the-sandbox")
    monkeypatch.setenv("SWEFORGE_PRIMARY_REPOS", PRIMARY)
    # The single name is allowlisted by definition here, so reach past
    # live_repository to the guard the mutating helpers call.
    from acceptance.runner.allowlist import check_live_target

    with pytest.raises(RepositoryNotAllowlisted):
        check_live_target("owner/some-third-repo")
