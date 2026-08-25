"""Resume identity: one interrupt occurrence, one human answer.

LangGraph resolves `Command(resume=...)` against whichever interrupt is
currently pending, and it replays the interrupted node from the beginning.  So
the answer SWEForge hands back must be selected by the pending occurrence, not
by clarification ordering: picking "the newest answered clarification" silently
feeds one question's answer into a different question.
"""

import subprocess
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from harness.github_fake import FakeGitHub
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from sweforge.context import RepoAgentContext
from sweforge.execution import SQLiteCheckpointer
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import ClarificationStatus, SQLiteGitHubStore, WorkflowPhase
from sweforge.server import ServerConfig, SWEForgeServer
from sweforge.workflow import WorkflowEngine

THREAD_ID = "github:1:issue:7"
# A fixed clock makes both clarifications share one created_at, which is what
# exposes ordering-based selection.  Production clocks only hide the defect.
FIXED_CLOCK = "2026-01-01T00:00:30Z"


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def source_event(repo, source_id, body, created):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=source_id,
        source_updated_at=created,
        source_created_at=created,
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="octocat",
        body=body,
        html_url=None,
    )


class State(TypedDict):
    messages: Annotated[list, add_messages]


def two_question_agent(resumed):
    """A real LangGraph graph that asks two distinct clarifications."""

    def node(state: State):
        region = interrupt(
            {
                "question": "Which region?",
                "reason": "two regions are valid",
                "answer_type": "CHOICE",
                "choices": ["us-east-1", "eu-west-1"],
                "occurrence_key": "tool-call-region",
            }
        )
        resumed.append(("tool-call-region", region))
        database = interrupt(
            {
                "question": "Which database?",
                "reason": "two databases are valid",
                "answer_type": "CHOICE",
                "choices": ["postgres", "mysql"],
                "occurrence_key": "tool-call-database",
            }
        )
        resumed.append(("tool-call-database", database))
        return {"messages": [AIMessage(content=f"used {region} and {database}")]}

    def factory(**kwargs):
        graph = StateGraph(State, context_schema=RepoAgentContext)
        graph.add_node("work", node)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile(checkpointer=kwargs.get("checkpointer"))

    return factory


def clarification_fixture(tmp_path, monkeypatch, resumed):
    monkeypatch.setattr("sweforge.agent.create_deep_agent", two_question_agent(resumed))
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("base\n")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/repo")
    store.upsert_repository(1, repo.full_name, "now")
    root = source_event(repo, "1", "@agent deploy the service", "2026-01-01T00:00:00Z")
    approval = source_event(repo, "2", "@agent approve", "2026-01-01T01:00:00Z")
    store.record_batch(
        1, "issue_comments", [root, approval], since="now", etag=None, polled_at="now"
    )
    engine = WorkflowEngine(store=store, client=FakeGitHub(), clock=lambda: FIXED_CLOCK)
    engine.start_cycle(
        event_key=root.event_key, plan_text="deploy", posted_comment_id=1
    )
    permit = engine.approve(event_key=approval.event_key)
    checkpointer = SQLiteCheckpointer(tmp_path / "checkpoints.db")
    kwargs = {
        "model": "provider:model",
        "repo_paths": {repo.full_name: source},
        "workspace_root": tmp_path / "workspaces",
        "lock_root": tmp_path / "locks",
        "checkpointer": checkpointer.saver,
        "secure_execution": False,
        "unsafe_local_shell": True,
    }
    return store, engine, repo, permit, checkpointer, kwargs


def answer(store, engine, repo, source_id, body, created):
    event = source_event(repo, source_id, body, created)
    store.record_batch(
        1, "issue_comments", [event], since="now", etag=None, polled_at="now"
    )
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    return event


def test_each_interrupt_occurrence_is_resumed_with_its_own_answer(
    tmp_path, monkeypatch
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )

    first = engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    assert first.status == "CLARIFICATION"
    assert first.clarification.occurrence_key == "tool-call-region"
    assert store.workflow_state(THREAD_ID).phase is WorkflowPhase.WAITING_FOR_INPUT

    answer(store, engine, repo, "3", "@agent us-east-1", "2026-01-01T02:00:00Z")
    assert store.workflow_state(THREAD_ID).phase is WorkflowPhase.EXECUTION_READY

    second = engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    assert second.status == "CLARIFICATION"
    assert second.clarification.occurrence_key == "tool-call-database"
    assert resumed == [("tool-call-region", "us-east-1")]

    answer(store, engine, repo, "4", "@agent postgres", "2026-01-01T03:00:00Z")
    third = engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    assert third.status == "SUCCEEDED"

    # Both clarifications share one created_at, so any ordering-based selection
    # would hand the database question the region answer.
    rows = store.connection.execute(
        "SELECT occurrence_key, created_at FROM clarification_requests"
    ).fetchall()
    assert {row["created_at"] for row in rows} == {FIXED_CLOCK}
    assert resumed == [
        ("tool-call-region", "us-east-1"),
        # LangGraph replays the node, so the first occurrence is re-served its
        # own recorded answer rather than the newer one.
        ("tool-call-region", "us-east-1"),
        ("tool-call-database", "postgres"),
    ]
    assert third.response == "used us-east-1 and postgres"
    checkpointer.close()
    store.close()


def test_resume_fails_closed_when_the_pending_occurrence_has_no_answer(
    tmp_path, monkeypatch
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    answer(store, engine, repo, "3", "@agent us-east-1", "2026-01-01T02:00:00Z")
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)

    # The database question is pending and unanswered while the region answer
    # is still ANSWERED for this cycle.  Force another authorized run.
    store.connection.execute(
        "UPDATE issue_workflow_state SET phase=? WHERE thread_id=?",
        (WorkflowPhase.EXECUTION_READY.value, THREAD_ID),
    )
    store.connection.execute(
        "UPDATE execution_permits SET consumed_at=NULL WHERE permit_id=?",
        (permit.permit_id,),
    )
    store.connection.execute(
        "UPDATE logical_executions SET status='RETRY_PENDING' WHERE thread_id=?",
        (THREAD_ID,),
    )
    store.connection.execute(
        "UPDATE event_executions SET status='RETRY_PENDING' WHERE thread_id=?",
        (THREAD_ID,),
    )
    store.connection.commit()

    replayed = engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    assert replayed.status == "CLARIFICATION"
    assert replayed.clarification.occurrence_key == "tool-call-database"
    # The region answer was never handed to the database question, and the
    # checkpoint was not advanced at all, so the node did not replay again.
    assert resumed == [("tool-call-region", "us-east-1")]
    pending = store.clarification_for_thread(THREAD_ID)
    assert pending.occurrence_key == "tool-call-database"
    assert pending.status == ClarificationStatus.OPEN.value
    checkpointer.close()
    store.close()


def test_unrelated_clarification_input_is_deferred_and_routed_once(
    tmp_path, monkeypatch
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    calls = 0

    def classify(**_):
        nonlocal calls
        calls += 1
        return {"relationship": "UNRELATED_FOLLOWUP", "extracted_answer": ""}

    engine.clarification_classifier = classify
    event = answer(
        store, engine, repo, "3", "@agent unrelated work", "2026-01-01T02:00:00Z"
    )
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    assert calls == 1
    assert len(store.deferred_followups(THREAD_ID)) == 1
    assert (
        store.clarification_for_thread(THREAD_ID).status
        == ClarificationStatus.OPEN.value
    )
    assert not store.is_thread_runnable(THREAD_ID, now=FIXED_CLOCK)
    disposition = store.input_consumption(event.event_key)
    assert disposition.purpose.value == "CLARIFICATION_ROUTED"
    checkpointer.close()
    store.close()


def test_ambiguous_clarification_input_is_routed_once_and_later_answer_wakes(
    tmp_path, monkeypatch
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    clarification_id = store.clarification_for_thread(THREAD_ID).clarification_id
    calls = 0

    def classify(**_):
        nonlocal calls
        calls += 1
        return {"relationship": "AMBIGUOUS", "extracted_answer": ""}

    engine.clarification_classifier = classify
    ambiguous = answer(
        store,
        engine,
        repo,
        "3",
        "@agent probably the first one",
        "2026-01-01T02:00:00Z",
    )
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    assert calls == 1
    assert store.input_consumption(ambiguous.event_key).purpose.value == (
        "CLARIFICATION_ROUTED"
    )
    assert (
        store.clarification_for_thread(THREAD_ID).status
        == ClarificationStatus.OPEN.value
    )
    assert not store.is_thread_runnable(THREAD_ID, now=FIXED_CLOCK)

    valid = source_event(repo, "4", "@agent us-east-1", "2026-01-01T03:00:00Z")
    store.record_batch(
        1, "issue_comments", [valid], since="now", etag=None, polled_at="now"
    )
    assert store.is_thread_runnable(THREAD_ID, now=FIXED_CLOCK)
    engine.clarification_classifier = lambda **_: {
        "relationship": "ANSWERS_CLARIFICATION",
        "extracted_answer": "us-east-1",
    }
    engine._resolve_open_clarification(store.workflow_state(THREAD_ID))
    assert (
        store.clarification(clarification_id).status
        == ClarificationStatus.ANSWERED.value
    )
    assert store.workflow_state(THREAD_ID).phase == WorkflowPhase.EXECUTION_READY
    checkpointer.close()
    store.close()


@pytest.mark.parametrize("relationship", ["UNRELATED_FOLLOWUP", "AMBIGUOUS"])
def test_once_terminates_after_disposing_human_wait_input(
    tmp_path, monkeypatch, relationship
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, _kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **_kwargs)
    event = source_event(
        repo, "3", "@agent probably the first one", "2026-01-01T02:00:00Z"
    )
    store.record_batch(
        1, "issue_comments", [event], since="now", etag=None, polled_at="now"
    )
    state_db = store.path
    store.close()
    checkpointer.close()
    calls = 0

    def worker(thread_id):
        nonlocal calls
        worker_store = SQLiteGitHubStore(state_db)

        def classify(**_):
            nonlocal calls
            calls += 1
            return {"relationship": relationship, "extracted_answer": ""}

        worker_engine = WorkflowEngine(
            store=worker_store, clarification_classifier=classify
        )
        worker_engine._resolve_open_clarification(
            worker_store.workflow_state(thread_id)
        )
        worker_store.close()

    class Poller:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, repositories):
            return None

    class Client:
        def close(self):
            pass

    config = ServerConfig(
        repositories=(repo.full_name,),
        repo_paths={repo.full_name: tmp_path},
        db=state_db,
        model="test-model",
        once=True,
    )
    SWEForgeServer(
        config,
        client_factory=lambda _: (Client(), None),
        poller_factory=Poller,
        worker_runner=worker,
    ).run()
    assert calls == 1
    final_store = SQLiteGitHubStore(state_db)
    assert not final_store.is_thread_runnable("github:1:issue:7", now=FIXED_CLOCK)
    assert final_store.input_consumption(event.event_key) is not None
    final_store.close()


def test_answered_clarification_lookup_is_occurrence_exact(tmp_path, monkeypatch):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    answer(store, engine, repo, "3", "@agent us-east-1", "2026-01-01T02:00:00Z")

    assert (
        store.answered_clarification_for_occurrence(
            thread_id=THREAD_ID, cycle_id=1, occurrence_key="tool-call-region"
        ).answer_json
        == '{"answer": "us-east-1", "residual": null}'
    )
    # No cross-occurrence, cross-cycle or empty-key match is ever returned.
    assert (
        store.answered_clarification_for_occurrence(
            thread_id=THREAD_ID, cycle_id=1, occurrence_key="tool-call-database"
        )
        is None
    )
    assert (
        store.answered_clarification_for_occurrence(
            thread_id=THREAD_ID, cycle_id=2, occurrence_key="tool-call-region"
        )
        is None
    )
    assert (
        store.answered_clarification_for_occurrence(
            thread_id=THREAD_ID, cycle_id=1, occurrence_key=""
        )
        is None
    )
    checkpointer.close()
    store.close()


def test_legacy_answer_without_occurrence_key_resumes_only_while_unambiguous(
    tmp_path, monkeypatch
):
    resumed: list[tuple[str, str]] = []
    store, engine, repo, permit, checkpointer, kwargs = clarification_fixture(
        tmp_path, monkeypatch, resumed
    )
    engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    answer(store, engine, repo, "3", "@agent us-east-1", "2026-01-01T02:00:00Z")
    # Simulate a row written before occurrence keys existed.
    store.connection.execute(
        "UPDATE clarification_requests SET occurrence_key='' WHERE thread_id=?",
        (THREAD_ID,),
    )
    store.connection.commit()
    assert (
        store.sole_answered_legacy_clarification(thread_id=THREAD_ID, cycle_id=1)
        is not None
    )

    resumed_run = engine.execute_authorized(permit_id=permit.permit_id, **kwargs)
    assert resumed_run.status == "CLARIFICATION"
    assert resumed[0] == ("tool-call-region", "us-east-1")

    # A second legacy answer in the same cycle is ambiguous and fails closed.
    store.connection.execute(
        """INSERT INTO clarification_requests(
           clarification_id, thread_id, cycle_id, root_event_key, occurrence_key,
           requested_from_phase, question, reason, answer_type, choices_json,
           origin_surface, response_subject_number, status, created_at,
           answered_at, answer_event_key, answer_json)
           VALUES(?,?,?,?,'',?,?,?,?,'[]','ISSUE',7,?,?,?,NULL,?)""",
        (
            "clarification-legacy-second",
            THREAD_ID,
            1,
            store.workflow_state(THREAD_ID).root_event_key,
            "EXECUTING",
            "Which database?",
            "r",
            "CHOICE",
            ClarificationStatus.ANSWERED.value,
            FIXED_CLOCK,
            FIXED_CLOCK,
            '{"answer": "mysql"}',
        ),
    )
    store.connection.commit()
    assert (
        store.sole_answered_legacy_clarification(thread_id=THREAD_ID, cycle_id=1)
        is None
    )
    checkpointer.close()
    store.close()


def test_clarification_tool_is_exposed_only_when_a_sink_is_supplied(monkeypatch):
    """The sink is the switch that registers request_clarification."""
    captured: dict[str, list[str]] = {}

    def factory(**kwargs):
        captured["tools"] = [
            getattr(item, "name", str(item)) for item in kwargs.get("tools", [])
        ]

        class Agent:
            def get_state(self, config):
                return type("Snapshot", (), {"tasks": ()})()

            def invoke(self, state, **kwargs):
                return {"messages": [type("Message", (), {"content": "done"})()]}

        return Agent()

    monkeypatch.setattr("sweforge.agent.create_deep_agent", factory)
    from sweforge.agent import run_task

    run_task(
        model="provider:model",
        worktree="/tmp",
        task="t",
        thread_id="thread-1",
        checkpointer=object(),
        interrupt_result_sink=lambda payload: None,
    )
    assert captured["tools"] == ["request_clarification"]

    run_task(
        model="provider:model",
        worktree="/tmp",
        task="t",
        thread_id="thread-1",
        checkpointer=object(),
    )
    assert captured["tools"] == []


def test_review_repair_run_does_not_expose_clarification(tmp_path, monkeypatch):
    """Review findings are internal repair input; repair may not ask the human.

    A repair shares the IssueThread's LangGraph checkpoint, so an interrupt
    raised there would strand a pending occurrence on the same thread that the
    initial execution resumes against.
    """
    from test_review_execution_completion import execution_ready_fixture

    store, engine, repo, root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path
    )
    runner_calls: list[dict] = []

    def runner(**kwargs):
        runner_calls.append(kwargs)
        Path(kwargs["worktree"], "fixed.txt").write_text("fixed\n")
        return "executor response"

    execute_kwargs["runner"] = runner
    advance_kwargs = {
        "thread_id": thread_id,
        "model": "planning-sonnet",
        "review_model": "review-sonnet",
        "repo_paths": execute_kwargs["repo_paths"],
        "workspace_root": execute_kwargs["workspace_root"],
        "execute_kwargs": execute_kwargs,
    }
    assert engine.advance(**advance_kwargs).phase is WorkflowPhase.REVIEW_EXECUTION
    assert len(runner_calls) == 1
    # The initial authorized execution may ask the human.
    assert "interrupt_result_sink" in runner_calls[0]

    from sweforge.reviewer import ExecutionReviewResult

    verdicts = iter(
        [
            ExecutionReviewResult(verdict="NEEDS_FIXES", summary="fix it"),
            ExecutionReviewResult(verdict="ACCEPT", summary="good"),
        ]
    )
    engine.reviewer = lambda **_: next(verdicts)
    assert engine.advance(**advance_kwargs).phase is WorkflowPhase.REPAIR_READY
    assert engine.advance(**advance_kwargs).phase is WorkflowPhase.REVIEW_EXECUTION
    assert len(runner_calls) == 2
    # The repair run must not be able to raise a clarification interrupt.
    assert "interrupt_result_sink" not in runner_calls[1]
    assert "resume_resolver" not in runner_calls[1]
    store.close()
