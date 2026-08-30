"""Durable repository-scoped memory backed by LangGraph's SQLite store."""

import sqlite3
from pathlib import Path

from langgraph.store.base import BaseStore
from langgraph.store.sqlite import SqliteStore

MEMORY_VIRTUAL_PATH = "/memories/AGENTS.md"
MEMORY_STORE_KEY = "/AGENTS.md"
LEGACY_MEMORY_STORE_KEY = "/memories/AGENTS.md"
DEFAULT_MEMORY_PATH = Path("~/.sweforge/memory.sqlite")
DEFAULT_MEMORY_CONTENT = "# SWEForge Repository Memory\n"


def repo_memory_namespace(repo_id: int) -> tuple[str, ...]:
    """Return the stable store namespace for one GitHub repository."""
    return ("sweforge", "repo", str(repo_id), "memory")


def legacy_repo_memory_namespace(repo_id: int) -> tuple[str, ...]:
    """Namespace used before memory and skills received separate scopes."""
    return ("sweforge", "repo", str(repo_id))


def repo_skills_namespace(
    repo_id: int, config_generation_id: str | None = None
) -> tuple[str, ...]:
    """Return the repo and optional immutable-generation skill namespace."""
    base = ("sweforge", "repo", str(repo_id), "skills")
    return (*base, config_generation_id) if config_generation_id else base


class SQLiteMemoryStore:
    """Own a native LangGraph SQLite store and its connection lifecycle."""

    def __init__(self, path: str | Path = DEFAULT_MEMORY_PATH) -> None:
        self.path = Path(path).expanduser()
        if str(path) != ":memory:":
            self.path = self.path.resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            ":memory:" if str(path) == ":memory:" else self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self.store = SqliteStore(self.connection)
        try:
            self.store.setup()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> BaseStore:
        return self.store

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def ensure_repo_memory(store: BaseStore, namespace: tuple[str, ...]) -> None:
    """Create the canonical memory file with a safe header when absent."""
    if store.get(namespace, MEMORY_STORE_KEY) is not None:
        return
    legacy = store.get(namespace, LEGACY_MEMORY_STORE_KEY)
    if legacy is None and len(namespace) == 4 and namespace[-1] == "memory":
        legacy_namespace = (*namespace[:-1],)
        legacy = store.get(legacy_namespace, MEMORY_STORE_KEY)
        if legacy is None:
            legacy = store.get(legacy_namespace, LEGACY_MEMORY_STORE_KEY)
    if legacy is not None:
        store.put(namespace, MEMORY_STORE_KEY, dict(legacy.value))
        return
    store.put(
        namespace,
        MEMORY_STORE_KEY,
        {"content": DEFAULT_MEMORY_CONTENT, "encoding": "utf-8"},
    )


def read_repo_memory(store: BaseStore, namespace: tuple[str, ...]) -> str | None:
    """Read the canonical repository memory file, if it exists."""
    item = store.get(namespace, MEMORY_STORE_KEY)
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


def write_repo_memory(
    store: BaseStore, namespace: tuple[str, ...], content: str
) -> None:
    """Replace the canonical repository memory file."""
    store.put(namespace, MEMORY_STORE_KEY, {"content": content, "encoding": "utf-8"})


def append_repo_memory(store: BaseStore, namespace: tuple[str, ...], text: str) -> None:
    """Append trusted operator text to the canonical repository memory file."""
    ensure_repo_memory(store, namespace)
    current = read_repo_memory(store, namespace)
    if current is None:
        current = DEFAULT_MEMORY_CONTENT
    separator = "" if not current or current.endswith("\n") else "\n"
    suffix = "" if text.endswith("\n") else "\n"
    write_repo_memory(store, namespace, f"{current}{separator}{text}{suffix}")
