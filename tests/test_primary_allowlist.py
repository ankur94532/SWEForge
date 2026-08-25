"""PRIMARY protection must be mechanical, fail closed, and unoverridable.

K8 requires a negative test: the harness must refuse a PRIMARY target. These
are that test, plus the cases that would quietly disarm it.
"""

import pytest

from acceptance.runner.allowlist import (
    ALLOWLIST_ENV,
    PRIMARY_ENV,
    Allowlist,
    PrimaryRepositoryRefused,
    RepositoryNotAllowlisted,
    check_live_target,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(PRIMARY_ENV, raising=False)


def test_refuses_a_primary_repository(monkeypatch):
    monkeypatch.setenv(PRIMARY_ENV, "acme/production")
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/sweforge-acceptance-1")
    with pytest.raises(PrimaryRepositoryRefused, match="PRIMARY"):
        check_live_target("acme/production")


def test_an_allowlist_entry_cannot_override_primary(monkeypatch):
    """Naming a repo in both lists must refuse, not permit."""
    monkeypatch.setenv(PRIMARY_ENV, "acme/production")
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/production acme/sweforge-acceptance-1")
    with pytest.raises(PrimaryRepositoryRefused):
        check_live_target("acme/production")


def test_fails_closed_when_no_allowlist_is_configured():
    """A missing allowlist must block the run, not permit everything."""
    with pytest.raises(RepositoryNotAllowlisted, match="not in the acceptance"):
        check_live_target("acme/anything")


def test_refuses_a_repository_that_was_never_allowlisted(monkeypatch):
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/sweforge-acceptance-1")
    with pytest.raises(RepositoryNotAllowlisted):
        check_live_target("acme/sweforge-acceptance-2")


def test_permits_an_allowlisted_repository(monkeypatch):
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/sweforge-acceptance-1")
    check_live_target("acme/sweforge-acceptance-1")


def test_accepts_comma_or_space_separated_configuration(monkeypatch):
    monkeypatch.setenv(ALLOWLIST_ENV, "a/one, a/two  a/three")
    for name in ("a/one", "a/two", "a/three"):
        check_live_target(name)


def test_an_unnamed_target_is_refused():
    """A blank name must not slip through as 'nothing to check'."""
    for value in ("", "   ", None):
        with pytest.raises(RepositoryNotAllowlisted, match="no repository was named"):
            check_live_target(value)


def test_whitespace_around_a_name_does_not_bypass_primary(monkeypatch):
    monkeypatch.setenv(PRIMARY_ENV, "acme/production")
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/production")
    with pytest.raises(PrimaryRepositoryRefused):
        check_live_target("  acme/production  ")


def test_code_level_primary_set_applies_even_with_no_environment(monkeypatch):
    """An empty or mistyped env var must not disarm the in-code denylist."""
    import acceptance.runner.allowlist as module

    monkeypatch.setattr(module, "PRIMARY_REPOS", frozenset({"acme/production"}))
    monkeypatch.setenv(ALLOWLIST_ENV, "acme/production")
    with pytest.raises(PrimaryRepositoryRefused):
        Allowlist.from_env().check("acme/production")
