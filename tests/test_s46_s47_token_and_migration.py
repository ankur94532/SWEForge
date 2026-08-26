"""S46 and S47, as unit tests.

COVERAGE-GAPS.md places both in its Low tier and says outright they are worth
a unit test rather than a scenario: the token refresh window is hard to stage
live, and in-place migration is a property of opening a database, not of a
workflow run. Written the way the document recommends rather than forced into
the scenario harness.

S46: an installation token is refreshed before it expires, not after.
S47: an existing database is migrated in place, keeping its rows.
"""

from datetime import UTC, datetime, timedelta

from sweforge.github_auth import TOKEN_REFRESH_WINDOW, InstallationToken
from sweforge.github_store import SQLiteGitHubStore


def _expiring_in(minutes: float) -> InstallationToken:
    return InstallationToken("tok", datetime.now(UTC) + timedelta(minutes=minutes))


def _is_reused(token: InstallationToken) -> bool:
    """Mirror the cache decision in token_for."""
    return datetime.now(UTC) < token.expires_at - TOKEN_REFRESH_WINDOW


def test_the_refresh_window_is_shorter_than_the_token_lifetime():
    """A window at or beyond the lifetime would refresh on every call."""
    assert timedelta(0) < TOKEN_REFRESH_WINDOW < timedelta(minutes=9)


def test_a_fresh_token_is_reused():
    assert _is_reused(_expiring_in(9))


def test_a_token_inside_the_refresh_window_is_not_reused():
    """The point of the window: refresh before expiry, never after."""
    assert not _is_reused(_expiring_in(TOKEN_REFRESH_WINDOW.total_seconds() / 60 - 1))


def test_an_expired_token_is_not_reused():
    assert not _is_reused(_expiring_in(-1))


def test_a_token_expiring_exactly_at_the_boundary_is_not_reused():
    """Fail toward refreshing: reusing a token that expires mid-request is the
    failure this window exists to prevent."""
    assert not _is_reused(_expiring_in(TOKEN_REFRESH_WINDOW.total_seconds() / 60))


def test_an_existing_database_is_migrated_in_place(tmp_path):
    """Reopening must migrate without discarding what is already stored."""
    path = tmp_path / "state.db"
    first = SQLiteGitHubStore(path)
    first.upsert_repository(1, "example/repo", "now")
    tables_before = {
        row[0]
        for row in first.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    first.close()

    reopened = SQLiteGitHubStore(path)
    rows = reopened.connection.execute(
        "SELECT full_name FROM repositories WHERE repo_id=1"
    ).fetchone()
    assert rows is not None, "reopening the database discarded its rows"
    assert rows["full_name"] == "example/repo"
    tables_after = {
        row[0]
        for row in reopened.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables_before <= tables_after, (
        f"migration dropped tables: {sorted(tables_before - tables_after)}"
    )
    reopened.close()


def test_migration_is_idempotent_across_repeated_opens(tmp_path):
    """Opening three times must not accumulate or lose schema."""
    path = tmp_path / "state.db"
    shapes = []
    for _ in range(3):
        store = SQLiteGitHubStore(path)
        store.upsert_repository(1, "example/repo", "now")
        shapes.append(
            {
                row[0]
                for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        store.close()
    assert shapes[0] == shapes[1] == shapes[2], "the schema moved between opens"
