"""One-shot execution of one routed GitHub SourceEvent."""

import argparse
import sys
from pathlib import Path

from .execution import SQLiteCheckpointer, execute_one
from .github_store import SQLiteGitHubStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute one routed GitHub SourceEvent with a Deep Agent"
    )
    parser.add_argument(
        "--db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument(
        "--checkpoints",
        type=Path,
        default=Path("~/.sweforge/checkpoints.sqlite").expanduser(),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("~/.sweforge/workspaces").expanduser(),
    )
    parser.add_argument(
        "--lock-root", type=Path, default=Path("~/.sweforge/locks").expanduser()
    )
    parser.add_argument(
        "--repo-path",
        action="append",
        required=True,
        metavar="OWNER/REPO=PATH",
        help="trusted local checkout mapping (repeatable)",
    )
    parser.add_argument("--model", required=True)
    return parser


def _repo_paths(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in result:
            raise ValueError("--repo-path must be unique OWNER/REPO=PATH mappings")
        result[name] = Path(path).expanduser().resolve()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = None
    checkpoints = None
    try:
        mappings = _repo_paths(args.repo_path)
        store = SQLiteGitHubStore(args.db)
        checkpoints = SQLiteCheckpointer(args.checkpoints)
        result = execute_one(
            store=store,
            model=args.model,
            repo_paths=mappings,
            workspace_root=args.workspace_root,
            lock_root=args.lock_root,
            checkpointer=checkpoints.saver,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-github-execute: {exc}", file=sys.stderr)
        return 2
    finally:
        if checkpoints is not None:
            checkpoints.close()
        if store is not None:
            store.close()

    if not result.has_work:
        print(f"execution status: {result.status}")
        return 3
    event = result.event
    assert event is not None
    print(f"event key: {event.event_key}")
    print(f"thread ID: {event.thread_id}")
    print(f"repository: {event.repo_full_name}")
    print(f"issue number: {event.issue_number}")
    print(f"workspace path: {result.workspace.path if result.workspace else '(none)'}")
    print(f"branch: {result.workspace.branch_name if result.workspace else '(none)'}")
    print(f"workspace: {'created' if result.workspace_created else 'reused'}")
    print(f"execution status: {result.status}")
    if result.status == "SUCCEEDED":
        print("\nAgent response:\n")
        print(result.response)
        print("\nChanged files:")
        print("\n".join(f"- {path}" for path in result.changed_files) or "- None")
        print("\nDiff from thread base:\n")
        print(result.diff or "(no diff)")
        return 0
    print(f"error: {result.error or 'execution failed'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
