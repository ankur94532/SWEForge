"""One-shot CLI for publishing a successful IssueThread execution."""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .github_auth import DEFAULT_API_VERSION, GitHubAppAuthenticator
from .github_client import GitHubAPIError, HttpxGitHubClient
from .github_publisher import GitHubPublisher
from .github_store import SQLiteGitHubStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one successful SWEForge execution"
    )
    parser.add_argument(
        "--db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument(
        "--lock-root", type=Path, default=Path("~/.sweforge/locks").expanduser()
    )
    parser.add_argument(
        "--retry", metavar="EVENT_KEY", help="retry one FAILED publication"
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
    client_id = os.getenv("SWEFORGE_GITHUB_APP_CLIENT_ID") or os.getenv(
        "SWEFORGE_GITHUB_CLIENT_ID"
    )
    app_id = os.getenv("SWEFORGE_GITHUB_APP_ID")
    key_path = os.getenv("SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH")
    if not (client_id or app_id) or not key_path:
        print(
            "sweforge-github-publish: GitHub App credentials are required",
            file=sys.stderr,
        )
        return 2
    authenticator = GitHubAppAuthenticator(
        app_id,
        key_path,
        client_id=client_id,
        api_url=args.api_url,
        api_version=args.api_version,
    )
    client = HttpxGitHubClient(
        token_provider=authenticator, api_url=args.api_url, api_version=args.api_version
    )
    store = SQLiteGitHubStore(args.db)
    try:
        if args.retry:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            store.retry_publication(args.retry, now=now)
        result = GitHubPublisher(
            store=store,
            client=client,
            token_provider=authenticator,
            lock_root=args.lock_root,
            api_url=args.api_url,
        ).publish_one(args.retry)
    except (GitHubAPIError, OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-github-publish: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        client.close()
        authenticator.close()
    print(f"publication status: {result.status}")
    if result.event_key:
        print(f"event key: {result.event_key}")
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    return (
        0
        if result.status in {"COMPLETED", "NO_CHANGES"}
        else (3 if result.status in {"NO_WORK", "BUSY"} else 1)
    )


if __name__ == "__main__":
    raise SystemExit(main())
