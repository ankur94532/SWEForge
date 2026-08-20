"""Command-line interface for SWEForge V0."""

import argparse
import sys
from pathlib import Path

from .agent import run_task
from .config import Config
from .workspace import Workspace, WorkspaceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Deep Agent in a Git worktree")
    parser.add_argument("repository", type=Path)
    parser.add_argument("task")
    parser.add_argument("--model")
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="retain the temporary worktree and print its path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = None
    try:
        config = Config.from_environment(args.model)
        workspace = Workspace.create(args.repository)
        response = run_task(
            model=config.model, worktree=str(workspace.path), task=args.task
        )
        changed = workspace.changed_files()
        diff = workspace.diff()
        print("Agent response:\n")
        print(response)
        print("\nChanged files:")
        print("\n".join(f"- {path}" for path in changed) or "- None")
        print(f"\nBase commit: {workspace.base_commit}")
        print("\nGit diff:\n")
        print(diff or "(no diff)")
        if args.keep_worktree:
            print(f"\nWorktree retained at: {workspace.path}")
        else:
            workspace.cleanup()
        return 0
    except (WorkspaceError, ValueError) as exc:
        print(f"sweforge: {exc}", file=sys.stderr)
        return 2
    finally:
        if workspace is not None and not args.keep_worktree and not workspace._removed:
            try:
                workspace.cleanup()
            except Exception as exc:  # pragma: no cover - best-effort error path
                print(f"sweforge: cleanup failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
