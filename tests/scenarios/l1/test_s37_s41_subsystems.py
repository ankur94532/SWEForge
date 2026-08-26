"""S37-S41: subsystem correctness.

Each covers a place where a subsystem could quietly hand back the wrong thing
rather than fail: an interrupt receiving another interrupt's answer, a poller
losing or duplicating an event, a re-ingested root forking a second cycle, a
memory proposal escaping the repository, and one repository retrieving
another's history.
"""

import hashlib
import json

import pytest
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_models import SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    ClarificationRequestRecord,
    ClarificationStatus,
    IssueResolutionRecord,
    resolution_id_for,
)
from sweforge.memory_learning import (
    RepoMemoryCandidate,
    RepoMemoryEvidence,
    validate_candidate,
)


def _clarification(thread_id, cycle_id, root, occurrence, answer=None, answer_key=None):
    return ClarificationRequestRecord(
        clarification_id=f"c-{occurrence}",
        thread_id=thread_id,
        cycle_id=cycle_id,
        root_event_key=root,
        occurrence_key=occurrence,
        requested_from_phase="EXECUTING",
        question=f"question for {occurrence}",
        reason="ambiguous",
        answer_type="TEXT",
        choices_json="[]",
        origin_surface="ISSUE",
        response_subject_number=7,
        response_comment_id=None,
        response_url=None,
        review_thread_root_id=None,
        status=(
            ClarificationStatus.ANSWERED.value
            if answer
            else ClarificationStatus.OPEN.value
        ),
        created_at="now",
        answered_at="now" if answer else None,
        answer_event_key=answer_key if answer else None,
        answer_json=json.dumps({"answer": answer}) if answer else None,
    )


@scenario(
    "S37",
    layer=Layer.L1,
    invariants=["INV-ONE-ROOT", "INV-THREAD-ISOLATION"],
    description="Each interrupt receives its own answer, never another's.",
)
def s37_two_clarifications_one_cycle(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        world.ingest(
            world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"),
            world.event("2", "@agent first", "2026-01-01T00:00:05Z"),
            world.event("3", "@agent second", "2026-01-01T00:00:09Z"),
        )
        thread_id = next(iter(world.thread_ids))
        store = world.store
        # root_event_key and answer_event_key are both foreign keys onto
        # source_events, so read the stored keys rather than assuming them.
        stored = {
            row["source_id"]: row["event_key"]
            for row in store.connection.execute(
                "SELECT source_id, event_key FROM source_events"
            )
        }
        root = stored["1"]
        store.save_clarification(
            _clarification(thread_id, 1, root, "alpha", "first", stored["2"])
        )
        store.save_clarification(
            _clarification(thread_id, 1, root, "beta", "second", stored["3"])
        )

        alpha = store.answered_clarification_for_occurrence(
            thread_id=thread_id, cycle_id=1, occurrence_key="alpha"
        )
        beta = store.answered_clarification_for_occurrence(
            thread_id=thread_id, cycle_id=1, occurrence_key="beta"
        )
        assert alpha is not None and beta is not None, "an answered interrupt was lost"
        assert json.loads(alpha.answer_json)["answer"] == "first"
        assert json.loads(beta.answer_json)["answer"] == "second", (
            "one interrupt received another interrupt's answer"
        )
        missing = store.answered_clarification_for_occurrence(
            thread_id=thread_id, cycle_id=1, occurrence_key="gamma"
        )
        assert missing is None, "an unknown occurrence was handed some answer"
    return world.observation()


@scenario(
    "S38",
    layer=Layer.L1,
    invariants=["INV-ONE-ROOT", "INV-THREAD-ISOLATION"],
    description="Repeated polls ingest every event once and never twice.",
)
def s38_poller_ingests_each_event_once(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        first = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
        second = world.event("2", "@agent and this", "2026-01-01T00:00:05Z")
        world.ingest(first)
        world.ingest(second)
        # The same batch arrives again, as an overlapping poll window does.
        world.ingest(first, second)

        rows = world.store.connection.execute(
            "SELECT source_id, COUNT(*) c FROM source_events GROUP BY source_id"
        ).fetchall()
        duplicated = [row["source_id"] for row in rows if row["c"] > 1]
        assert not duplicated, f"an overlapping poll duplicated {duplicated}"
        assert len(rows) == 2, f"expected two distinct events, got {len(rows)}"
    return world.observation()


@scenario(
    "S39",
    layer=Layer.L1,
    invariants=["INV-ONE-ROOT", "INV-PLAN-CANONICAL"],
    description="Re-ingesting an unchanged root starts no second cycle.",
)
def s39_reingested_root_is_deduplicated(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        root = world.event("1", "@agent fix it", "2026-01-01T00:00:00Z")
        world.ingest(root)
        thread_id = next(iter(world.thread_ids))
        before = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_threads"
        ).fetchone()[0]

        world.ingest(root)
        after = world.store.connection.execute(
            "SELECT COUNT(*) FROM issue_threads"
        ).fetchone()[0]
        assert after == before, "re-ingesting the root created a second thread"
        events = world.store.connection.execute(
            "SELECT COUNT(*) FROM source_events WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        assert events == 1, f"the root was stored {events} times"
    return world.observation()


@scenario(
    "S40",
    layer=Layer.L1,
    invariants=["INV-NO-MEMORY-WRITTEN", "INV-NO-PUBLICATION"],
    description="A memory proposal corpus is refused payload by payload.",
)
def s40_memory_proposal_validator_corpus(root_dir) -> Observation:
    world = World.build(root_dir)
    source = world.root / "source"
    target = source / "README.md"
    lines = target.read_text().splitlines()
    excerpt = "\n".join(lines[0:1])
    digest = hashlib.sha256(excerpt.encode()).hexdigest()

    def candidate(path="README.md", start=1, end=1, fact="The base file exists."):
        return RepoMemoryCandidate(
            candidate_id="cand-1",
            category="architecture",
            fact=fact,
            durability_reason="stable structural fact",
            evidence=[
                RepoMemoryEvidence(
                    path=path,
                    start_line=start,
                    end_line=end,
                    content_hash=digest,
                    excerpt=excerpt,
                )
            ],
        )

    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))

        # A valid candidate is accepted, so the corpus cannot pass vacuously.
        validate_candidate(candidate(), repo_id=1, worktree=source)

        corpus = {
            "path escape": (candidate(path="../outside.md"), "escapes"),
            "absolute path": (candidate(path="/etc/passwd"), "escapes"),
            "inverted range": (candidate(start=5, end=2), "inverted"),
            "range past end": (candidate(start=1, end=9999), "outside the file"),
            "task specific": (candidate(fact="I fixed the bug."), "task-specific"),
            # SECRET_RE matches a labelled credential, not any opaque token.
            "secret": (
                candidate(fact="The deploy uses api_key=ghp_ExampleValue123."),
                "secret",
            ),
        }
        for name, (payload, expected) in corpus.items():
            with pytest.raises(ValueError, match=expected):
                validate_candidate(payload, repo_id=1, worktree=source)
            del name
    return world.observation()


@scenario(
    "S41",
    layer=Layer.L1,
    invariants=["INV-REPO-ISOLATION"],
    description="One repository never retrieves another repository's cases.",
)
def s41_resolution_retrieval_is_repository_scoped(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        store = world.store
        # Both repositories need a real thread and event: the resolution table
        # has foreign keys onto both.
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        store.upsert_repository(2, "example/other", "now")
        other = SourceEvent(
            repo_id=2,
            repo_full_name="example/other",
            source_kind=SourceKind.ISSUE_COMMENT,
            source_id="other-1",
            source_updated_at="2026-01-01T00:00:00Z",
            source_created_at="2026-01-01T00:00:00Z",
            subject_kind=SubjectKind.ISSUE,
            subject_number=7,
            author_login="octocat",
            body="@agent fix it",
            html_url=None,
        )
        store.record_batch(
            2, "issue_comments", [other], since="now", etag=None, polled_at="now"
        )

        rows = {
            row["repo_id"]: (row["thread_id"], row["event_key"])
            for row in store.connection.execute(
                "SELECT e.repo_id, e.thread_id, e.event_key FROM source_events e"
            )
        }
        world.extra_repo_ids = frozenset({2})

        for repo_id, symptom in (
            (1, "pricing boundary rounding defect"),
            (2, "pricing boundary rounding defect"),
        ):
            thread, event_key = rows[repo_id]
            store.save_issue_resolution(
                IssueResolutionRecord(
                    resolution_id=resolution_id_for(
                        thread_id=thread, cycle_id=1, root_input_id=event_key
                    ),
                    repo_id=repo_id,
                    thread_id=thread,
                    cycle_id=1,
                    root_input_id=event_key,
                    source_event_key=event_key,
                    issue_number=7,
                    issue_title="pricing",
                    issue_description_snapshot="pricing",
                    task_summary="fix the boundary",
                    symptom_summary=symptom,
                    root_cause="off by one",
                    fix_summary="widened the branch",
                    affected_components_json=json.dumps([]),
                    changed_files_json=json.dumps([]),
                    validation_summary="tests pass",
                    search_terms_json=json.dumps(["pricing", "boundary"]),
                    limitations="",
                    plan_id=None,
                    execution_id=None,
                    review_id=None,
                    publication_id=None,
                    commit_sha=None,
                    pr_number=None,
                    pr_url=None,
                    status="COMPLETED",
                    error_message=None,
                    attempt_count=1,
                    created_at="now",
                    updated_at="now",
                )
            )

        mine = store.search_issue_resolutions(repo_id=1, query="pricing boundary")
        threads = {item.thread_id for item in mine}
        assert rows[1][0] in threads, "a repository could not retrieve its own case"
        assert rows[2][0] not in threads, (
            "a repository retrieved another repository's case"
        )
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S37", "S38", "S39", "S40", "S41"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
