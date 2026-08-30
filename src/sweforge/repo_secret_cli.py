"""Operator CLI for encrypted repository-scoped runtime credentials."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .github_store import SQLiteGitHubStore
from .repo_config import RepoConfigRegistry
from .repo_secrets import RepoSecretStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage encrypted SWEForge repository credentials"
    )
    parser.add_argument(
        "--state-db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set")
    set_command.add_argument("repo", metavar="OWNER/REPO")
    set_command.add_argument("name")
    set_command.add_argument(
        "--stdin",
        action="store_true",
        help="read the value from standard input instead of a hidden prompt",
    )
    list_command = commands.add_parser("list")
    list_command.add_argument("repo", metavar="OWNER/REPO")
    delete = commands.add_parser("delete")
    delete.add_argument("repo", metavar="OWNER/REPO")
    delete.add_argument("name")
    check = commands.add_parser("check")
    check.add_argument("repo", metavar="OWNER/REPO")
    return parser


def _required_references(registry: RepoConfigRegistry, repo_id: int) -> set[str]:
    generation = registry.current_generation(repo_id)
    if generation is None:
        raise ValueError("repository has no installed configuration")
    manifest = generation.manifest
    return {
        secret_name
        for item in [*manifest["scripts"], *manifest["mcp"]]
        for references in (item.get("secret_env", {}), item.get("secret_headers", {}))
        for secret_name in references.values()
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = SQLiteGitHubStore(args.state_db.expanduser())
    try:
        repo_id = state.repository_id_for_full_name(args.repo)
        if repo_id is None:
            raise ValueError(f"repository has not been observed: {args.repo}")
        secrets = RepoSecretStore.from_environment(state, required=True)
        assert secrets is not None
        if args.command == "set":
            value = (
                sys.stdin.readline().rstrip("\r\n")
                if args.stdin
                else getpass.getpass("Value: ")
            )
            secrets.set(repo_id, args.name, value)
            print(f"{args.name}\tconfigured")
        elif args.command == "list":
            for name in secrets.list_names(repo_id):
                print(f"{name}\tconfigured")
        elif args.command == "delete":
            if not secrets.delete(repo_id, args.name):
                raise ValueError("repository credential does not exist")
            print(f"{args.name}\tdeleted")
        elif args.command == "check":
            required = _required_references(RepoConfigRegistry(state), repo_id)
            configured = set(secrets.list_names(repo_id))
            missing = sorted(required - configured)
            for name in missing:
                print(f"{name}\tmissing")
            if not missing:
                print("All required repository credentials are configured.")
            return 1 if missing else 0
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge secret: {exc}", file=sys.stderr)
        return 2
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
