"""Authoritative durable GitHub workflow worker tick."""

import argparse
import os
import sys
from pathlib import Path

from .execution import SQLiteCheckpointer
from .github_auth import DEFAULT_API_VERSION, GitHubAppAuthenticator
from .github_client import HttpxGitHubClient
from .github_store import SQLiteGitHubStore
from .repo_memory import SQLiteMemoryStore
from .workflow import WorkflowEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advance one SWEForge workflow")
    parser.add_argument(
        "--db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    parser.add_argument(
        "--checkpoints",
        type=Path,
        default=Path("~/.sweforge/checkpoints.sqlite").expanduser(),
    )
    parser.add_argument(
        "--memory-db", type=Path, default=Path("~/.sweforge/memory.sqlite").expanduser()
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
        "--repo-path", action="append", required=True, metavar="OWNER/REPO=PATH"
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--model", help="legacy fallback for all workflow roles")
    parser.add_argument("--planning-model")
    parser.add_argument("--execution-model")
    parser.add_argument("--review-model")
    parser.add_argument(
        "--api-url",
        default=os.getenv("SWEFORGE_GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument(
        "--api-version",
        default=os.getenv("SWEFORGE_GITHUB_API_VERSION", DEFAULT_API_VERSION),
    )
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
    planning_model = args.planning_model or args.model
    execution_model = args.execution_model or args.model
    review_model = args.review_model or args.model
    if not all((planning_model, execution_model, review_model)):
        print(
            "sweforge-github-workflow: planning, execution, and review "
            "models are required",
            file=sys.stderr,
        )
        return 2
    client_id = os.getenv("SWEFORGE_GITHUB_APP_CLIENT_ID") or os.getenv(
        "SWEFORGE_GITHUB_CLIENT_ID"
    )
    app_id = os.getenv("SWEFORGE_GITHUB_APP_ID")
    key_path = os.getenv("SWEFORGE_GITHUB_APP_PRIVATE_KEY_PATH")
    if not (client_id or app_id) or not key_path:
        print(
            "sweforge-github-workflow: GitHub App credentials are required",
            file=sys.stderr,
        )
        return 2
    store = checkpoints = memory = authenticator = client = None
    try:
        mappings = _repo_paths(args.repo_path)
        authenticator = GitHubAppAuthenticator(
            app_id,
            key_path,
            client_id=client_id,
            api_url=args.api_url,
            api_version=args.api_version,
        )
        client = HttpxGitHubClient(
            token_provider=authenticator,
            api_url=args.api_url,
            api_version=args.api_version,
        )
        store = SQLiteGitHubStore(args.db)
        checkpoints = SQLiteCheckpointer(args.checkpoints)
        memory = SQLiteMemoryStore(args.memory_db)
        result = WorkflowEngine(store=store, client=client).advance(
            thread_id=args.thread_id,
            model=planning_model,
            review_model=review_model,
            repo_paths=mappings,
            workspace_root=args.workspace_root,
            memory_store=memory.store,
            execute_kwargs={
                "model": execution_model,
                "repo_paths": mappings,
                "workspace_root": args.workspace_root,
                "lock_root": args.lock_root,
                "checkpointer": checkpoints.saver,
                "memory_store": memory.store,
            },
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-github-workflow: {exc}", file=sys.stderr)
        return 1
    finally:
        for resource in (memory, checkpoints, store, client, authenticator):
            if resource is not None:
                resource.close()
    print(f"workflow phase: {result.phase.value}")
    if result.plan_id:
        print(f"plan ID: {result.plan_id}")
    if result.permit_id:
        print(f"permit ID: {result.permit_id}")
    if result.message:
        print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
