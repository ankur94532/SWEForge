"""Operator controls for local IssueThread execution records."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .execution import recover_stale, utc_timestamp
from .github_store import ExecutionStatus, SQLiteGitHubStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage SWEForge executions")
    parser.add_argument(
        "--db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument(
        "--lock-root", type=Path, default=Path("~/.sweforge/locks").expanduser()
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    recover = subparsers.add_parser("recover-stale")
    recover.add_argument("--older-than-minutes", type=float, default=5.0)
    retry = subparsers.add_parser("retry")
    retry.add_argument("event_key")
    skip = subparsers.add_parser("skip")
    skip.add_argument("event_key")
    skip.add_argument("--reason", default="skipped by operator")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteGitHubStore(args.db)
    try:
        if args.command == "status":
            for record in store.execution_records():
                print(
                    f"{record.event_key} | {record.thread_id} | "
                    f"{record.status.value} | "
                    f"attempt={record.attempt_count} | started={record.started_at} | "
                    f"completed={record.completed_at or '-'}"
                )
            return 0
        if args.command == "recover-stale":
            if args.older_than_minutes < 0:
                raise ValueError("older-than-minutes must be non-negative")
            recovered = recover_stale(
                store=store,
                lock_root=args.lock_root,
                older_than_seconds=int(args.older_than_minutes * 60),
            )
            print(f"interrupted: {len(recovered)}")
            for event_key in recovered:
                print(event_key)
            return 0
        if args.command == "retry":
            status = store.retry_execution(args.event_key)
            print(f"{args.event_key}: {status.value}")
            return 0
        if args.command == "skip":
            store.skip_execution(
                args.event_key,
                completed_at=utc_timestamp(datetime.now(UTC)),
                reason=args.reason,
            )
            print(f"{args.event_key}: {ExecutionStatus.SKIPPED.value}")
            return 0
        raise ValueError("unknown command")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-github-execution: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
