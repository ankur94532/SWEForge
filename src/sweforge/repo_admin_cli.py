"""Operator CLI for focused repository configuration mutations.

Every command materializes the complete current generation from immutable
installed content, applies exactly one staged change, validates the whole
resulting bundle, and installs a new generation atomically. Existing
IssueThreads stay bound to their generation; only new work uses the new one.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .github_store import SQLiteGitHubStore
from .repo_config import (
    MAX_BUNDLE_FILE_BYTES,
    RepoConfigGeneration,
    RepoConfigRegistry,
    load_bundle_yaml,
)
from .skills import declared_skill_name, parse_skill_metadata

NOT_CONFIGURED = (
    "repository has no installed configuration; run "
    "`sweforge repo configure OWNER/REPO ./bundle` or `sweforge repo init "
    "OWNER/REPO` first"
)


GROUPS = ("workflow", "skill", "tool", "mcp")


def build_parser(group: str) -> argparse.ArgumentParser:
    """Build the parser for one focused mutation group."""
    if group not in GROUPS:
        raise ValueError(f"unknown configuration group: {group}")
    parser = argparse.ArgumentParser(
        prog=f"sweforge {group}",
        description="Apply one focused change to a trusted repository configuration",
    )
    parser.add_argument(
        "--state-db", type=Path, default=Path("~/.sweforge/state.db").expanduser()
    )
    commands = parser.add_subparsers(dest="command", required=True)
    if group in ("workflow", "mcp"):
        replace = commands.add_parser("set")
        replace.add_argument("repo", metavar="OWNER/REPO")
        replace.add_argument("path", type=Path)
        return parser
    add = commands.add_parser("add")
    add.add_argument("repo", metavar="OWNER/REPO")
    add.add_argument("path", type=Path)
    add.add_argument(
        "--replace",
        action="store_true",
        help="replace an already installed entry instead of failing",
    )
    remove = commands.add_parser("remove")
    remove.add_argument("repo", metavar="OWNER/REPO")
    remove.add_argument("name")
    return parser


def _read_source_file(path: Path, label: str) -> str:
    source = path.expanduser()
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"{label} is not a readable file: {path}")
    data = source.read_bytes()
    if not data or len(data) > MAX_BUNDLE_FILE_BYTES:
        raise ValueError(f"{label} is empty or too large: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8: {path}") from exc


def _copy_source_directory(source: Path, destination: Path, label: str) -> None:
    root = source.expanduser()
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{label} is not a directory: {source}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"{label} symlinks are forbidden: {path.name}")
        if not path.is_file():
            continue
        _read_source_file(path, label)
    shutil.copytree(root, destination)


def _workflow_skill_references(spec) -> dict[str, str]:
    references: dict[str, str] = {}
    for task in spec.tasks:
        for phase in (task.planning, task.execution, task.validation):
            for name in phase.skills:
                references.setdefault(name, task.id)
    return references


def _workflow_tool_references(spec) -> dict[str, str]:
    references: dict[str, str] = {}
    for task in spec.tasks:
        for phase in (task.planning, task.execution, task.validation):
            for name in phase.tools:
                references.setdefault(name, task.id)
    return references


def _installed_script_directories(staging: Path) -> dict[str, str]:
    """Map registered tool names to their installed directory names."""
    result: dict[str, str] = {}
    scripts = staging / "tools" / "scripts"
    if not scripts.is_dir():
        return result
    for directory in sorted(path for path in scripts.iterdir() if path.is_dir()):
        metadata = directory / "tool.yaml"
        if not metadata.is_file():
            continue
        document = load_bundle_yaml(
            metadata.read_text(encoding="utf-8"), f"script tool {directory.name}"
        )
        name = document.get("name")
        if isinstance(name, str):
            result[name] = directory.name
    return result


def _set_workflow(path: Path) -> Callable[[Path], str]:
    content = _read_source_file(path, "workflow")

    def apply(staging: Path) -> str:
        (staging / "workflow.yaml").write_text(content, encoding="utf-8")
        return "Replaced workflow: workflow.yaml"

    return apply


def _add_skill(path: Path, replace: bool) -> Callable[[Path], str]:
    source = path.expanduser()
    document = _read_source_file(source / "SKILL.md", "skill SKILL.md")
    name = declared_skill_name(document, str(source)) or source.name
    parse_skill_metadata(name, document)

    def apply(staging: Path) -> str:
        target = staging / "skills" / name
        if target.exists():
            if not replace:
                raise ValueError(f'skill "{name}" is already installed; use --replace')
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_source_directory(source, target, "skill")
        verb = "Replaced" if replace else "Added"
        return f"{verb} skill: {name}"

    return apply


def _remove_skill(name: str) -> Callable[[Path], str]:
    def apply(staging: Path) -> str:
        target = staging / "skills" / name
        if not target.is_dir():
            raise ValueError(f'skill "{name}" is not installed')
        shutil.rmtree(target)
        return f"Removed skill: {name}"

    return apply


def _add_tool(path: Path, replace: bool) -> Callable[[Path], str]:
    source = path.expanduser()
    document = load_bundle_yaml(
        _read_source_file(source / "tool.yaml", "script tool tool.yaml"),
        "script tool",
    )
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("script tool tool.yaml must declare a name")

    def apply(staging: Path) -> str:
        installed = _installed_script_directories(staging)
        target = staging / "tools" / "scripts" / source.name
        existing = installed.get(name)
        if (existing is not None or target.exists()) and not replace:
            raise ValueError(f'tool "{name}" is already installed; use --replace')
        if existing is not None:
            shutil.rmtree(staging / "tools" / "scripts" / existing)
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_source_directory(source, target, "script tool")
        verb = "Replaced" if replace else "Added"
        return f"{verb} tool: {name}"

    return apply


def _remove_tool(name: str) -> Callable[[Path], str]:
    def apply(staging: Path) -> str:
        directory = _installed_script_directories(staging).get(name)
        if directory is None:
            raise ValueError(f'tool "{name}" is not installed')
        shutil.rmtree(staging / "tools" / "scripts" / directory)
        return f"Removed tool: {name}"

    return apply


def _set_mcp(path: Path) -> Callable[[Path], str]:
    content = _read_source_file(path, "MCP configuration")

    def apply(staging: Path) -> str:
        target = staging / "tools" / "mcp"
        target.mkdir(parents=True, exist_ok=True)
        (target / "servers.yaml").write_text(content, encoding="utf-8")
        return "Replaced MCP configuration: tools/mcp/servers.yaml"

    return apply


def _guard_removals(
    registry: RepoConfigRegistry,
    generation: RepoConfigGeneration,
    repo_id: int,
    *,
    skill: str | None = None,
    tool: str | None = None,
) -> None:
    spec = registry.load_workflow(repo_id, generation.generation_id)
    if skill is not None:
        task = _workflow_skill_references(spec).get(skill)
        if task is not None:
            raise ValueError(
                f'cannot remove skill "{skill}": '
                f'workflow task "{task}" still references it'
            )
    if tool is not None:
        task = _workflow_tool_references(spec).get(tool)
        if task is not None:
            raise ValueError(
                f'cannot remove tool "{tool}": '
                f'workflow task "{task}" still references it'
            )


def _apply(
    state: SQLiteGitHubStore, repo: str, mutate: Callable[[Path], str], **guards
) -> str:
    repo_id = state.repository_id_for_full_name(repo)
    if repo_id is None:
        raise ValueError(f"repository has not been observed: {repo}")
    registry = RepoConfigRegistry(state)
    current = registry.current_generation(repo_id)
    if current is None:
        raise ValueError(NOT_CONFIGURED)
    if guards:
        _guard_removals(registry, current, repo_id, **guards)
    with tempfile.TemporaryDirectory(prefix="sweforge-config-") as workspace:
        staging = Path(workspace) / "bundle"
        registry.materialize(repo_id, current.generation_id, staging)
        action = mutate(staging)
        generation = registry.install(repo_id, staging)
    return "\n".join(
        [
            f"Repository: {repo}",
            f"Generation: {generation.generation}",
            f"Digest: {generation.digest}",
            action,
        ]
    )


def main(argv: list[str] | None = None) -> int:
    selected = list(argv) if argv is not None else sys.argv[1:]
    group = selected[0] if selected and selected[0] in GROUPS else None
    if group is None:
        print(f"sweforge: expected one of {', '.join(GROUPS)}", file=sys.stderr)
        return 2
    args = build_parser(group).parse_args(selected[1:])
    state = SQLiteGitHubStore(args.state_db.expanduser())
    try:
        guards: dict[str, str] = {}
        if group == "workflow":
            mutate = _set_workflow(args.path)
        elif group == "mcp":
            mutate = _set_mcp(args.path)
        elif group == "skill" and args.command == "add":
            mutate = _add_skill(args.path, args.replace)
        elif group == "skill":
            mutate = _remove_skill(args.name)
            guards = {"skill": args.name}
        elif args.command == "add":
            mutate = _add_tool(args.path, args.replace)
        else:
            mutate = _remove_tool(args.name)
            guards = {"tool": args.name}
        print(_apply(state, args.repo, mutate, **guards))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"sweforge {group}: {exc}", file=sys.stderr)
        return 2
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
