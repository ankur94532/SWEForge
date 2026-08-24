import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import TypedDict

import pytest
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph
from pydantic import PrivateAttr

from sweforge.agent import run_task
from sweforge.context import RepoAgentContext
from sweforge.execution import (
    SQLiteCheckpointer,
    ThreadLockUnavailable,
    event_message_id,
    execute_one,
    normalize_task,
    recover_stale,
    thread_lock,
)
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    ExecutionStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(tmp_path: Path, name: str = "repo") -> Path:
    path = tmp_path / name
    path.mkdir()
    git("init", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("initial\n")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "initial", cwd=path)
    return path


def source_event(
    repo: RepositoryRef,
    *,
    source_id: str,
    updated: str,
    body: str,
    subject_kind: SubjectKind = SubjectKind.ISSUE,
    number: int = 7,
) -> SourceEvent:
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE,
        source_id=source_id,
        source_updated_at=updated,
        subject_kind=subject_kind,
        subject_number=number,
        author_login="octocat",
        body=body,
        html_url=None,
    )


def persist(store, event: SourceEvent, stream: str = "issues") -> None:
    store.upsert_repository(event.repo_id, event.repo_full_name, "now")
    store.record_batch(
        event.repo_id,
        stream,
        [event],
        since="now",
        etag=None,
        polled_at=event.source_updated_at,
    )


def _tool_name(item) -> str:
    if isinstance(item, dict):
        function = item.get("function", {})
        return item.get("name") or function.get("name") or ""
    return item.name


class RecordingToolModel(BaseChatModel):
    """Deterministic chat model that records the real bound tool schemas."""

    _responses: list[AIMessage] = PrivateAttr()
    _surfaces: list[tuple[str, ...]] = PrivateAttr(default_factory=list)
    _messages: list[list] = PrivateAttr(default_factory=list)
    _barrier: Barrier | None = PrivateAttr(default=None)

    def __init__(self, responses: list[AIMessage], *, barrier: Barrier | None = None):
        super().__init__()
        self._responses = list(responses)
        self._barrier = barrier

    @property
    def _llm_type(self) -> str:
        return "sweforge-recording-tools"

    @property
    def surfaces(self) -> list[tuple[str, ...]]:
        return self._surfaces

    @property
    def messages(self) -> list[list]:
        return self._messages

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self._surfaces.append(tuple(sorted(_tool_name(item) for item in tools)))
        if self._barrier is not None:
            self._barrier.wait(timeout=5)
            self._barrier = None
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._messages.append(list(messages))
        message = self._responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_run_task_uses_native_checkpointer_and_thread_config(monkeypatch):
    calls = {}

    class FakeAgent:
        def invoke(self, state, config=None):
            calls["invoke"] = (state, config)
            return {"messages": [type("Message", (), {"content": "done"})()]}

    def fake_create(**kwargs):
        calls["create"] = kwargs
        return FakeAgent()

    monkeypatch.setattr("sweforge.agent.create_deep_agent", fake_create)
    checkpointer = object()
    assert (
        run_task(
            model="provider:model",
            worktree="/tmp/worktree",
            task="fix it",
            thread_id="github:1:issue:7",
            checkpointer=checkpointer,
        )
        == "done"
    )
    assert calls["create"]["checkpointer"] is checkpointer
    assert calls["invoke"][0] == {"messages": [{"role": "user", "content": "fix it"}]}
    assert calls["invoke"][1] == {"configurable": {"thread_id": "github:1:issue:7"}}


def test_initial_and_repair_actual_tool_surfaces(tmp_path):
    initial_model = RecordingToolModel([AIMessage(content="initial done")])
    repair_model = RecordingToolModel([AIMessage(content="repair done")])

    run_task(
        model=initial_model,
        worktree=str(tmp_path),
        task="inspect",
    )
    run_task(
        model=repair_model,
        worktree=str(tmp_path),
        task="repair",
        repair_mode=True,
    )

    initial_tools = set(initial_model.surfaces[-1])
    repair_tools = set(repair_model.surfaces[-1])
    expected_filesystem = {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
    }
    assert "task" in initial_tools
    assert "task" not in repair_tools
    assert expected_filesystem <= initial_tools
    assert expected_filesystem <= repair_tools


def test_repair_rejects_stale_task_call_without_dispatch(tmp_path):
    model = RecordingToolModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "delegate",
                            "subagent_type": "general-purpose",
                        },
                        "id": "stale-task",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="stopped"),
        ]
    )

    assert (
        run_task(
            model=model,
            worktree=str(tmp_path),
            task="repair",
            repair_mode=True,
        )
        == "stopped"
    )
    assert all("task" not in surface for surface in model.surfaces)
    tool_messages = [
        message
        for invocation in model.messages
        for message in invocation
        if getattr(message, "type", None) == "tool"
    ]
    assert any("not a valid tool" in str(message.content) for message in tool_messages)


def test_default_subagent_reproduces_async_structured_tool_sync_failure(tmp_path):
    from deepagents import create_deep_agent
    from langchain_core.tools import StructuredTool

    async def async_probe(operation_id: str) -> str:
        return operation_id

    probe = StructuredTool.from_function(
        coroutine=async_probe,
        name="acceptance_retryable_probe",
        description="Async-only acceptance probe.",
    )
    model = RecordingToolModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "invoke the probe",
                            "subagent_type": "general-purpose",
                        },
                        "id": "delegate-probe",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "acceptance_retryable_probe",
                        "args": {"operation_id": "repair"},
                        "id": "sync-probe",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    agent = create_deep_agent(
        model=model,
        tools=[probe],
        subagents=[],
        backend=LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True),
    )

    with pytest.raises(
        NotImplementedError, match="StructuredTool does not support sync invocation"
    ):
        agent.invoke({"messages": [{"role": "user", "content": "repair"}]})
    assert "task" in model.surfaces[0]


def test_repair_cannot_reach_async_tool_through_stale_task_call(tmp_path):
    from langchain_core.tools import StructuredTool

    from sweforge.agent import _build_backend, _create_repair_agent

    calls = []

    async def async_probe(operation_id: str) -> str:
        calls.append(operation_id)
        return operation_id

    probe = StructuredTool.from_function(
        coroutine=async_probe,
        name="acceptance_retryable_probe",
        description="Async-only acceptance probe.",
    )
    model = RecordingToolModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "invoke the probe",
                            "subagent_type": "general-purpose",
                        },
                        "id": "stale-delegation",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="rejected"),
        ]
    )
    agent = _create_repair_agent(
        model=model,
        tools=[probe],
        backend=_build_backend(str(tmp_path)),
        system_prompt="repair directly",
        memory=None,
        skills=None,
        permissions=None,
        store=None,
        context_schema=None,
        checkpointer=None,
        middleware=[],
    )

    result = agent.invoke({"messages": [{"role": "user", "content": "repair"}]})
    assert result["messages"][-1].content == "rejected"
    assert all("task" not in surface for surface in model.surfaces)
    assert calls == []


def test_repair_filesystem_and_execute_record_evidence(tmp_path):
    observations = []
    model = RecordingToolModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/repair.txt",
                            "content": "repair-file\n",
                        },
                        "id": "write-repair",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/repair.txt"},
                        "id": "read-repair",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": "printf 'repair-evidence\\n'"},
                        "id": "execute-repair",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="repair complete"),
        ]
    )
    context = RepoAgentContext(1, "example/repo", "github:1:issue:7")

    result = run_task(
        model=model,
        worktree=str(tmp_path),
        task="repair",
        repo_context=context,
        secure_execution=True,
        sandbox_backend_provider=lambda **_: LocalShellBackend(
            root_dir=str(tmp_path), virtual_mode=True
        ),
        execution_evidence_sink=lambda **item: observations.append(item),
        repair_mode=True,
    )

    assert result == "repair complete"
    assert (tmp_path / "repair.txt").read_text() == "repair-file\n"
    assert len(observations) == 1, [
        (getattr(message, "name", None), str(message.content))
        for invocation in model.messages
        for message in invocation
        if getattr(message, "type", None) == "tool"
    ]
    assert observations[0]["command"] == "printf 'repair-evidence\\n'"
    assert observations[0]["exit_code"] == 0
    assert observations[0]["output"] == "repair-evidence\n"


def test_concurrent_initial_and_repair_tool_surfaces_are_isolated(tmp_path):
    for _ in range(10):
        barrier = Barrier(2)
        initial_model = RecordingToolModel(
            [AIMessage(content="initial")], barrier=barrier
        )
        repair_model = RecordingToolModel(
            [AIMessage(content="repair")], barrier=barrier
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            initial = pool.submit(
                run_task,
                model=initial_model,
                worktree=str(tmp_path),
                task="initial",
            )
            repair = pool.submit(
                run_task,
                model=repair_model,
                worktree=str(tmp_path),
                task="repair",
                repair_mode=True,
            )
            assert initial.result(timeout=10) == "initial"
            assert repair.result(timeout=10) == "repair"
        assert "task" in initial_model.surfaces[-1]
        assert "task" not in repair_model.surfaces[-1]


def test_normalize_task_removes_only_invocation_token():
    assert normalize_task("@agent fix the failing test") == "fix the failing test"
    assert normalize_task("Please @AGENT investigate this") == "Please investigate this"
    with pytest.raises(ValueError, match="did not contain a task"):
        normalize_task("@agent")


def test_run_task_resumes_checkpointed_event_without_new_message(monkeypatch):
    calls = []

    class FakeAgent:
        def get_state(self, config):
            return SimpleNamespace(
                values={"messages": [SimpleNamespace(id="event-id")]}
            )

        def invoke(self, state, config=None, durability=None):
            calls.append((state, config, durability))
            return {"messages": [SimpleNamespace(content="resumed")]}

    monkeypatch.setattr(
        "sweforge.agent.create_deep_agent", lambda **kwargs: FakeAgent()
    )
    assert (
        run_task(
            model="provider:model",
            worktree="/tmp/worktree",
            task="continue",
            thread_id="github:1:issue:7",
            checkpointer=object(),
            message_id="event-id",
            resume_if_present=True,
        )
        == "resumed"
    )
    assert calls == [
        (None, {"configurable": {"thread_id": "github:1:issue:7"}}, "sync")
    ]


def test_run_task_delivers_missing_event_with_stable_human_message(monkeypatch):
    calls = []

    class FakeAgent:
        def get_state(self, config):
            return SimpleNamespace(values={"messages": []})

        def invoke(self, state, config=None, durability=None):
            calls.append((state, config, durability))
            return {"messages": [SimpleNamespace(content="new")]}

    monkeypatch.setattr(
        "sweforge.agent.create_deep_agent", lambda **kwargs: FakeAgent()
    )
    run_task(
        model="provider:model",
        worktree="/tmp/worktree",
        task="new task",
        thread_id="github:1:issue:7",
        checkpointer=object(),
        message_id="event-id",
    )
    message = calls[0][0]["messages"][0]
    assert message.id == "event-id"
    assert message.content == "new task"
    assert calls[0][2] == "sync"


def test_deep_agents_message_reducer_replaces_duplicate_ids():
    from deepagents.graph import _messages_delta_reducer
    from langchain_core.messages import HumanMessage

    result = _messages_delta_reducer(
        [HumanMessage(content="first", id="same")],
        [[HumanMessage(content="replacement", id="same")]],
    )
    assert [(message.id, message.content) for message in result] == [
        ("same", "replacement")
    ]


def test_event_message_ids_are_stable_and_distinct():
    assert event_message_id("one") == event_message_id("one")
    assert event_message_id("one") != event_message_id("two")


def test_first_event_creates_persistent_workspace_and_is_idempotent(tmp_path):
    source = repository(tmp_path)
    repo = RepositoryRef(123, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent edit it"
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(store, event)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        Path(kwargs["worktree"], "change.txt").write_text("agent change\n")
        return "completed"

    result = execute_one(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=runner,
    )
    assert result.status == "SUCCEEDED"
    assert result.workspace_created
    assert result.changed_files == ("change.txt",)
    assert calls[0]["thread_id"] == "github:123:issue:7"
    assert not (source / "change.txt").exists()
    metadata = store.thread_workspace("github:123:issue:7")
    assert metadata is not None
    assert metadata.branch_name == "sweforge/issue-7"
    assert metadata.base_commit == git("rev-parse", "HEAD", cwd=source)
    assert (
        execute_one(
            store=store,
            model="provider:model",
            repo_paths={repo.full_name: source},
            workspace_root=tmp_path / "workspaces",
            lock_root=tmp_path / "locks",
            checkpointer=object(),
            runner=runner,
        ).status
        == "NO_WORK"
    )
    assert len(calls) == 1
    store.close()


def test_follow_up_reuses_workspace_and_checkpoint_thread(tmp_path):
    source = repository(tmp_path)
    repo = RepositoryRef(123, "owner/repo")
    first = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent first"
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(store, first)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            Path(kwargs["worktree"], "first.txt").write_text("preserve\n")
        return f"run {len(calls)}"

    common = dict(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=runner,
    )
    assert execute_one(**common).status == "SUCCEEDED"
    workspace = tmp_path / "workspaces/123/issue-7"
    git("add", "first.txt", cwd=workspace)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "publish first",
        cwd=workspace,
    )
    second = replace(
        first,
        source_id="2",
        source_updated_at="2026-01-01T00:01:00Z",
        body="@agent follow up",
    )
    persist(store, second)
    assert execute_one(**common).status == "SUCCEEDED"
    assert calls[0]["worktree"] == calls[1]["worktree"]
    assert calls[0]["thread_id"] == calls[1]["thread_id"]
    assert Path(calls[1]["worktree"], "first.txt").read_text() == "preserve\n"
    store.close()


def test_workspace_namespaces_and_unrouted_events(tmp_path):
    first_source = repository(tmp_path, "one")
    second_source = repository(tmp_path, "two")
    first = RepositoryRef(1, "owner/one")
    second = RepositoryRef(2, "owner/two")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(
        store,
        source_event(
            first, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent one"
        ),
    )
    persist(
        store,
        source_event(
            second, source_id="2", updated="2026-01-01T00:00:00Z", body="@agent two"
        ),
    )
    persist(
        store,
        source_event(
            first,
            source_id="3",
            updated="2026-01-01T00:02:00Z",
            body="@agent pr",
            subject_kind=SubjectKind.PULL_REQUEST,
            number=12,
        ),
        "review_comments",
    )
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return "ok"

    common = dict(
        store=store,
        model="provider:model",
        repo_paths={first.full_name: first_source, second.full_name: second_source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=runner,
    )
    assert execute_one(**common).event.repo_id == 1
    assert execute_one(**common).event.repo_id == 2
    assert execute_one(**common).status == "NO_WORK"
    assert len(calls) == 2
    assert calls[0]["worktree"] != calls[1]["worktree"]
    assert store.thread_workspace("github:1:issue:12") is None
    store.close()


def test_mapped_pr_event_reuses_issue_thread(tmp_path):
    source = repository(tmp_path)
    repo = RepositoryRef(123, "owner/repo")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    issue = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent issue"
    )
    persist(store, issue)
    pr = source_event(
        repo,
        source_id="2",
        updated="2026-01-01T00:01:00Z",
        body="@agent review",
        subject_kind=SubjectKind.PULL_REQUEST,
        number=12,
    )
    persist(store, pr, "review_comments")
    store.register_pr_mapping(repo.repo_id, 12, "github:123:issue:7")
    calls = []
    result = execute_one(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=lambda **kwargs: calls.append(kwargs) or "ok",
    )
    assert result.status == "SUCCEEDED"
    assert calls[0]["thread_id"] == "github:123:issue:7"
    assert result.workspace.path == Path(tmp_path / "workspaces/123/issue-7")
    store.close()


def test_claim_order_and_concurrency_idempotency(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    first = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent first"
    )
    second = source_event(
        repo, source_id="2", updated="2026-01-01T00:01:00Z", body="@agent second"
    )
    persist(store, first)
    persist(store, second)
    claim = store.claim_next_event(now="2026-01-01T00:02:00Z")
    assert claim.event_key == first.event_key
    assert store.claim_next_event(now="2026-01-01T00:02:00Z") is None
    store.mark_execution_succeeded(
        first.event_key,
        completed_at="2026-01-01T00:03:00Z",
        response_text="ok",
        workspace_path="/tmp/workspace",
    )
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            "github:1:issue:7",
            1,
            repo.full_name,
            7,
            "/tmp/repository",
            "/tmp/workspace",
            "sweforge/issue-7",
            "base",
            "now",
            "now",
        )
    )
    assert (
        store.claim_next_event(now="2026-01-01T00:04:00Z").event_key == second.event_key
    )
    assert store.claim_next_event(now="2026-01-01T00:05:00Z") is None
    store.close()


def test_thread_locks_are_per_thread_and_nonblocking(tmp_path):
    with thread_lock(tmp_path / "locks", "github:1:issue:7"):
        with pytest.raises(ThreadLockUnavailable):
            with thread_lock(tmp_path / "locks", "github:1:issue:7"):
                pass
        with thread_lock(tmp_path / "locks", "github:1:issue:8"):
            pass


def test_failure_preserves_workspace_and_redacts_environment(tmp_path, monkeypatch):
    source = repository(tmp_path)
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent fail"
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(store, event)
    secret = "super-secret-token"
    monkeypatch.setenv("TEST_EXECUTION_SECRET", secret)

    def runner(**kwargs):
        raise RuntimeError(secret)

    result = execute_one(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=runner,
    )
    assert result.status == "FAILED"
    assert result.workspace is not None and result.workspace.path.exists()
    row = store.execution_for_event(event.event_key)
    assert row["status"] == ExecutionStatus.FAILED.value
    assert secret not in row["error_message"]
    store.close()


def test_empty_task_is_failed_without_runner(tmp_path):
    source = repository(tmp_path)
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent"
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(store, event)
    called = False

    def runner(**kwargs):
        nonlocal called
        called = True
        return "bad"

    result = execute_one(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=runner,
    )
    assert result.status == "FAILED"
    assert not called
    assert "did not contain a task" in result.error
    assert not (tmp_path / "workspaces").exists()
    store.close()


def test_store_and_checkpoint_reopen_persist_state(tmp_path):
    source = repository(tmp_path)
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent run"
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    persist(store, event)
    execute_one(
        store=store,
        model="provider:model",
        repo_paths={repo.full_name: source},
        workspace_root=tmp_path / "workspaces",
        lock_root=tmp_path / "locks",
        checkpointer=object(),
        runner=lambda **kwargs: "ok",
    )
    metadata = store.thread_workspace("github:1:issue:7")
    store.close()
    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    assert reopened.execution_for_event(event.event_key)["status"] == "SUCCEEDED"
    assert reopened.thread_workspace("github:1:issue:7") == metadata
    reopened.close()

    class State(TypedDict):
        value: str

    def node(state: State):
        return {"value": state["value"]}

    checkpoint_path = tmp_path / "checkpoints.sqlite"
    with SQLiteCheckpointer(checkpoint_path) as saver:
        graph = StateGraph(State)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        compiled = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "github:1:issue:7"}}
        compiled.invoke({"value": "persisted"}, config)
    with SQLiteCheckpointer(checkpoint_path) as saver:
        graph = StateGraph(State)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        compiled = graph.compile(checkpointer=saver)
        assert compiled.get_state(config).values == {"value": "persisted"}


def test_stale_running_recovery_uses_free_lock_and_age(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent run"
    )
    persist(store, event)
    store.claim_next_event(now="2026-01-01T00:00:00Z")
    recovered = recover_stale(
        store=store,
        lock_root=tmp_path / "locks",
        older_than_seconds=60,
        now=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    )
    assert recovered == [event.event_key]
    assert store.execution_for_event(event.event_key)["status"] == "INTERRUPTED"
    assert store.claim_next_event(now="2026-01-01T00:03:00Z") is None
    assert store.retry_execution(event.event_key).value == "RETRY_PENDING"
    assert (
        store.claim_next_event(now="2026-01-01T00:04:00Z").event_key == event.event_key
    )
    assert store.execution_for_event(event.event_key)["attempt_count"] == 2
    store.mark_execution_failed(
        event.event_key,
        completed_at="2026-01-01T00:05:00Z",
        error_message="retry failed",
        workspace_path=None,
    )
    store.skip_execution(
        event.event_key,
        completed_at="2026-01-01T00:06:00Z",
        reason="operator skip",
    )
    assert store.execution_for_event(event.event_key)["status"] == "SKIPPED"
    store.close()


def test_live_or_recent_running_execution_is_not_recovered(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent run"
    )
    persist(store, event)
    store.claim_next_event(now="2026-01-01T00:00:00Z")
    current = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    assert (
        recover_stale(
            store=store,
            lock_root=tmp_path / "locks",
            older_than_seconds=60,
            now=current,
        )
        == []
    )
    with thread_lock(tmp_path / "held-locks", "github:1:issue:7"):
        assert (
            recover_stale(
                store=store,
                lock_root=tmp_path / "held-locks",
                older_than_seconds=0,
                now=current,
            )
            == []
        )
    assert store.execution_for_event(event.event_key)["status"] == "RUNNING"
    store.close()


def test_failure_blocks_later_until_explicit_retry_or_skip(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    first = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent first"
    )
    second = source_event(
        repo, source_id="2", updated="2026-01-01T00:01:00Z", body="@agent second"
    )
    persist(store, first)
    persist(store, second)
    store.claim_next_event(now="2026-01-01T00:02:00Z")
    store.mark_execution_failed(
        first.event_key,
        completed_at="2026-01-01T00:03:00Z",
        error_message="failed",
        workspace_path=None,
    )
    assert store.claim_next_event(now="2026-01-01T00:04:00Z") is None
    assert store.retry_execution(first.event_key).value == "RETRY_PENDING"
    retry = store.claim_next_event(now="2026-01-01T00:05:00Z")
    assert retry.event_key == first.event_key
    assert store.execution_for_event(first.event_key)["attempt_count"] == 2
    store.mark_execution_failed(
        first.event_key,
        completed_at="2026-01-01T00:06:00Z",
        error_message="failed again",
        workspace_path=None,
    )
    store.skip_execution(
        first.event_key, completed_at="2026-01-01T00:07:00Z", reason="operator skip"
    )
    assert (
        store.claim_next_event(now="2026-01-01T00:08:00Z").event_key == second.event_key
    )
    store.close()


def test_pull_request_event_claim_uses_original_issue_workspace_number(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    issue = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent issue"
    )
    persist(store, issue)
    pull_request = source_event(
        repo,
        source_id="2",
        updated="2026-01-01T00:01:00Z",
        body="@agent follow up",
        subject_kind=SubjectKind.PULL_REQUEST,
        number=42,
    )
    persist(store, pull_request, "review_comments")
    store.register_pr_mapping(repo.repo_id, 42, issue.thread_id or "github:1:issue:7")
    store.claim_next_event(now="first")
    store.mark_execution_succeeded(
        issue.event_key,
        completed_at="first",
        response_text="done",
        workspace_path="/workspace",
        start_head_sha="base",
        end_head_sha="base",
        end_dirty=False,
    )
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            "github:1:issue:7",
            repo.repo_id,
            repo.full_name,
            7,
            "/repository",
            "/workspace",
            "sweforge/issue-7",
            "base",
            "first",
            "first",
        )
    )
    claim = store.claim_next_event(now="second")
    assert claim and claim.event_key == pull_request.event_key
    assert claim.issue_number == 7
    store.close()


def test_retry_and_skip_status_transitions_are_safe(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "owner/repo")
    event = source_event(
        repo, source_id="1", updated="2026-01-01T00:00:00Z", body="@agent run"
    )
    persist(store, event)
    store.claim_next_event(now="now")
    with pytest.raises(ValueError):
        store.skip_execution(event.event_key, completed_at="now", reason="no")
    store.mark_execution_failed(
        event.event_key, completed_at="now", error_message="bad", workspace_path=None
    )
    assert store.retry_execution(event.event_key).value == "RETRY_PENDING"
    assert store.retry_execution(event.event_key).value == "RETRY_PENDING"
    store.claim_next_event(now="now")
    store.mark_execution_succeeded(
        event.event_key, completed_at="now", response_text="ok", workspace_path="/w"
    )
    with pytest.raises(ValueError):
        store.retry_execution(event.event_key)
    with pytest.raises(ValueError):
        store.skip_execution(event.event_key, completed_at="now", reason="no")
    store.close()
