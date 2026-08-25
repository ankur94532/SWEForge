"""Every invariant needs a passing case and a failing case.

This is the guard audit's own remediation rule applied here: an invariant with
no failing test may be unable to fire, and an invariant that cannot fire is not
protection -- it is a dead branch that reads as a pass.
"""

import pytest
from harness.github_fake import FakeGitHub
from harness.invariants import REGISTRY, InvariantResult, check, register
from harness.observation import LedgerGitHubFacts, Observation


def ev(kind, seq=1, thread="t1", cycle=1, **data):
    return {
        "kind": kind,
        "seq": seq,
        "thread_id": thread,
        "cycle_id": cycle,
        "data": data,
    }


def obs(*events, github=None):
    return Observation(events=list(events), github=github)


def test_every_registered_invariant_has_the_expected_shape():
    for key, item in REGISTRY.items():
        assert item.id == key
        assert item.description
        assert item.scope in {"thread", "cycle", "repo", "campaign"}


def test_unknown_invariant_is_an_error_not_a_pass():
    with pytest.raises(KeyError, match="unknown invariant"):
        check("INV-DOES-NOT-EXIST", obs(ev("ROOT_INGESTED")))


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError, match="duplicate invariant id"):
        register("INV-ONE-ROOT", "dupe")(lambda o: InvariantResult(True))


def test_an_empty_event_log_raises_rather_than_passing():
    """Absence of evidence must never be reported as evidence of absence."""
    with pytest.raises(RuntimeError, match="cannot observe"):
        check("INV-ONE-ROOT", obs())


# -- root / plan -----------------------------------------------------------


def test_one_root_passes_with_a_single_root():
    assert check("INV-ONE-ROOT", obs(ev("ROOT_INGESTED"))).ok


def test_one_root_fails_with_two_roots_on_one_thread():
    r = check("INV-ONE-ROOT", obs(ev("ROOT_INGESTED", 1), ev("ROOT_INGESTED", 2)))
    assert not r.ok and "multiple roots" in r.detail


def test_plan_canonical_passes_for_one_plan_per_cycle():
    assert check("INV-PLAN-CANONICAL", obs(ev("PLAN_CREATED", plan_id="p1"))).ok


def test_plan_canonical_fails_on_two_plans_in_one_cycle():
    r = check(
        "INV-PLAN-CANONICAL",
        obs(ev("PLAN_CREATED", 1, plan_id="p1"), ev("PLAN_CREATED", 2, plan_id="p2")),
    )
    assert not r.ok and "multiple plans" in r.detail


# -- permits ---------------------------------------------------------------


PLAN = ev("PLAN_CREATED", 1, plan_id="p1", plan_version=1)


def test_permit_bound_passes_when_it_matches_the_plan():
    permit = ev("PERMIT_CREATED", 2, permit_id="x", plan_id="p1", plan_version=1)
    assert check("INV-PERMIT-BOUND", obs(PLAN, permit)).ok


def test_permit_bound_fails_on_a_different_plan_id():
    permit = ev("PERMIT_CREATED", 2, permit_id="x", plan_id="OTHER", plan_version=1)
    r = check("INV-PERMIT-BOUND", obs(PLAN, permit))
    assert not r.ok and "binds OTHER" in r.detail


def test_permit_bound_fails_on_a_stale_plan_version():
    permit = ev("PERMIT_CREATED", 2, permit_id="x", plan_id="p1", plan_version=1)
    newer = ev("PLAN_CREATED", 1, plan_id="p1", plan_version=2)
    r = check("INV-PERMIT-BOUND", obs(newer, permit))
    assert not r.ok and "version" in r.detail


def test_permit_bound_fails_when_no_permit_exists():
    assert not check("INV-PERMIT-BOUND", obs(PLAN)).ok


def test_permit_none_passes_with_no_permit():
    assert check("INV-PERMIT-NONE", obs(PLAN)).ok


def test_permit_none_fails_when_one_was_minted():
    r = check("INV-PERMIT-NONE", obs(PLAN, ev("PERMIT_CREATED", 2, permit_id="x")))
    assert not r.ok and "created" in r.detail


def test_permit_source_passes_for_a_single_source():
    assert check(
        "INV-PERMIT-SOURCE", obs(ev("PERMIT_CREATED", permit_source="USER"))
    ).ok


def test_permit_source_fails_on_mixed_sources():
    r = check(
        "INV-PERMIT-SOURCE",
        obs(
            ev("PERMIT_CREATED", 1, permit_source="USER"),
            ev("PERMIT_CREATED", 2, permit_source="AUTO"),
        ),
    )
    assert not r.ok and "mixed" in r.detail


# -- execution -------------------------------------------------------------


def test_one_initial_passes_with_a_single_attempt():
    assert check(
        "INV-ONE-INITIAL",
        obs(ev("EXECUTION_STARTED", attempt_kind="INITIAL", attempt_id="a1")),
    ).ok


def test_one_initial_fails_with_two_initial_attempts_in_a_cycle():
    r = check(
        "INV-ONE-INITIAL",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_kind="INITIAL", attempt_id="a1"),
            ev("EXECUTION_STARTED", 2, attempt_kind="INITIAL", attempt_id="a2"),
        ),
    )
    assert not r.ok and "multiple INITIAL" in r.detail


def test_one_initial_ignores_repair_attempts():
    assert check(
        "INV-ONE-INITIAL",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_kind="INITIAL", attempt_id="a1"),
            ev("EXECUTION_STARTED", 2, attempt_kind="REVIEW_REPAIR", attempt_id="a2"),
        ),
    ).ok


def test_no_hot_retry_passes_when_nothing_failed():
    assert check(
        "INV-NO-HOT-RETRY", obs(ev("EXECUTION_STARTED", attempt_kind="INITIAL"))
    ).ok


def test_no_hot_retry_fails_when_an_attempt_follows_a_terminal_failure():
    r = check(
        "INV-NO-HOT-RETRY",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_kind="INITIAL", attempt_id="a1"),
            ev("EXECUTION_FAILED", 2, attempt_id="a1"),
            ev("EXECUTION_STARTED", 3, attempt_kind="INITIAL", attempt_id="a2"),
        ),
    )
    assert not r.ok and "after failure" in r.detail


def test_retry_bounded_passes_within_the_bound():
    assert check("INV-RETRY-BOUNDED", obs(ev("EXECUTION_STARTED", retry_count=3))).ok


def test_retry_bounded_fails_beyond_the_bound():
    r = check("INV-RETRY-BOUNDED", obs(ev("EXECUTION_STARTED", retry_count=4)))
    assert not r.ok and "over retry bound" in r.detail


# -- publication -----------------------------------------------------------


def _published() -> LedgerGitHubFacts:
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    fake.create_pull_request(
        repo, head="sweforge/issue-7", base="main", title="t", body="b"
    )
    fake.create_comment(repo, 7, "done")
    return LedgerGitHubFacts(fake)


def test_one_publication_passes_for_a_single_pr_created_once():
    assert check("INV-ONE-PUBLICATION", obs(github=_published())).ok


def test_one_publication_fails_when_a_second_pr_is_created():
    fake = FakeGitHub()
    repo = fake.repository("example/repo")
    for head in ("sweforge/issue-7", "sweforge/issue-7-again"):
        fake.create_pull_request(repo, head=head, base="main", title="t", body="b")
    r = check("INV-ONE-PUBLICATION", obs(github=LedgerGitHubFacts(fake)))
    assert not r.ok and "exactly 1" in r.detail


def test_one_publication_needs_github_facts_rather_than_passing_blind():
    with pytest.raises(RuntimeError, match="needs GitHubFacts"):
        check("INV-ONE-PUBLICATION", obs(ev("PLAN_CREATED")))


def test_no_publication_passes_when_nothing_was_created():
    assert check("INV-NO-PUBLICATION", obs(github=LedgerGitHubFacts(FakeGitHub()))).ok


def test_no_publication_fails_when_a_pr_exists():
    r = check("INV-NO-PUBLICATION", obs(github=_published()))
    assert not r.ok and "pull request" in r.detail


def test_no_publication_ignores_a_posted_plan_comment():
    """A plan comment is not a publication."""
    fake = FakeGitHub()
    fake.create_comment(
        fake.repository("example/repo"), 7, "<!-- sweforge:plan:p1 --> plan"
    )
    assert check("INV-NO-PUBLICATION", obs(github=LedgerGitHubFacts(fake))).ok


def test_no_publication_fails_on_a_publication_comment():
    fake = FakeGitHub()
    fake.create_comment(
        fake.repository("example/repo"), 7, "<!-- sweforge:publication:p1 --> done"
    )
    r = check("INV-NO-PUBLICATION", obs(github=LedgerGitHubFacts(fake)))
    assert not r.ok and "publication comment" in r.detail


# -- tranche 2: plan versioning, attempts, review, provenance, isolation ----


def test_plan_versioned_passes_on_monotonic_versions():
    assert check(
        "INV-PLAN-VERSIONED",
        obs(
            ev("PLAN_CREATED", 1, plan_version=1),
            ev("PLAN_REVISED", 2, plan_version=2),
        ),
    ).ok


def test_plan_versioned_fails_when_a_version_is_reused():
    r = check(
        "INV-PLAN-VERSIONED",
        obs(
            ev("PLAN_CREATED", 1, plan_version=1), ev("PLAN_REVISED", 2, plan_version=1)
        ),
    )
    assert not r.ok and "repeated plan version" in r.detail


def test_plan_versioned_fails_when_versions_go_backwards():
    r = check(
        "INV-PLAN-VERSIONED",
        obs(
            ev("PLAN_CREATED", 1, plan_version=2), ev("PLAN_REVISED", 2, plan_version=1)
        ),
    )
    assert not r.ok and "not monotonic" in r.detail


def test_attempt_terminal_passes_when_every_attempt_finished():
    assert check(
        "INV-ATTEMPT-TERMINAL",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_id="a1"),
            ev("EXECUTION_SUCCEEDED", 2, attempt_id="a1"),
        ),
    ).ok


def test_attempt_terminal_fails_on_a_dangling_attempt():
    r = check("INV-ATTEMPT-TERMINAL", obs(ev("EXECUTION_STARTED", 1, attempt_id="a1")))
    assert not r.ok and "non-terminal" in r.detail


def test_review_ledger_fresh_passes_for_distinct_attempts():
    assert check(
        "INV-REVIEW-LEDGER-FRESH",
        obs(
            ev("REVIEW_ATTEMPT", 1, attempt_id="a1"),
            ev("REVIEW_ATTEMPT", 2, attempt_id="a1"),
        ),
    ).ok


def test_review_ledger_fresh_fails_when_no_review_was_attempted():
    r = check("INV-REVIEW-LEDGER-FRESH", obs(ev("PLAN_CREATED", 1, plan_id="p")))
    assert not r.ok and "no review attempt" in r.detail


def test_no_repair_on_infra_passes_without_an_infra_failure():
    assert check("INV-REVIEW-NO-REPAIR-ON-INFRA", obs(ev("REVIEW_ATTEMPT", 1))).ok


def test_no_repair_on_infra_fails_when_a_repair_immediately_follows():
    r = check(
        "INV-REVIEW-NO-REPAIR-ON-INFRA",
        obs(
            ev("REVIEW_INFRA_FAILED", 1, attempt_id="a1"),
            ev("REPAIR_AUTHORIZED", 2, permit_id="p"),
        ),
    )
    assert not r.ok and "immediately after infra failure" in r.detail


def test_provenance_passes_for_one_root_per_cycle():
    assert check(
        "INV-PROVENANCE",
        obs(
            ev("PLAN_CREATED", 1, root_event_key="root-1"),
            ev("PERMIT_CREATED", 2, root_event_key="root-1"),
        ),
    ).ok


def test_provenance_fails_when_a_cycle_cites_two_roots():
    r = check(
        "INV-PROVENANCE",
        obs(
            ev("PLAN_CREATED", 1, root_event_key="root-1"),
            ev("PERMIT_CREATED", 2, root_event_key="root-2"),
        ),
    )
    assert not r.ok and "multiple roots" in r.detail


def test_deferred_preserved_passes_with_distinct_ids():
    assert check(
        "INV-DEFERRED-PRESERVED",
        obs(
            ev("INPUT_DEFERRED", 1, deferred_id="d1"),
            ev("INPUT_DEFERRED", 2, deferred_id="d2"),
        ),
    ).ok


def test_deferred_preserved_fails_when_an_id_is_reused():
    r = check(
        "INV-DEFERRED-PRESERVED",
        obs(
            ev("INPUT_DEFERRED", 1, deferred_id="d1"),
            ev("INPUT_DEFERRED", 2, deferred_id="d1"),
        ),
    )
    assert not r.ok and "reused" in r.detail


def test_no_injection_passes_when_input_arrives_outside_active_windows():
    assert check(
        "INV-NO-INJECTION",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_id="a1"),
            ev("EXECUTION_SUCCEEDED", 2, attempt_id="a1"),
            ev("INPUT_DELIVERED", 3, event_key="e1"),
        ),
    ).ok


def test_no_injection_fails_when_input_lands_mid_execution():
    r = check(
        "INV-NO-INJECTION",
        obs(
            ev("EXECUTION_STARTED", 1, attempt_id="a1"),
            ev("INPUT_DELIVERED", 2, event_key="e1"),
            ev("EXECUTION_SUCCEEDED", 3, attempt_id="a1"),
        ),
    )
    assert not r.ok and "inside an active window" in r.detail


def test_thread_isolation_passes_for_declared_threads():
    o = Observation(events=[ev("PLAN_CREATED", 1)], thread_ids=frozenset({"t1"}))
    assert check("INV-THREAD-ISOLATION", o).ok


def test_thread_isolation_fails_on_a_foreign_thread():
    o = Observation(
        events=[ev("PLAN_CREATED", 1, thread="t9")], thread_ids=frozenset({"t1"})
    )
    r = check("INV-THREAD-ISOLATION", o)
    assert not r.ok and "foreign threads" in r.detail


def test_thread_isolation_refuses_to_pass_vacuously():
    with pytest.raises(RuntimeError, match="vacuously pass"):
        check("INV-THREAD-ISOLATION", obs(ev("PLAN_CREATED", 1)))


def test_repo_isolation_fails_on_a_foreign_repository():
    events = [dict(ev("PLAN_CREATED", 1), repo_id=99)]
    o = Observation(events=events, repo_ids=frozenset({1}))
    r = check("INV-REPO-ISOLATION", o)
    assert not r.ok and "foreign repositories" in r.detail


def test_repo_isolation_passes_for_declared_repositories():
    events = [dict(ev("PLAN_CREATED", 1), repo_id=1)]
    o = Observation(events=events, repo_ids=frozenset({1}))
    assert check("INV-REPO-ISOLATION", o).ok


def test_repo_isolation_refuses_to_pass_vacuously():
    with pytest.raises(RuntimeError, match="vacuously pass"):
        check("INV-REPO-ISOLATION", obs(ev("PLAN_CREATED", 1)))


def test_no_empty_commit_passes_when_nothing_was_committed():
    assert check("INV-NO-EMPTY-COMMIT", obs(ev("PLAN_CREATED", 1))).ok


def test_no_empty_commit_fails_when_a_commit_exists():
    r = check("INV-NO-EMPTY-COMMIT", obs(ev("COMMIT_CREATED", 1, commit_sha="abc")))
    assert not r.ok and "commit(s) created" in r.detail


# -- tranche 3: store-backed, filesystem, locking --------------------------


@pytest.fixture
def seeded(tmp_path):
    """A real thread, plan, permit and attempt, built through the public API.

    Hand-written INSERTs trip four foreign keys; going through the store also
    keeps these tests honest if the schema moves.
    """
    from sweforge.github_models import (
        RepositoryRef,
        SourceEvent,
        SourceKind,
        SubjectKind,
    )
    from sweforge.github_store import AttemptStatus, SQLiteGitHubStore
    from sweforge.workflow import WorkflowEngine

    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/repo")

    def event(source_id, body, when):
        return SourceEvent(
            repo_id=repo.repo_id,
            repo_full_name=repo.full_name,
            source_kind=SourceKind.ISSUE_COMMENT,
            source_id=source_id,
            source_updated_at=when,
            source_created_at=when,
            subject_kind=SubjectKind.ISSUE,
            subject_number=7,
            author_login="octocat",
            body=body,
            html_url=None,
        )

    root = event("1", "@agent fix it", "2026-01-01T00:00:00Z")
    approval = event("2", "@agent approve", "2026-01-01T00:01:00Z")
    store.upsert_repository(repo.repo_id, repo.full_name, "now")
    store.record_batch(
        repo.repo_id,
        "issue_comments",
        [root, approval],
        since="now",
        etag=None,
        polled_at="now",
    )
    engine = WorkflowEngine(store=store, clock=lambda: "2026-01-01T00:00:30Z")
    plan = engine.start_cycle(
        event_key=root.event_key, plan_text="edit README", posted_comment_id=1
    )
    permit = engine.approve(event_key=approval.event_key)
    attempt = store.ensure_execution_attempt(
        attempt_id="a1",
        thread_id=permit.thread_id,
        cycle_id=permit.cycle_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        root_event_key=plan.root_event_key,
        authorization_id=permit.permit_id,
        created_at="now",
    )
    store.finish_execution_attempt(
        attempt.attempt_id,
        status=AttemptStatus.SUCCEEDED,
        completed_at="now",
        response_text="done",
        start_head_sha="a",
        end_head_sha="b",
        end_dirty=False,
    )
    return {
        "store": store,
        "thread_id": permit.thread_id,
        "cycle_id": permit.cycle_id,
        "plan_id": plan.plan_id,
        "root_event_key": plan.root_event_key,
    }


def sobs(seeded, **kw):
    return Observation(store=seeded["store"], **kw)


@pytest.mark.parametrize(
    "invariant",
    [
        "INV-EVIDENCE-CONTIGUOUS",
        "INV-EVIDENCE-TRUSTED",
        "INV-REVIEW-GROUNDED",
        "INV-PUB-MAPPING",
        "INV-PUB-AUTHORIZED",
        "INV-NO-FALSE-RESOLUTION",
        "INV-NO-FALSE-MEMORY",
        "INV-LEARNING-ISOLATED",
    ],
)
def test_store_backed_invariants_refuse_to_pass_without_a_store(invariant):
    with pytest.raises(RuntimeError, match="needs a store"):
        check(invariant, Observation(events=[ev("PLAN_CREATED")]))


def _evidence(seeded, *sequences):
    for number in sequences:
        seeded["store"].connection.execute(
            "INSERT INTO execution_tool_evidence "
            "(evidence_id,attempt_id,thread_id,cycle_id,sequence_number,kind,"
            " command,exit_code,output,output_hash,truncated,recorded_at) "
            "VALUES (?,'a1',?,?,?,'execute','cmd',0,'out','h',0,'now')",
            (
                f"exec-evidence-{number}",
                seeded["thread_id"],
                seeded["cycle_id"],
                number,
            ),
        )
    seeded["store"].connection.commit()


def _review(seeded, verdict="ACCEPT", checks="[]", ledger="[]"):
    seeded["store"].connection.execute(
        "INSERT INTO execution_reviews "
        "(review_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,"
        " attempt_id,review_iteration,verdict,summary,findings_json,"
        " repair_instructions_json,created_at,completed_at,"
        " requirement_checks_json,inspection_json,challenge_json,read_ledger_json) "
        "VALUES ('r1',?,?,?,1,?,'a1',1,?,'s','[]','[]','now','now',?,'{}','{}',?)",
        (
            seeded["thread_id"],
            seeded["cycle_id"],
            seeded["plan_id"],
            seeded["root_event_key"],
            verdict,
            checks,
            ledger,
        ),
    )
    seeded["store"].connection.commit()


def _publication(seeded, status="COMPLETED"):
    seeded["store"].connection.execute(
        "INSERT INTO logical_publications "
        "(publication_id,source_event_key,thread_id,cycle_id,root_input_id,"
        " repo_id,repo_full_name,issue_number,status,branch_name,"
        " created_at,updated_at) "
        "VALUES ('pub-1',?,?,?,?,1,'example/repo',7,?,'sweforge/issue-7','now','now')",
        (
            seeded["root_event_key"],
            seeded["thread_id"],
            seeded["cycle_id"],
            seeded["root_event_key"],
            status,
        ),
    )
    seeded["store"].connection.commit()


def test_evidence_contiguous_passes_on_an_unbroken_sequence(seeded):
    _evidence(seeded, 1, 2, 3)
    assert check("INV-EVIDENCE-CONTIGUOUS", sobs(seeded)).ok


def test_evidence_contiguous_fails_on_a_gap(seeded):
    _evidence(seeded, 1, 2, 4)
    r = check("INV-EVIDENCE-CONTIGUOUS", sobs(seeded))
    assert not r.ok and "gap" in r.detail


def test_evidence_trusted_passes_when_every_cited_id_exists(seeded):
    _evidence(seeded, 1)
    _review(seeded, ledger='[{"source_id": "exec-evidence-1"}]')
    assert check("INV-EVIDENCE-TRUSTED", sobs(seeded)).ok


def test_evidence_trusted_fails_on_a_dangling_citation(seeded):
    _review(seeded, ledger='[{"source_id": "exec-evidence-missing"}]')
    r = check("INV-EVIDENCE-TRUSTED", sobs(seeded))
    assert not r.ok and "unknown execution evidence" in r.detail


def test_review_grounded_passes_when_requirements_cite_evidence(seeded):
    _review(
        seeded, checks='[{"requirement_id": "r1", "evidence_refs": [{"kind": "X"}]}]'
    )
    assert check("INV-REVIEW-GROUNDED", sobs(seeded)).ok


def test_review_grounded_fails_on_an_ungrounded_requirement(seeded):
    _review(seeded, checks='[{"requirement_id": "r1", "evidence_refs": []}]')
    r = check("INV-REVIEW-GROUNDED", sobs(seeded))
    assert not r.ok and "ungrounded requirement" in r.detail


def test_review_grounded_fails_when_an_accept_has_no_checks_at_all(seeded):
    _review(seeded, checks="[]")
    r = check("INV-REVIEW-GROUNDED", sobs(seeded))
    assert not r.ok and "no requirement checks" in r.detail


def test_pub_mapping_passes_for_one_to_one(seeded):
    seeded["store"].connection.execute(
        "INSERT INTO pr_thread_mappings VALUES (1,7,?)", (seeded["thread_id"],)
    )
    seeded["store"].connection.commit()
    assert check("INV-PUB-MAPPING", sobs(seeded)).ok


def test_pub_mapping_fails_when_one_thread_holds_two_pull_requests(seeded):
    """The reachable violation.

    A PR shared by two threads is prevented by the UNIQUE constraint on
    (repo_id, pr_number), so that branch of the invariant guards against a
    state only reachable across databases or after a migration. One thread
    holding two PRs has no such constraint and is what a duplicate publication
    would actually produce.
    """
    store = seeded["store"]
    store.connection.executemany(
        "INSERT INTO pr_thread_mappings VALUES (?,?,?)",
        [(1, 7, seeded["thread_id"]), (1, 8, seeded["thread_id"])],
    )
    store.connection.commit()
    r = check("INV-PUB-MAPPING", sobs(seeded))
    assert not r.ok and "multiple pull requests" in r.detail


def test_pub_authorized_passes_with_an_accept_for_the_same_cycle(seeded):
    _review(seeded, verdict="ACCEPT")
    _publication(seeded)
    assert check("INV-PUB-AUTHORIZED", sobs(seeded)).ok


def test_pub_authorized_fails_without_an_accept_review(seeded):
    _publication(seeded)
    r = check("INV-PUB-AUTHORIZED", sobs(seeded))
    assert not r.ok and "no ACCEPT review" in r.detail


def _resolution(seeded):
    seeded["store"].connection.execute(
        "INSERT INTO issue_resolution_memory "
        "(resolution_id,repo_id,thread_id,cycle_id,root_input_id,source_event_key,"
        " issue_number,status,attempt_count,created_at,updated_at) "
        "VALUES ('res-1',1,?,?,?,?,7,'COMPLETED',1,'now','now')",
        (
            seeded["thread_id"],
            seeded["cycle_id"],
            seeded["root_event_key"],
            seeded["root_event_key"],
        ),
    )
    seeded["store"].connection.commit()


def test_no_false_resolution_passes_when_the_publication_finalized(seeded):
    _publication(seeded, status="COMPLETED")
    _resolution(seeded)
    assert check("INV-NO-FALSE-RESOLUTION", sobs(seeded)).ok


def test_no_false_resolution_fails_without_a_finalized_publication(seeded):
    _resolution(seeded)
    r = check("INV-NO-FALSE-RESOLUTION", sobs(seeded))
    assert not r.ok and "without a finalized publication" in r.detail


def _candidate(seeded, path="src/a.py"):
    seeded["store"].connection.execute(
        "INSERT INTO repo_memory_candidates "
        "(candidate_id,repo_id,thread_id,cycle_id,root_input_id,source_event_key,"
        " category,fact,durability_reason,evidence_path,evidence_start_line,"
        " evidence_end_line,status,created_at,updated_at) "
        "VALUES ('c1',1,?,?,?,?,'build','f','r',?,1,2,'ACCEPTED','now','now')",
        (
            seeded["thread_id"],
            seeded["cycle_id"],
            seeded["root_event_key"],
            seeded["root_event_key"],
            path,
        ),
    )
    seeded["store"].connection.commit()


def test_no_false_memory_passes_when_evidence_lines_are_cited(seeded):
    _candidate(seeded)
    assert check("INV-NO-FALSE-MEMORY", sobs(seeded)).ok


def test_no_false_memory_fails_when_accepted_without_an_evidence_path(seeded):
    """The schema already forbids null line numbers, so an empty path is the
    reachable way to accept memory that cites nothing."""
    _candidate(seeded, path="")
    r = check("INV-NO-FALSE-MEMORY", sobs(seeded))
    assert not r.ok and "without cited lines" in r.detail


def _learning(seeded, status="FAILED"):
    seeded["store"].connection.execute(
        "INSERT INTO repo_memory_learning "
        "(learning_id,source_event_key,thread_id,cycle_id,root_input_id,repo_id,"
        " status,attempt_count,created_at,updated_at) "
        "VALUES ('l1',?,?,?,?,1,?,1,'now','now')",
        (
            seeded["root_event_key"],
            seeded["thread_id"],
            seeded["cycle_id"],
            seeded["root_event_key"],
            status,
        ),
    )
    seeded["store"].connection.commit()


def test_learning_isolated_passes_when_publication_stayed_completed(seeded):
    _publication(seeded, status="COMPLETED")
    _learning(seeded, status="FAILED")
    assert check("INV-LEARNING-ISOLATED", sobs(seeded)).ok


def test_learning_isolated_fails_when_a_curator_failure_left_publication_broken(seeded):
    _publication(seeded, status="FAILED")
    _learning(seeded, status="FAILED")
    r = check("INV-LEARNING-ISOLATED", sobs(seeded))
    assert not r.ok and "left publication" in r.detail


# -- filesystem confinement and lock order ---------------------------------


def test_worktree_confined_refuses_to_pass_without_planted_markers():
    with pytest.raises(RuntimeError, match="planted deliberately"):
        check("INV-WORKTREE-CONFINED", Observation(events=[ev("PLAN_CREATED")]))


def test_worktree_confined_passes_when_outside_paths_are_untouched(tmp_path):
    from harness.observation import plant_outside_markers

    marker = tmp_path / "outside.txt"
    marker.write_text("original\n")
    o = Observation(outside_markers=plant_outside_markers(marker))
    assert check("INV-WORKTREE-CONFINED", o).ok


def test_worktree_confined_fails_when_an_outside_path_is_modified(tmp_path):
    from harness.observation import plant_outside_markers

    marker = tmp_path / "outside.txt"
    marker.write_text("original\n")
    o = Observation(outside_markers=plant_outside_markers(marker))
    marker.write_text("tampered\n")
    r = check("INV-WORKTREE-CONFINED", o)
    assert not r.ok and "modified" in r.detail


def test_worktree_confined_fails_when_an_outside_path_is_deleted(tmp_path):
    from harness.observation import plant_outside_markers

    marker = tmp_path / "outside.txt"
    marker.write_text("original\n")
    o = Observation(outside_markers=plant_outside_markers(marker))
    marker.unlink()
    r = check("INV-WORKTREE-CONFINED", o)
    assert not r.ok and "deleted" in r.detail


def test_lock_order_passes_for_thread_then_repo_git():
    assert check(
        "INV-LOCK-ORDER",
        obs(
            ev("LOCK_ACQUIRED", 1, lock_kind="thread", lock_key="t1"),
            ev("LOCK_ACQUIRED", 2, lock_kind="repo_git", lock_key="1"),
        ),
    ).ok


def test_lock_order_fails_on_inversion():
    r = check(
        "INV-LOCK-ORDER",
        obs(
            ev("LOCK_ACQUIRED", 1, lock_kind="repo_git", lock_key="1"),
            ev("LOCK_ACQUIRED", 2, lock_kind="thread", lock_key="t1"),
        ),
    )
    assert not r.ok and "while holding" in r.detail
