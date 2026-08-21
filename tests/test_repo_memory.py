from pathlib import Path

from deepagents.backends import LocalShellBackend, StateBackend, StoreBackend
from deepagents.middleware.filesystem import _check_fs_permission
from langchain_core.messages import AIMessage

from sweforge.agent import _build_backend, run_task
from sweforge.github_store import SQLiteGitHubStore
from sweforge.repo_memory import (
    DEFAULT_MEMORY_CONTENT,
    LEGACY_MEMORY_STORE_KEY,
    MEMORY_STORE_KEY,
    MEMORY_VIRTUAL_PATH,
    SQLiteMemoryStore,
    ensure_repo_memory,
    read_repo_memory,
    repo_memory_namespace,
    write_repo_memory,
)
from sweforge.repo_memory_cli import main as memory_main


def test_memory_persists_after_close_and_reopen(tmp_path: Path):
    path = tmp_path / "memory.sqlite"
    namespace = repo_memory_namespace(101)
    first = SQLiteMemoryStore(path)
    write_repo_memory(first.store, namespace, "# Repo A\nRun pytest.\n")
    first.close()

    second = SQLiteMemoryStore(path)
    assert read_repo_memory(second.store, namespace) == "# Repo A\nRun pytest.\n"
    second.close()


def test_memory_is_shared_by_threads_in_one_repo_and_isolated_by_repo():
    memory = SQLiteMemoryStore(":memory:")
    repo_a = repo_memory_namespace(101)
    repo_b = repo_memory_namespace(202)
    write_repo_memory(memory.store, repo_a, "repo A only")

    assert read_repo_memory(memory.store, repo_a) == "repo A only"
    assert read_repo_memory(memory.store, repo_b) is None
    assert repo_memory_namespace(101) == repo_memory_namespace(101)
    memory.close()


def test_same_issue_number_does_not_collide_between_repositories():
    assert repo_memory_namespace(101) != repo_memory_namespace(202)


def test_missing_memory_is_initialized_without_overwriting_existing_content():
    memory = SQLiteMemoryStore(":memory:")
    namespace = repo_memory_namespace(101)
    ensure_repo_memory(memory.store, namespace)
    assert read_repo_memory(memory.store, namespace) == DEFAULT_MEMORY_CONTENT
    write_repo_memory(memory.store, namespace, "existing")
    ensure_repo_memory(memory.store, namespace)
    assert read_repo_memory(memory.store, namespace) == "existing"
    memory.close()


def test_store_backend_routes_memory_and_preserves_workspace_routes(tmp_path: Path):
    memory = SQLiteMemoryStore(":memory:")
    backend = _build_backend(
        str(tmp_path),
        memory_store=memory.store,
        memory_namespace=repo_memory_namespace(101),
    )

    assert isinstance(backend.default, LocalShellBackend)
    assert isinstance(backend.routes["/sweforge_internal/"], StateBackend)
    assert isinstance(backend.routes["/memories/"], StoreBackend)
    write_repo_memory(memory.store, repo_memory_namespace(101), "memory")
    assert (
        memory.store.get(repo_memory_namespace(101), MEMORY_STORE_KEY).value["content"]
        == "memory"
    )
    assert backend.read(MEMORY_VIRTUAL_PATH).file_data["content"] == "memory"
    downloaded = backend.download_files([MEMORY_VIRTUAL_PATH])[0]
    assert downloaded.content == b"memory"
    assert downloaded.error is None
    backend.write("/memories/test.md", "test")
    assert memory.store.get(repo_memory_namespace(101), "/test.md") is not None
    assert backend.default.execute("test -d .").exit_code == 0
    memory.close()


def test_store_search_is_scoped_to_namespace_segments():
    memory = SQLiteMemoryStore(":memory:")
    repo_one = repo_memory_namespace(1)
    repo_twelve = repo_memory_namespace(12)
    write_repo_memory(memory.store, repo_one, "repo one")
    write_repo_memory(memory.store, repo_twelve, "repo twelve")

    results = memory.store.search(repo_one)
    assert [item.value["content"] for item in results] == ["repo one"]
    memory.close()


def test_legacy_memory_key_is_migrated_and_correct_key_wins():
    memory = SQLiteMemoryStore(":memory:")
    namespace = repo_memory_namespace(101)
    legacy_content = "legacy operator memory\n"
    memory.store.put(
        namespace,
        LEGACY_MEMORY_STORE_KEY,
        {"content": legacy_content, "encoding": "utf-8"},
    )
    ensure_repo_memory(memory.store, namespace)
    assert (
        memory.store.get(namespace, MEMORY_STORE_KEY).value["content"] == legacy_content
    )
    backend = _build_backend(
        "/tmp", memory_store=memory.store, memory_namespace=namespace
    )
    assert backend.read(MEMORY_VIRTUAL_PATH).file_data["content"] == legacy_content

    correct_content = "correct operator memory\n"
    write_repo_memory(memory.store, namespace, correct_content)
    memory.store.put(
        namespace,
        LEGACY_MEMORY_STORE_KEY,
        {"content": "stale legacy memory\n", "encoding": "utf-8"},
    )
    ensure_repo_memory(memory.store, namespace)
    assert read_repo_memory(memory.store, namespace) == correct_content
    memory.close()


def test_run_task_wires_native_memory_and_denies_memory_writes(monkeypatch, tmp_path):
    calls = {}

    class FakeAgent:
        def invoke(self, state, config=None):
            return {"messages": [AIMessage(content="done")]}

    def fake_create(**kwargs):
        calls.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr("sweforge.agent.create_deep_agent", fake_create)
    memory = SQLiteMemoryStore(":memory:")
    namespace = repo_memory_namespace(101)
    assert (
        run_task(
            model="provider:model",
            worktree=str(tmp_path),
            task="inspect",
            memory_store=memory.store,
            memory_namespace=namespace,
        )
        == "done"
    )

    assert calls["memory"] == [MEMORY_VIRTUAL_PATH]
    assert calls["store"] is memory.store
    permission = calls["permissions"][0]
    assert permission.operations == ["write"]
    assert permission.paths == ["/memories/**"]
    assert permission.mode == "deny"
    assert str(memory.path) not in calls["backend"].default._env.values()
    assert (
        _check_fs_permission(calls["permissions"], "write", MEMORY_VIRTUAL_PATH)
        == "deny"
    )
    assert (
        _check_fs_permission(calls["permissions"], "read", MEMORY_VIRTUAL_PATH)
        == "allow"
    )
    assert (
        _check_fs_permission(calls["permissions"], "write", "/calculator.py") == "allow"
    )
    memory.close()


def _observed_state(tmp_path: Path) -> Path:
    state_path = tmp_path / "state.db"
    state = SQLiteGitHubStore(state_path)
    state.upsert_repository(101, "owner/repo", "now")
    state.close()
    return state_path


def test_memory_cli_resolves_repo_id_and_supports_show_replace_append(
    tmp_path: Path, capsys
):
    state_path = _observed_state(tmp_path)
    memory_path = tmp_path / "memory.sqlite"
    assert (
        memory_main(
            [
                "--state-db",
                str(state_path),
                "--memory-db",
                str(memory_path),
                "--repo",
                "owner/repo",
                "show",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == DEFAULT_MEMORY_CONTENT

    replacement = tmp_path / "memory.md"
    replacement.write_text("# Trusted\nRun pytest.\n")
    assert (
        memory_main(
            [
                "--state-db",
                str(state_path),
                "--memory-db",
                str(memory_path),
                "--repo",
                "owner/repo",
                "replace",
                "--file",
                str(replacement),
            ]
        )
        == 0
    )
    assert (
        memory_main(
            [
                "--state-db",
                str(state_path),
                "--memory-db",
                str(memory_path),
                "--repo",
                "owner/repo",
                "append",
                "--text",
                "Use uv run pytest.",
            ]
        )
        == 0
    )
    assert (
        memory_main(
            [
                "--state-db",
                str(state_path),
                "--memory-db",
                str(memory_path),
                "--repo",
                "owner/repo",
                "show",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "# Trusted\nRun pytest.\nUse uv run pytest.\n"


def test_memory_cli_rejects_unknown_repository(tmp_path: Path, capsys):
    state_path = _observed_state(tmp_path)
    result = memory_main(
        [
            "--state-db",
            str(state_path),
            "--memory-db",
            str(tmp_path / "memory.sqlite"),
            "--repo",
            "owner/unknown",
            "show",
        ]
    )
    assert result == 2
    assert "has not been observed" in capsys.readouterr().err
