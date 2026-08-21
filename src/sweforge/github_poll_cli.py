"""CLI for one durable GitHub polling pass."""

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv("SWEFORGE_GITHUB_TOKEN")
    if not token:
        print(
            "sweforge-github-poll: SWEFORGE_GITHUB_TOKEN is required",
            file=sys.stderr,
        )
        return 2
    if args.initial_lookback_minutes < 0:
        print("sweforge-github-poll: lookback must be non-negative", file=sys.stderr)
        return 2

    client = HttpxGitHubClient(token, api_url=args.api_url)
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

    print(f"repositories polled: {result.repositories}")
    print(f"events discovered: {result.discovered}")
    print(f"events newly persisted: {result.persisted}")
    print(f"issue threads resolved: {result.issue_threads}")
    print(f"unrouted PR events: {result.unrouted_pr_events}")
    print("secrets: not logged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
