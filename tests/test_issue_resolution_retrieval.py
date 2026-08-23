"""Repository-scoped retrieval of historical resolved cases.

Retrieval is local, deterministic and bounded: SQLite FTS5/BM25 over the
authoritative case rows, always filtered by authoritative repo_id.
"""

import json

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_store import (
    IssueResolutionRecord,
    IssueResolutionStatus,
    SQLiteGitHubStore,
    fts_match_expression,
    resolution_id_for,
)
from sweforge.issue_resolution import MAX_CASE_CONTEXT_CHARS, render_case_context


def event(repo, source_id, created="2026-01-01T00:00:00Z"):
    return SourceEvent(
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        source_kind=SourceKind.ISSUE_COMMENT,
        source_id=source_id,
        source_updated_at=created,
        source_created_at=created,
        subject_kind=SubjectKind.ISSUE,
        subject_number=int(source_id),
        author_login="octocat",
        body="@agent fix it",
        html_url=None,
    )


def case(
    store,
    *,
    repo,
    issue_number,
    cycle_id=1,
    title,
    symptom,
    root_cause,
    fix,
    components=(),
    status=IssueResolutionStatus.COMPLETED.value,
):
    source = event(repo, str(issue_number))
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [source],
        since="now",
        etag=None,
        polled_at="now",
    )
    thread_id = store.source_event(source.event_key)["thread_id"]
    root_input_id = f"{source.event_key}:{cycle_id}"
    record = IssueResolutionRecord(
        resolution_id=resolution_id_for(
            thread_id=thread_id, cycle_id=cycle_id, root_input_id=root_input_id
        ),
        repo_id=repo.repo_id,
        thread_id=thread_id,
        cycle_id=cycle_id,
        root_input_id=root_input_id,
        source_event_key=source.event_key,
        issue_number=issue_number,
        issue_title=title,
        issue_description_snapshot=f"Reported: {symptom}",
        task_summary=f"Resolve issue {issue_number}",
        symptom_summary=symptom,
        root_cause=root_cause,
        fix_summary=fix,
        affected_components_json=json.dumps(list(components)),
        changed_files_json=json.dumps(["src/app.py"]),
        validation_summary="unit tests",
        search_terms_json=json.dumps([]),
        limitations="",
        plan_id=None,
        execution_id=None,
        review_id=None,
        publication_id=None,
        commit_sha=None,
        pr_number=None,
        pr_url=None,
        status=status,
        error_message=None,
        attempt_count=1,
        created_at="now",
        updated_at="now",
    )
    return store.save_issue_resolution(record)


def seeded(tmp_path, name="state.db"):
    store = SQLiteGitHubStore(tmp_path / name)
    repo_a = RepositoryRef(1, "example/alpha")
    repo_b = RepositoryRef(2, "example/beta")
    store.upsert_repository(1, repo_a.full_name, "now")
    store.upsert_repository(2, repo_b.full_name, "now")
    case(
        store,
        repo=repo_a,
        issue_number=142,
        title="Search requests return 500 for new tenants",
        symptom="Newly added tenants receive HTTP 500 from search.",
        root_cause="Tenant configuration skipped the new region mapping.",
        fix="Added the mapping and made the config fallback explicit.",
        components=["config loader", "search routing"],
    )
    case(
        store,
        repo=repo_a,
        issue_number=88,
        title="Bulk discount miscalculated above threshold",
        symptom="Discounts above one hundred dollars were wrong.",
        root_cause="Subtotal cents were divided before DiscountPolicy ran.",
        fix="Removed the premature division.",
        components=["DiscountPolicy"],
    )
    case(
        store,
        repo=repo_b,
        issue_number=7,
        title="Tenant region mapping missing in beta",
        symptom="Beta tenants receive 500 from search too.",
        root_cause="Region mapping absent.",
        fix="Added mapping.",
        components=["config loader"],
    )
    return store, repo_a, repo_b


def test_case_is_found_by_issue_title_keywords(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    found = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="tenants search returning 500", limit=5
    )
    assert [item.issue_number for item in found][:1] == [142]
    store.close()


def test_case_is_found_by_root_cause_and_fix_keywords(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    by_cause = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="subtotal cents divided DiscountPolicy", limit=5
    )
    assert by_cause[0].issue_number == 88
    by_fix = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="premature division removed", limit=5
    )
    assert by_fix[0].issue_number == 88
    store.close()


def test_retrieval_never_crosses_repositories(tmp_path):
    store, repo_a, repo_b = seeded(tmp_path)
    from_a = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="region mapping tenants", limit=10
    )
    assert {item.repo_id for item in from_a} == {repo_a.repo_id}
    assert 7 not in {item.issue_number for item in from_a}
    from_b = store.search_issue_resolutions(
        repo_id=repo_b.repo_id, query="region mapping tenants", limit=10
    )
    assert {item.repo_id for item in from_b} == {repo_b.repo_id}
    assert {item.issue_number for item in from_b} == {7}
    store.close()


def test_top_k_is_bounded_and_deterministic(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    query = "tenant search discount mapping"
    first = store.search_issue_resolutions(repo_id=repo_a.repo_id, query=query, limit=1)
    assert len(first) == 1
    repeated = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query=query, limit=1
    )
    assert [item.resolution_id for item in first] == [
        item.resolution_id for item in repeated
    ]
    # An absurd limit is clamped rather than honoured.
    assert (
        len(
            store.search_issue_resolutions(
                repo_id=repo_a.repo_id, query=query, limit=9999
            )
        )
        <= 25
    )
    store.close()


def test_many_cycles_in_one_issue_do_not_flood_results(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/alpha")
    store.upsert_repository(1, repo.full_name, "now")
    for cycle_id in (1, 2, 3):
        case(
            store,
            repo=repo,
            issue_number=100,
            cycle_id=cycle_id,
            title="Pricing regression",
            symptom="Pricing regression in checkout.",
            root_cause="Rounding applied twice.",
            fix=f"Round once, pass {cycle_id}.",
        )
    unbounded = store.search_issue_resolutions(
        repo_id=1, query="pricing rounding checkout", limit=5
    )
    assert len(unbounded) == 3
    diversified = store.search_issue_resolutions(
        repo_id=1, query="pricing rounding checkout", limit=5, per_thread_limit=1
    )
    assert len(diversified) == 1
    store.close()


def test_no_matches_render_empty_context(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    assert (
        store.search_issue_resolutions(
            repo_id=repo_a.repo_id, query="kubernetes helm chart rollout", limit=3
        )
        == []
    )
    assert render_case_context([]) == ""
    store.close()


def test_non_completed_cases_are_never_retrievable(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/alpha")
    store.upsert_repository(1, repo.full_name, "now")
    case(
        store,
        repo=repo,
        issue_number=5,
        title="Broken widget rendering",
        symptom="Widget rendering broke.",
        root_cause="unknown",
        fix="none",
        status=IssueResolutionStatus.FAILED.value,
    )
    assert (
        store.search_issue_resolutions(repo_id=1, query="widget rendering", limit=5)
        == []
    )
    store.close()


def test_retrieval_survives_reopen_and_index_rebuild(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    store.close()
    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    assert reopened.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="tenant region mapping", limit=3
    )
    # The index is derived: it can be dropped and rebuilt from the base rows.
    reopened.connection.execute("DELETE FROM issue_resolution_fts")
    reopened.connection.commit()
    assert (
        reopened.search_issue_resolutions(
            repo_id=repo_a.repo_id, query="tenant region mapping", limit=3
        )
        == []
    )
    assert reopened.rebuild_issue_resolution_index() == 3
    assert reopened.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="tenant region mapping", limit=3
    )
    reopened.close()


def test_query_builder_is_injection_safe(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    # Raw FTS5 operators and unbalanced quotes must not raise.
    for hostile in ('tenant" OR haystack:*', "NEAR( a b", '"', "AND OR NOT", "()"):
        store.search_issue_resolutions(repo_id=repo_a.repo_id, query=hostile, limit=3)
    assert fts_match_expression("") == ""
    assert fts_match_expression("a of the") == ""
    store.close()


def test_rendered_context_is_bounded_and_marked_as_clues(tmp_path):
    store, repo_a, _ = seeded(tmp_path)
    records = store.search_issue_resolutions(
        repo_id=repo_a.repo_id, query="tenant search 500", limit=3
    )
    rendered = render_case_context(records)
    assert "clues, not" in rendered
    assert "verify against the" in rendered
    assert "#142" in rendered
    assert len(rendered) <= len(records) * MAX_CASE_CONTEXT_CHARS + 400
    store.close()


def test_planning_receives_bounded_cases_but_plan_stays_authoritative(tmp_path):
    """Cases reach the planner as clues; they are not authorization."""
    from test_publication_identity import THREAD_ID, Harness
    from test_publication_identity import source_event as evt

    harness = Harness(tmp_path)
    harness.store.upsert_issue_metadata(
        repo_id=harness.repo.repo_id,
        issue_number=7,
        title="Search requests return 500 for new tenants",
        body="Newly added tenants receive HTTP 500.",
        observed_at="2026-01-01T00:00:00Z",
    )
    origin = evt(harness.repo, "1", "@agent fix tenant search", "2026-01-01T00:00:00Z")
    harness.record(origin)
    case(
        harness.store,
        repo=harness.repo,
        issue_number=142,
        title="Search requests return 500 for new tenants",
        symptom="Newly added tenants receive HTTP 500 from search.",
        root_cause="Tenant configuration skipped the new region mapping.",
        fix="Added the mapping and made the config fallback explicit.",
        components=["config loader"],
    )
    seen: dict[str, str] = {}

    def planner(**kwargs):
        seen["cases"] = kwargs.get("historical_cases", "")
        return "Requirements:\n1. apply the requested change"

    harness.engine.planner = planner
    plan = harness.engine.plan_event(
        event_key=origin.event_key,
        model="planning-sonnet",
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
    )
    assert "#142" in seen["cases"]
    assert "clues, not" in seen["cases"]
    assert "region mapping" in seen["cases"]
    # The approved plan text remains what the application authorizes.
    assert plan.plan_text == "Requirements:\n1. apply the requested change"
    assert "#142" not in plan.plan_text
    assert harness.store.workflow_state(THREAD_ID).current_plan_id == plan.plan_id
    harness.store.close()


def test_planning_context_is_empty_when_no_cases_exist(tmp_path):
    from test_publication_identity import Harness
    from test_publication_identity import source_event as evt

    harness = Harness(tmp_path)
    origin = evt(harness.repo, "1", "@agent fix something", "2026-01-01T00:00:00Z")
    harness.record(origin)
    seen: dict[str, str] = {}

    def planner(**kwargs):
        seen["cases"] = kwargs.get("historical_cases", "")
        return "Requirements:\n1. do it"

    harness.engine.planner = planner
    harness.engine.plan_event(
        event_key=origin.event_key,
        model="planning-sonnet",
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
    )
    assert seen["cases"] == ""
    harness.store.close()
