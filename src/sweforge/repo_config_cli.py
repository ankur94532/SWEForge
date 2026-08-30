"""Operator CLI for trusted repository configuration bundles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .github_store import SQLiteGitHubStore
from .repo_config import RepoConfigRegistry, validate_repo_bundle

STARTER_WORKFLOW = """version: 1
workflow_id: starter
tasks:
  - id: implementation
    depends_on: []
    planning:
      skill: implementation
      tools: [ls, read_file, glob, grep]
    execution:
      skill: implementation
      tools: [ls, read_file, glob, grep, edit_file, execute]
    validation:
      skill: implementation
      tools: [read_file, glob, grep, run_validation]
"""

STARTER_SKILL = """---
name: implementation
description: Plan, implement, and validate the repository's approved changes.
---

# Implementation

Inspect the repository, follow the approved scope, preserve unrelated work, and
provide concrete validation evidence.
"""

MCP_README = """# Repository MCP tools

Add `servers.yaml` only when this repository uses trusted MCP capabilities.
See `examples/repo-config/tools/mcp/servers.example.yaml` for the schema.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage trusted SWEForge repository configuration"
    )
    parser.add_argument(
        "--state-db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("repo", metavar="OWNER/REPO")
    init.add_argument("--output", type=Path)
    configure = commands.add_parser("configure")
    configure.add_argument("repo", metavar="OWNER/REPO")
    configure.add_argument("bundle", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("repo", metavar="OWNER/REPO")
    validate.add_argument("bundle", type=Path, nargs="?")
    show = commands.add_parser("show")
    show.add_argument("repo", metavar="OWNER/REPO")
    return parser


def _init(repo: str, output: Path | None) -> Path:
    if "/" not in repo:
        raise ValueError("repository must be OWNER/REPO")
    target = (output or Path(repo.replace("/", "-") + "-sweforge")).expanduser()
    if target.exists():
        raise ValueError(f"template output already exists: {target}")
    (target / "skills" / "implementation").mkdir(parents=True)
    (target / "tools" / "scripts").mkdir(parents=True)
    (target / "tools" / "mcp").mkdir(parents=True)
    (target / "workflow.yaml").write_text(STARTER_WORKFLOW, encoding="utf-8")
    (target / "skills" / "implementation" / "SKILL.md").write_text(
        STARTER_SKILL, encoding="utf-8"
    )
    (target / "tools" / "mcp" / "README.md").write_text(MCP_README, encoding="utf-8")
    return target.resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        try:
            target = _init(args.repo, args.output)
            print(target)
            return 0
        except (OSError, ValueError) as exc:
            print(f"sweforge repo: {exc}", file=sys.stderr)
            return 2
    state = SQLiteGitHubStore(args.state_db.expanduser())
    try:
        repo_id = state.repository_id_for_full_name(args.repo)
        if repo_id is None:
            raise ValueError(f"repository has not been observed: {args.repo}")
        registry = RepoConfigRegistry(state)
        if args.command == "configure":
            generation = registry.install(repo_id, args.bundle)
            print(
                json.dumps(
                    {
                        "repo": args.repo,
                        "generation": generation.generation,
                        "digest": generation.digest,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "validate":
            if args.bundle is not None:
                bundle = validate_repo_bundle(args.bundle)
                result = {
                    "repo": args.repo,
                    "valid": True,
                    "digest": bundle.digest,
                    "workflow": bundle.workflow.workflow_id,
                }
            else:
                generation = registry.current_generation(repo_id)
                if generation is None:
                    raise ValueError("repository has no installed configuration")
                registry.load_workflow(repo_id, generation.generation_id)
                result = {
                    "repo": args.repo,
                    "valid": True,
                    "generation": generation.generation,
                    "digest": generation.digest,
                }
            print(json.dumps(result, sort_keys=True))
        elif args.command == "show":
            print(
                json.dumps(
                    {"repo": args.repo, **registry.safe_summary(repo_id)},
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge repo: {exc}", file=sys.stderr)
        return 2
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
