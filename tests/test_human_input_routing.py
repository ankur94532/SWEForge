import json
from dataclasses import replace

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    ClarificationRequestRecord,
    ClarificationStatus,
    SQLiteGitHubStore,
    WorkflowPhase,
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
    store.close()


def test_clarification_answer_and_replay_state_are_durable(tmp_path):
    store, _repo, events = _store_with_cycle(tmp_path)
    thread_id = store.source_event(events[0].event_key)["thread_id"]
    record = ClarificationRequestRecord(
        clarification_id="clarification-1",
        thread_id=thread_id,
        cycle_id=1,
        root_event_key=events[0].event_key,
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
    store.resume_clarification(
        "clarification-1",
        answer_event_key=events[1].event_key,
        answer_json=json.dumps({"answer": "us-east-1"}),
        now="later",
    )
    answered = store.clarification("clarification-1")
    assert answered.status == ClarificationStatus.ANSWERED.value
    assert answered.answer_event_key == events[1].event_key
    store.close()
