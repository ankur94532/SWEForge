"""Trusted operator CLI for repository-scoped memory."""

import argparse
import sys
from pathlib import Path

from .github_store import SQLiteGitHubStore
from .repo_memory import (
    DEFAULT_MEMORY_PATH,
    SQLiteMemoryStore,
    append_repo_memory,
    ensure_repo_memory,
    read_repo_memory,
    repo_memory_namespace,
    write_repo_memory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage SWEForge repository memory")
    parser.add_argument(
        "--state-db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument("--memory-db", type=Path, default=DEFAULT_MEMORY_PATH)
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    replace = commands.add_parser("replace")
    replace.add_argument("--file", type=Path, required=True)
    append = commands.add_parser("append")
    append.add_argument("--text", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_path = args.state_db.expanduser()
    if not state_path.exists():
        print(
            f"sweforge-repo-memory: state database not found: {state_path}",
            file=sys.stderr,
        )
        return 2
    state = SQLiteGitHubStore(state_path)
    memory = None
    try:
        repo_id = state.repository_id_for_full_name(args.repo)
        if repo_id is None:
            raise ValueError(f"repository has not been observed: {args.repo}")
        memory = SQLiteMemoryStore(args.memory_db)
        namespace = repo_memory_namespace(repo_id)
        ensure_repo_memory(memory.store, namespace)
        if args.command == "show":
            sys.stdout.write(read_repo_memory(memory.store, namespace) or "")
        elif args.command == "replace":
            write_repo_memory(memory.store, namespace, args.file.read_text())
        elif args.command == "append":
            append_repo_memory(memory.store, namespace, args.text)
        else:
            raise ValueError(f"unknown command: {args.command}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-repo-memory: {exc}", file=sys.stderr)
        return 2
    finally:
        if memory is not None:
            memory.close()
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
