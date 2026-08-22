"""Trusted operator CLI for repository-scoped Deep Agents skills."""

import argparse
import sys
from pathlib import Path

from .github_store import SQLiteGitHubStore
from .repo_memory import DEFAULT_MEMORY_PATH, SQLiteMemoryStore
from .skills import (
    list_repo_skills,
    put_repo_skill,
    remove_repo_skill,
    show_repo_skill,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage SWEForge repository skills")
    parser.add_argument(
        "--state-db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument("--memory-db", type=Path, default=DEFAULT_MEMORY_PATH)
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    show = commands.add_parser("show")
    show.add_argument("path")
    put = commands.add_parser("put")
    put.add_argument("path")
    put.add_argument("--file", type=Path, required=True)
    remove = commands.add_parser("remove")
    remove.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = SQLiteGitHubStore(args.state_db.expanduser())
    memory = None
    try:
        repo_id = state.repository_id_for_full_name(args.repo)
        if repo_id is None:
            raise ValueError(f"repository has not been observed: {args.repo}")
        memory = SQLiteMemoryStore(args.memory_db)
        if args.command == "list":
            sys.stdout.write("\n".join(list_repo_skills(memory.store, repo_id)))
        elif args.command == "show":
            content = show_repo_skill(memory.store, repo_id, args.path)
            if content is None:
                raise ValueError("skill file not found")
            sys.stdout.write(content)
        elif args.command == "put":
            put_repo_skill(memory.store, repo_id, args.path, args.file.read_text())
        elif args.command == "remove":
            remove_repo_skill(memory.store, repo_id, args.path)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-repo-skills: {exc}", file=sys.stderr)
        return 2
    finally:
        if memory is not None:
            memory.close()
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
