"""CLI for one durable GitHub polling pass."""

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

from .github_auth import DEFAULT_API_VERSION, GitHubAppAuthenticator
from .github_client import GitHubAPIError, HttpxGitHubClient
from .github_poller import GitHubPoller
from .github_store import SQLiteGitHubStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll GitHub repositories for @agent")
    parser.add_argument("--repo", action="append", required=True, help="owner/name")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("~/.sweforge/state.db").expanduser(),
        help="SQLite state database path",
    )
    parser.add_argument(
        "--initial-lookback-minutes", type=int, default=10, metavar="MINUTES"
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("SWEFORGE_GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument(
        "--api-version",
        default=os.getenv("SWEFORGE_GITHUB_API_VERSION", DEFAULT_API_VERSION),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app_client_id = os.getenv("SWEFORGE_GITHUB_APP_CLIENT_ID") or os.getenv(
        "SWEFORGE_GITHUB_CLIENT_ID"
    )
    app_id = os.getenv("SWEFORGE_GITHUB_APP_ID")
    private_key_path = os.getenv("SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH")
    legacy_token = os.getenv("SWEFORGE_GITHUB_TOKEN")
    if args.initial_lookback_minutes < 0:
        print("sweforge-github-poll: lookback must be non-negative", file=sys.stderr)
        return 2

    authenticator = None
    if app_client_id or app_id or private_key_path:
        if not (app_client_id or app_id) or not private_key_path:
            print(
                "sweforge-github-poll: both GitHub App credentials are required",
                file=sys.stderr,
            )
            return 2
        authenticator = GitHubAppAuthenticator(
            app_id,
            private_key_path,
            client_id=app_client_id,
            api_url=args.api_url,
            api_version=args.api_version,
        )
        client = HttpxGitHubClient(
            token_provider=authenticator,
            api_url=args.api_url,
            api_version=args.api_version,
        )
    elif legacy_token:
        client = HttpxGitHubClient(
            legacy_token, api_url=args.api_url, api_version=args.api_version
        )
    else:
        print(
            "sweforge-github-poll: GitHub App credentials are required "
            "(legacy SWEFORGE_GITHUB_TOKEN is also supported)",
            file=sys.stderr,
        )
        return 2
    store = SQLiteGitHubStore(args.db)
    try:
        result = GitHubPoller(
            client,
            store,
            initial_lookback=timedelta(minutes=args.initial_lookback_minutes),
        ).poll(args.repo)
    except (GitHubAPIError, OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-github-poll: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        client.close()
        if authenticator is not None:
            authenticator.close()

    print(f"repositories polled: {result.repositories}")
    print(f"events discovered: {result.events_discovered}")
    print(f"events newly persisted: {result.events_persisted}")
    print(f"threads newly created: {result.threads_created}")
    print(f"events routed: {result.events_routed}")
    print(f"PR events unrouted: {result.pr_events_unrouted}")
    print("secrets: not logged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
