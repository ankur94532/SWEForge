"""Trusted repository-scoped Deep Agents skill management."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from langgraph.store.base import BaseStore

from .repo_memory import repo_skills_namespace

SKILLS_VIRTUAL_PATH = "/skills/"
MAX_SKILL_FILE_BYTES = 200_000
MAX_SKILL_DESCRIPTION_CHARS = 300
SKILL_LIST_PAGE_SIZE = 100
_SKILL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

DEFAULT_WORKFLOW_SKILLS = {
    "implementation-planning": """---
name: implementation-planning
description: >-
  Plan repository changes within the approved scope using concrete codebase
  evidence.
---

# Implementation planning

Inspect the repository carefully, keep the plan within the approved scope, and
identify concrete validation for the intended changes. Use only the tools
authorized for the current planning phase.
""",
    "implementation-execution": """---
name: implementation-execution
description: >-
  Implement the approved repository change and record concrete execution
  evidence.
---

# Implementation execution

Follow the approved plan, preserve unrelated work, and implement the scoped
repository changes. Use only the tools authorized for the current execution
phase and record concrete execution evidence.
""",
    "implementation-validation": """---
name: implementation-validation
description: >-
  Validate the implementation against the approved plan and report concrete
  evidence.
---

# Implementation validation

Inspect the resulting changes, validate them against the approved plan, and
report concrete evidence. Use only the tools authorized for the current
validation phase.
""",
}


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """Bounded operator-owned metadata for one authorized workflow skill."""

    name: str
    description: str
    path: str


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate frontmatter keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing skill metadata",
                node.start_mark,
                "metadata keys must be scalar values",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing skill metadata",
                node.start_mark,
                f"duplicate metadata key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def canonical_skill_path(name: str) -> str:
    """Return the only advertised read path for a trusted skill identity."""
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        raise ValueError("skill name is malformed")
    return f"/skills/{name}/SKILL.md"


def _skill_frontmatter(content: str, label: str) -> dict | None:
    """Return the authoritative frontmatter mapping, or None when absent."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"required workflow skill is missing: {label}")
    if not content.startswith("---"):
        return None
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"skill metadata is malformed: {label}")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration as exc:
        raise ValueError(f"skill metadata is malformed: {label}") from exc
    try:
        metadata = yaml.load("\n".join(lines[1:end]), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"skill metadata is malformed: {label}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"skill metadata must be a mapping: {label}")
    return metadata


def declared_skill_name(content: str, label: str) -> str | None:
    """Return the trusted name a skill file declares for itself, if any."""
    metadata = _skill_frontmatter(content, label)
    if metadata is None:
        return None
    name = metadata.get("name")
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        raise ValueError(f"skill metadata name is malformed: {label}")
    return name


def parse_skill_metadata(name: str, content: str) -> SkillMetadata:
    """Parse bounded Agent Skills-compatible frontmatter, or a safe fallback.

    Existing operator skills without frontmatter remain usable through a
    deterministic description. Once a file starts a frontmatter block, that
    metadata is authoritative and any malformed value fails closed.
    """
    path = canonical_skill_path(name)
    metadata = _skill_frontmatter(content, name)
    if metadata is None:
        return SkillMetadata(
            name=name,
            description=f"Trusted skill {name}; load for full instructions.",
            path=path,
        )
    metadata_name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(metadata_name, str) or metadata_name != name:
        raise ValueError(f"skill metadata name does not match directory: {name}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill metadata description is required: {name}")
    normalized_description = " ".join(description.split())
    if len(normalized_description) > MAX_SKILL_DESCRIPTION_CHARS:
        raise ValueError(f"skill metadata description is too long: {name}")
    return SkillMetadata(name=name, description=normalized_description, path=path)


def _skill_key(path: str) -> str:
    normalized = PurePosixPath("/" + path.lstrip("/"))
    if normalized.is_absolute() and ".." in normalized.parts:
        raise ValueError("skill path traversal is not allowed")
    key = "/" + "/".join(part for part in normalized.parts if part != "/")
    if key == "/" or "*" in key or "?" in key:
        raise ValueError("invalid skill path")
    return key


def put_repo_skill(
    store: BaseStore,
    repo_id: int,
    relative_path: str,
    content: str,
    *,
    config_generation_id: str | None = None,
) -> None:
    """Trusted operator write for one repo skill file."""
    if not content or len(content.encode()) > MAX_SKILL_FILE_BYTES:
        raise ValueError("skill content is empty or too large")
    key = _skill_key(relative_path)
    if key.endswith("/SKILL.md") is False and key != "/SKILL.md":
        # Supporting resources are allowed, but every skill directory must be
        # anchored by a SKILL.md. Validation of the bundle occurs at listing.
        if key.count("/") < 2:
            raise ValueError("skill files must be inside a skill directory")
    store.put(
        repo_skills_namespace(repo_id, config_generation_id),
        key,
        {"content": content, "encoding": "utf-8"},
    )


def remove_repo_skill(store: BaseStore, repo_id: int, relative_path: str) -> None:
    store.delete(repo_skills_namespace(repo_id), _skill_key(relative_path))


def list_repo_skills(store: BaseStore, repo_id: int) -> list[str]:
    namespace = repo_skills_namespace(repo_id)
    keys: list[str] = []
    offset = 0
    while True:
        page = store.search(
            namespace,
            limit=SKILL_LIST_PAGE_SIZE,
            offset=offset,
        )
        keys.extend(str(item.key) for item in page)
        if len(page) < SKILL_LIST_PAGE_SIZE:
            break
        offset += len(page)
    return sorted(keys)


def show_repo_skill(
    store: BaseStore,
    repo_id: int,
    relative_path: str,
    *,
    config_generation_id: str | None = None,
) -> str | None:
    item = store.get(
        repo_skills_namespace(repo_id, config_generation_id),
        _skill_key(relative_path),
    )
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


def ensure_default_repo_skills(
    store: BaseStore, repo_id: int, names: Iterable[str]
) -> int:
    """Seed missing application-owned default skills without replacing content."""
    count = 0
    for name in dict.fromkeys(names):
        content = DEFAULT_WORKFLOW_SKILLS.get(name)
        if content is None:
            continue
        relative_path = f"{name}/SKILL.md"
        key = _skill_key(relative_path)
        if store.get(repo_skills_namespace(repo_id), key) is not None:
            continue
        put_repo_skill(store, repo_id, relative_path, content)
        count += 1
    return count


def seed_repo_skills(store: BaseStore, repo_id: int, root: str | Path) -> int:
    """Trusted operator import; repository files cannot call this API."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise ValueError("skill root is not a directory")
    count = 0
    for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
        path.resolve().relative_to(root_path)
        put_repo_skill(
            store, repo_id, path.relative_to(root_path).as_posix(), path.read_text()
        )
        count += 1
    return count


def validate_skill_tree(store: BaseStore, repo_id: int) -> None:
    keys = list_repo_skills(store, repo_id)
    skill_dirs = {
        str(PurePosixPath(key).parent) for key in keys if key.endswith("/SKILL.md")
    }
    if not skill_dirs:
        raise ValueError("repository has no SKILL.md files")
    for key in keys:
        if ".." in PurePosixPath(key).parts or not key.startswith("/"):
            raise ValueError(f"invalid stored skill key: {key}")
