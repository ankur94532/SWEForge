import json
import sqlite3
from dataclasses import replace
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sweforge.agent import run_task
from sweforge.execution import ClarificationRequestProposal
from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    ClarificationRequestRecord,
    ClarificationStatus,
    InputPurpose,
    SQLiteGitHubStore,
    WorkflowPhase,
    execution_id_for,
)
from sweforge.workflow import WorkflowEngine


def _events(repo: RepositoryRef) -> list[SourceEvent]:
    common = dict(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_updated_at="2026-01-01T00:00:00Z",
        source_created_at="2026-01-01T00:00:00Z",
        subject_kind=SubjectKind.ISSUE,
        subject_number=7,
        author_login="operator",
        html_url=None,
    )
    return [
        SourceEvent(source_id="root", body="@agent fix the scheduler", **common),
        SourceEvent(
            source_id="followup",
            body="@agent also update the README",
            **{
                **common,
                "source_updated_at": "2026-01-01T00:00:01Z",
                "source_created_at": "2026-01-01T00:00:01Z",
            },
        ),
    ]


def _store_with_cycle(tmp_path):
    repo = RepositoryRef(101, "owner/repo")
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    events = _events(repo)
    store.record_batch(
        repo.repo_id, "issues", events, since="now", etag=None, polled_at="now"
    )
    root = store.source_event(events[0].event_key)
    assert root is not None
    engine = WorkflowEngine(store=store)
    engine.start_cycle(event_key=root["event_key"], plan_text="plan")
    return store, repo, events


def test_active_followup_is_durable_and_not_an_unconsumed_active_input(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    # The source event key is the stable lookup; use the thread recorded by the event.
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    state = store.workflow_state(thread_id)
    assert state is not None
    store.save_workflow_state(replace(state, phase=WorkflowPhase.EXECUTING))
    engine = WorkflowEngine(store=store)
    engine._defer_active_followups(store.workflow_state(thread_id))
    assert (
        store.unconsumed_inputs(thread_id, after_event_key=state.root_event_key) == []
    )
    queued = store.deferred_followups(thread_id)
    assert [row["event_key"] for row in queued] == [events[1].event_key]
    assert len(engine.pending_planning_inputs(thread_id)) == 1
    engine._acknowledge_delivered(
        events[1].event_key,
        thread_id=thread_id,
        cycle_id=1,
        purpose=InputPurpose.PLANNING_INPUT,
    )
    assert engine.pending_planning_inputs(thread_id) == []
    assert engine.pending_planning_inputs(thread_id) == []
    store.close()


def test_planning_delivers_and_acknowledges_sibling_deferred_ids_exactly(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    first = store.defer_followup(
        source_event_key=events[1].event_key,
        thread_id=thread_id,
        originating_cycle_id=1,
        queued_at="one",
        residual_text="update README",
    )
    second = store.defer_followup(
        source_event_key=events[1].event_key,
        thread_id=thread_id,
        originating_cycle_id=1,
        queued_at="two",
        residual_text="add tests",
    )
    state = store.workflow_state(thread_id)
    store.save_workflow_state(replace(state, phase=WorkflowPhase.EXECUTING))
    engine = WorkflowEngine(store=store)
    delivered = engine.pending_planning_inputs(thread_id)
    assert {item[0] for item in delivered} == {first.deferred_id, second.deferred_id}
    engine._acknowledge_delivered(
        first.deferred_id,
        thread_id=thread_id,
        cycle_id=1,
        purpose=InputPurpose.PLANNING_INPUT,
    )
    remaining = engine.pending_planning_inputs(thread_id)
    assert [item[0] for item in remaining] == [second.deferred_id]
    store.close()


def test_clarification_answer_and_replay_state_are_durable(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    record = ClarificationRequestRecord(
        clarification_id="clarification-1",
        thread_id=thread_id,
        cycle_id=1,
        root_event_key=events[0].event_key,
        occurrence_key="tool-call-1",
        requested_from_phase=WorkflowPhase.EXECUTING.value,
        question="Which region?",
        reason="Two valid regions were found.",
        answer_type="CHOICE",
        choices_json=json.dumps(["us-east-1", "eu-west-1"]),
        origin_surface="ISSUE",
        response_subject_number=7,
        response_comment_id="clarification-comment",
        response_url=None,
        review_thread_root_id=None,
        status=ClarificationStatus.OPEN.value,
        created_at="now",
        answered_at=None,
        answer_event_key=None,
        answer_json=None,
    )
    store.save_clarification(record)
    assert store.clarification_for_thread(thread_id).question == "Which region?"
    store.resolve_clarification_answer(
        "clarification-1",
        answer_event_key=events[1].event_key,
        answer_json=json.dumps(
            {"answer": "us-east-1", "residual": "update the README"}
        ),
        residual_text="update the README",
        now="later",
    )
    answered = store.clarification("clarification-1")
    assert answered.status == ClarificationStatus.ANSWERED.value
    assert answered.answer_event_key == events[1].event_key
    deferred = store.deferred_followup(events[1].event_key)
    assert deferred is not None
    assert deferred.deferred_id != events[1].event_key
    assert store.deferred_text_for_event(events[1].event_key) == "update the README"
    store.close()


def test_same_question_has_distinct_occurrence_ids(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    state = store.workflow_state(thread_id)
    assert state is not None
    engine = WorkflowEngine(store=store)
    first = engine._persist_clarification(
        state=state,
        proposal=ClarificationRequestProposal(
            question="Which region?",
            reason="Need one region.",
            occurrence_key="tool-call-1",
        ),
    )
    second = engine._persist_clarification(
        state=state,
        proposal=ClarificationRequestProposal(
            question="Which region?",
            reason="Need one region.",
            occurrence_key="tool-call-2",
        ),
    )
    assert first.clarification_id != second.clarification_id
    store.close()


def test_terminal_clarification_replay_cannot_reopen(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    base = ClarificationRequestRecord(
        clarification_id="clarification-terminal",
        thread_id=thread_id,
        cycle_id=1,
        root_event_key=events[0].event_key,
        occurrence_key="occ-terminal",
        requested_from_phase=WorkflowPhase.EXECUTING.value,
        question="Continue?",
        reason="needed",
        answer_type="BOOLEAN",
        choices_json="[]",
        origin_surface="ISSUE",
        response_subject_number=7,
        response_comment_id=None,
        response_url=None,
        review_thread_root_id=None,
        status=ClarificationStatus.OPEN.value,
        created_at="now",
        answered_at=None,
        answer_event_key=None,
        answer_json=None,
    )
    store.save_clarification(base)
    store.connection.execute(
        "UPDATE clarification_requests SET status=? WHERE clarification_id=?",
        (ClarificationStatus.ANSWERED.value, base.clarification_id),
    )
    store.connection.commit()
    store.save_clarification(base)
    assert (
        store.clarification(base.clarification_id).status
        == ClarificationStatus.ANSWERED.value
    )
    store.connection.execute(
        "UPDATE clarification_requests SET status=? WHERE clarification_id=?",
        (ClarificationStatus.CANCELLED.value, base.clarification_id),
    )
    store.connection.commit()
    store.save_clarification(base)
    assert (
        store.clarification(base.clarification_id).status
        == ClarificationStatus.CANCELLED.value
    )
    store.close()


def test_legacy_deferred_followup_table_is_rebuilt_idempotently(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    db_path = store.path
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    store.close()
    db = sqlite3.connect(db_path)
    db.execute("DROP TABLE deferred_followups")
    db.execute("""CREATE TABLE deferred_followups(
        source_event_key TEXT PRIMARY KEY REFERENCES source_events(event_key),
        thread_id TEXT NOT NULL REFERENCES issue_threads(thread_id),
        originating_cycle_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED',
        queued_at TEXT NOT NULL, consumed_cycle_id INTEGER, consumed_at TEXT)""")
    db.execute(
        "INSERT INTO deferred_followups("
        "source_event_key,thread_id,originating_cycle_id,queued_at) "
        "VALUES(?,?,?,?)",
        (events[1].event_key, thread_id, 1, "now"),
    )
    db.commit()
    db.close()
    reopened = SQLiteGitHubStore(db_path)
    first = reopened.deferred_followup(events[1].event_key)
    assert first is not None
    assert first.deferred_id.startswith("deferred-")
    columns = {
        row[1]
        for row in reopened.connection.execute("PRAGMA table_info(deferred_followups)")
    }
    assert "deferred_id" in columns
    reopened.close()
    reopened = SQLiteGitHubStore(db_path)
    assert (
        reopened.deferred_followup(events[1].event_key).deferred_id == first.deferred_id
    )
    reopened.close()


def test_deferred_ids_from_one_source_event_are_consumed_independently(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    first = store.defer_followup(
        source_event_key=events[1].event_key,
        thread_id=thread_id,
        originating_cycle_id=1,
        queued_at="one",
        residual_text="first",
    )
    second = store.defer_followup(
        source_event_key=events[1].event_key,
        thread_id=thread_id,
        originating_cycle_id=1,
        queued_at="two",
        residual_text="second",
    )
    assert first.deferred_id != second.deferred_id
    store.consume_deferred_followup(
        events[1].event_key,
        deferred_id=first.deferred_id,
        cycle_id=2,
        consumed_at="later",
    )
    assert (
        store.deferred_followup(
            events[1].event_key, deferred_id=first.deferred_id
        ).status
        == "CONSUMED"
    )
    assert (
        store.deferred_followup(
            events[1].event_key, deferred_id=second.deferred_id
        ).status
        == "QUEUED"
    )
    store.close()


def test_logical_executions_share_provenance_but_not_cycle_identity(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    ids = [
        execution_id_for(thread_id=thread_id, cycle_id=cycle, root_input_id=logical)
        for cycle, logical in ((2, "deferred-one"), (3, "deferred-two"))
    ]
    for execution_id, cycle, logical in zip(
        ids, (2, 3), ("deferred-one", "deferred-two")
    ):
        store.connection.execute(
            """INSERT INTO logical_executions(
               execution_id, source_event_key, thread_id, cycle_id, root_input_id,
               status, attempt_count, started_at) VALUES(?,?,?,?,?,?,?,?)""",
            (
                execution_id,
                events[1].event_key,
                thread_id,
                cycle,
                logical,
                "RUNNING",
                1,
                "same-time",
            ),
        )
    store.connection.commit()
    first = store.execution_for_cycle(
        thread_id=thread_id,
        cycle_id=2,
        root_event_key=events[1].event_key,
        root_input_id="deferred-one",
    )
    second = store.execution_for_cycle(
        thread_id=thread_id,
        cycle_id=3,
        root_event_key=events[1].event_key,
        root_input_id="deferred-two",
    )
    assert first["execution_id"] != second["execution_id"]
    assert (
        first["source_event_key"] == second["source_event_key"] == events[1].event_key
    )
    store.mark_execution_succeeded(
        events[1].event_key,
        execution_id=first["execution_id"],
        completed_at="done",
        response_text="one",
        workspace_path="/one",
    )
    assert store.execution_for_id(second["execution_id"])["status"] == "RUNNING"
    store.close()


def test_native_interrupt_prevents_post_request_action_until_resume():
    class State(TypedDict):
        actions: list[str]

    def node(state: State):
        actions = [*state["actions"], "before"]
        answer = interrupt({"question": "continue?"})
        actions.append(f"after:{answer}")
        return {"actions": actions}

    graph = StateGraph(State)
    graph.add_node("clarifying", node)
    graph.add_edge(START, "clarifying")
    graph.add_edge("clarifying", END)
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "native-clarification"}}

    paused = compiled.invoke({"actions": []}, config=config)
    assert "after:yes" not in paused["actions"]
    assert paused["__interrupt__"][0].value["question"] == "continue?"
    assert compiled.get_state(config).tasks[0].interrupts

    resumed = compiled.invoke(Command(resume="yes"), config=config)
    assert resumed["actions"] == ["before", "after:yes"]


def test_sweforge_agent_boundary_only_reports_real_pending_interrupt(monkeypatch):
    calls = []
    invocations = 0

    class FakeAgent:
        def invoke(self, state, **kwargs):
            nonlocal invocations
            invocations += 1
            calls.append(state)
            if invocations in (1, 2):

                class Pending:
                    value = {
                        "question": "Which region?",
                        "reason": "required",
                        "answer_type": "CHOICE",
                        "choices": ["east", "west"],
                        "occurrence_key": f"occ-{invocations}",
                    }

                return {"messages": [], "__interrupt__": [Pending()]}
            return {"messages": [type("Message", (), {"content": "done"})()]}

    monkeypatch.setattr("sweforge.agent.create_deep_agent", lambda **_: FakeAgent())
    interruptions = []
    assert (
        run_task(
            model="provider:model",
            worktree="/tmp",
            task="fix",
            thread_id="thread-1",
            checkpointer=object(),
            interrupt_result_sink=interruptions.append,
        )
        == ""
    )
    assert interruptions[0]["occurrence_key"] == "occ-1"
    assert (
        run_task(
            model="provider:model",
            worktree="/tmp",
            task="fix",
            thread_id="thread-1",
            checkpointer=object(),
            resume_value="east",
            interrupt_result_sink=interruptions.append,
        )
        == ""
    )
    assert len(interruptions) == 2
    assert interruptions[1]["occurrence_key"] == "occ-2"
    assert (
        run_task(
            model="provider:model",
            worktree="/tmp",
            task="fix",
            thread_id="thread-1",
            checkpointer=object(),
            resume_value="west",
            interrupt_result_sink=interruptions.append,
        )
        == "done"
    )
    assert len(interruptions) == 2
