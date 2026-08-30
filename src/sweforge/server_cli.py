"""CLI for the long-running single-process SWEForge server."""

import argparse
import os
import signal
import sys
from pathlib import Path

from .github_auth import DEFAULT_API_VERSION
from .server import ServerConfig, SWEForgeServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the durable SWEForge dispatcher")
    parser.add_argument(
        "--repo-path", action="append", required=True, metavar="OWNER/REPO=PATH"
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
    parser.add_argument("--model")
    parser.add_argument("--planning-model")
    parser.add_argument("--execution-model")
    parser.add_argument("--review-model")
    parser.add_argument("--memory-model")
    parser.add_argument("--resolution-model")
    parser.add_argument("--clarification-model")
    parser.add_argument("--capabilities-config", type=Path)
    parser.add_argument(
        "--workflow-spec",
        type=Path,
        help=(
            "trusted operator-owned YAML workflow specification; never loaded "
            "from a target repository"
        ),
    )
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--sandbox-provider")
    parser.add_argument("--unsafe-local-shell", action="store_true")
    parser.add_argument(
        "--api-url",
        default=os.getenv("SWEFORGE_GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument(
        "--api-version",
        default=os.getenv("SWEFORGE_GITHUB_API_VERSION", DEFAULT_API_VERSION),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--max-ticks", type=int, default=20)
    parser.add_argument("--initial-lookback-minutes", type=int, default=10)
    parser.add_argument(
        "--debug-agent",
        action="store_true",
        help="show bounded, redacted live agent/workflow events on stderr",
    )
    parser.add_argument(
        "--debug-agent-tools",
        action="store_true",
        help="also show bounded, redacted tool arguments and results",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll once, drain discovered work, then exit",
    )
    return parser


def _mappings(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in result:
            raise ValueError("--repo-path must be unique OWNER/REPO=PATH mappings")
        result[name] = Path(path).expanduser().resolve()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mappings = _mappings(args.repo_path)
        if not all(
            (
                args.planning_model or args.model,
                args.execution_model or args.model,
                args.review_model or args.model,
            )
        ):
            raise ValueError("planning, execution, and review models are required")
        if args.initial_lookback_minutes < 0:
            raise ValueError("lookback must be non-negative")
        config = ServerConfig(
            repositories=tuple(sorted(mappings)),
            repo_paths=mappings,
            db=args.db,
            checkpoints=args.checkpoints,
            memory_db=args.memory_db,
            workspace_root=args.workspace_root,
            lock_root=args.lock_root,
            model=args.model or "",
            planning_model=args.planning_model,
            execution_model=args.execution_model,
            review_model=args.review_model,
            memory_model=args.memory_model,
            resolution_model=args.resolution_model,
            clarification_model=args.clarification_model,
            capabilities_config=args.capabilities_config,
            workflow_spec=args.workflow_spec,
            sandbox_provider=args.sandbox_provider,
            ready_file=args.ready_file,
            unsafe_local_shell=args.unsafe_local_shell,
            api_url=args.api_url,
            api_version=args.api_version,
            workers=args.workers,
            poll_interval=args.poll_interval,
            max_ticks=args.max_ticks,
            initial_lookback_minutes=args.initial_lookback_minutes,
            once=args.once,
            debug_agent=args.debug_agent or args.debug_agent_tools,
            debug_agent_tools=args.debug_agent_tools,
        )
        server = SWEForgeServer(config)
        signal.signal(signal.SIGTERM, lambda *_: server.request_stop())
        signal.signal(signal.SIGINT, lambda *_: server.request_stop())
        server.run()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge-serve: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
