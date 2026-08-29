"""Trusted repository-scoped Deep Agents skill management."""

from pathlib import Path, PurePosixPath

from langgraph.store.base import BaseStore

from .repo_memory import repo_skills_namespace

SKILLS_VIRTUAL_PATH = "/skills/"
MAX_SKILL_FILE_BYTES = 200_000
SKILL_LIST_PAGE_SIZE = 100


def _skill_key(path: str) -> str:
    normalized = PurePosixPath("/" + path.lstrip("/"))
    if normalized.is_absolute() and ".." in normalized.parts:
        raise ValueError("skill path traversal is not allowed")
    key = "/" + "/".join(part for part in normalized.parts if part != "/")
    if key == "/" or "*" in key or "?" in key:
        raise ValueError("invalid skill path")
    return key


def put_repo_skill(
    store: BaseStore, repo_id: int, relative_path: str, content: str
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
        repo_skills_namespace(repo_id),
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


def show_repo_skill(store: BaseStore, repo_id: int, relative_path: str) -> str | None:
    item = store.get(repo_skills_namespace(repo_id), _skill_key(relative_path))
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


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
