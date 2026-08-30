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
        "--discard-worktree",
        action="store_true",
        help="remove the temporary worktree after the run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    selected = list(argv) if argv is not None else sys.argv[1:]
    if selected[:1] == ["repo"]:
        from .repo_config_cli import main as repo_main

        return repo_main(selected[1:])
    if selected[:1] == ["secret"]:
        from .repo_secret_cli import main as secret_main

        return secret_main(selected[1:])
    if selected[:1] in (["workflow"], ["skill"], ["tool"], ["mcp"]):
        from .repo_admin_cli import main as admin_main

        return admin_main(selected)
    args = build_parser().parse_args(selected)
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
        if args.discard_worktree:
            workspace.cleanup()
            print(f"\nWorktree discarded: {workspace.path}")
        else:
            print(f"\nWorktree retained at: {workspace.path}")
        return 0
    except (WorkspaceError, ValueError) as exc:
        print(f"sweforge: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"sweforge: agent failed: {exc}", file=sys.stderr)
        if workspace is not None:
            print(f"Worktree retained at: {workspace.path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
