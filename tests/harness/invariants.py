"""Named invariants: the reusable assertion unit for every scenario.

Scenarios are never collapsed, but their assertions are. A scenario declares
which invariants it requires and a failure names the invariant that broke,
rather than surfacing a traceback from inside a large test file.

Every predicate reads only the Observation bundle, so the same invariant holds
offline and live without a second implementation.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from harness.observation import Observation

Scope = Literal["thread", "cycle", "repo", "campaign"]


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """The outcome of one invariant.

    `substantive` separates "held over something" from "held over nothing".
    A universally quantified check is trivially true on an empty set, so a
    campaign that reported those as plain PASS would present the absence of
    evidence as evidence. Absence assertions ("no permit was created") are
    substantive at zero, because zero is the claim.
    """

    ok: bool
    detail: str = ""
    evidence: dict | None = None
    substantive: bool = True

    @property
    def status(self) -> str:
        if not self.ok:
            return "FAIL"
        return "PASS" if self.substantive else "VACUOUS"


@dataclass(frozen=True, slots=True)
class Invariant:
    id: str
    description: str
    scope: Scope
    predicate: Callable[[Observation], InvariantResult]

    def check(self, observation: Observation) -> InvariantResult:
        return self.predicate(observation)


REGISTRY: dict[str, Invariant] = {}


def register(
    invariant_id: str, description: str, scope: Scope = "cycle"
) -> Callable[[Callable[[Observation], InvariantResult]], Invariant]:
    def wrap(fn: Callable[[Observation], InvariantResult]) -> Invariant:
        if invariant_id in REGISTRY:
            raise ValueError(f"duplicate invariant id: {invariant_id}")
        item = Invariant(invariant_id, description, scope, fn)
        REGISTRY[invariant_id] = item
        return item

    return wrap


def check(invariant_id: str, observation: Observation) -> InvariantResult:
    if invariant_id not in REGISTRY:
        raise KeyError(f"unknown invariant: {invariant_id}")
    return REGISTRY[invariant_id].check(observation)


def _ok(detail: str, *, observed: int | None = None, **evidence) -> InvariantResult:
    """Pass. `observed=0` marks a universal check that ranged over nothing."""
    return InvariantResult(True, detail, evidence or None, observed != 0)


def _fail(detail: str, **evidence) -> InvariantResult:
    return InvariantResult(False, detail, evidence or None)


def _require_events(observation: Observation) -> None:
    """An empty log is unobservable, not proof that nothing happened."""
    if not observation.events:
        raise RuntimeError(
            "no events recorded: the invariant cannot observe anything. Enable "
            "SWEFORGE_EVENT_LOG for this scenario rather than treating an empty "
            "log as a pass."
        )


# -- root and plan ---------------------------------------------------------


@register(
    "INV-ONE-ROOT", "Exactly one durable root event per scenario thread", "thread"
)
def inv_one_root(observation: Observation) -> InvariantResult:
    _require_events(observation)
    roots = observation.events_of("ROOT_INGESTED")
    by_thread: dict[str, int] = {}
    for item in roots:
        by_thread[item.get("thread_id")] = by_thread.get(item.get("thread_id"), 0) + 1
    extra = {tid: n for tid, n in by_thread.items() if n > 1}
    if extra:
        return _fail(f"threads with multiple roots: {extra}", counts=by_thread)
    return _ok(
        f"{len(roots)} root event(s), one per thread",
        observed=len(roots),
        counts=by_thread,
    )


@register("INV-PLAN-CANONICAL", "One canonical plan per thread and cycle")
def inv_plan_canonical(observation: Observation) -> InvariantResult:
    _require_events(observation)
    seen: dict[tuple, list[str]] = {}
    for item in observation.events_of("PLAN_CREATED"):
        key = (item.get("thread_id"), item.get("cycle_id"))
        seen.setdefault(key, []).append(item["data"].get("plan_id"))
    duplicated = {k: v for k, v in seen.items() if len(set(v)) > 1}
    if duplicated:
        return _fail(f"multiple plans for a cycle: {duplicated}", plans=seen)
    return _ok(f"{len(seen)} cycle(s), one plan each", observed=len(seen), plans=seen)


# -- permits ---------------------------------------------------------------


@register("INV-PERMIT-BOUND", "Permit binds the exact plan id and version")
def inv_permit_bound(observation: Observation) -> InvariantResult:
    _require_events(observation)
    plans = {
        (i.get("thread_id"), i.get("cycle_id")): i["data"]
        for i in observation.events_of("PLAN_CREATED")
    }
    permits = observation.events_of("PERMIT_CREATED")
    if not permits:
        return _fail("no permit was created", plans=len(plans))
    for permit in permits:
        key = (permit.get("thread_id"), permit.get("cycle_id"))
        plan = plans.get(key)
        if plan is None:
            return _fail(f"permit for a cycle with no plan: {key}")
        data = permit["data"]
        if data.get("plan_id") != plan.get("plan_id"):
            return _fail(
                f"permit {data.get('permit_id')} binds {data.get('plan_id')}, "
                f"cycle plan is {plan.get('plan_id')}"
            )
        if data.get("plan_version") != plan.get("plan_version"):
            return _fail(
                f"permit binds version {data.get('plan_version')}, "
                f"plan is {plan.get('plan_version')}"
            )
    return _ok(
        f"{len(permits)} permit(s) bound to their exact plan", observed=len(permits)
    )


@register("INV-PERMIT-NONE", "No ExecutionPermit was created")
def inv_permit_none(observation: Observation) -> InvariantResult:
    """Prefers the durable store: a permit is a row, and rows outlive logging.

    Falls back to the event log only when no store is available, and raises if
    neither is, rather than reporting "no permits" from having looked nowhere.
    """
    if observation.store is not None:
        rows = _rows(observation.store, "SELECT permit_id FROM execution_permits")
        if rows:
            return _fail(f"{len(rows)} permit(s) exist: {[row[0] for row in rows][:5]}")
        return _ok("no permit row exists")
    _require_events(observation)
    permits = observation.events_of("PERMIT_CREATED")
    if permits:
        ids = [i["data"].get("permit_id") for i in permits]
        return _fail(f"{len(permits)} permit(s) created: {ids}", permits=ids)
    return _ok("no permit created")


@register("INV-PERMIT-SOURCE", "Permit source is exactly the one the scenario requires")
def inv_permit_source(observation: Observation) -> InvariantResult:
    _require_events(observation)
    sources = {
        i["data"].get("permit_source") for i in observation.events_of("PERMIT_CREATED")
    }
    if not sources:
        return _fail("no permit was created")
    if len(sources) > 1:
        return _fail(f"mixed permit sources in one scenario: {sorted(sources)}")
    return _ok(f"permit source {sources.pop()}")


# -- execution -------------------------------------------------------------


@register("INV-ONE-INITIAL", "Exactly one INITIAL attempt per cycle")
def inv_one_initial(observation: Observation) -> InvariantResult:
    _require_events(observation)
    per_cycle: dict[tuple, set[str]] = {}
    for item in observation.events_of("EXECUTION_STARTED"):
        if item["data"].get("attempt_kind") != "INITIAL":
            continue
        key = (item.get("thread_id"), item.get("cycle_id"))
        per_cycle.setdefault(key, set()).add(item["data"].get("attempt_id"))
    extra = {k: sorted(v) for k, v in per_cycle.items() if len(v) > 1}
    if extra:
        return _fail(f"cycles with multiple INITIAL attempts: {extra}")
    return _ok(
        f"{len(per_cycle)} cycle(s), one INITIAL attempt each", observed=len(per_cycle)
    )


@register("INV-NO-HOT-RETRY", "No further attempt after a terminal execution failure")
def inv_no_hot_retry(observation: Observation) -> InvariantResult:
    _require_events(observation)
    failed_at = {
        (i.get("thread_id"), i.get("cycle_id")): i.get("seq")
        for i in observation.events_of("EXECUTION_FAILED")
    }
    if not failed_at:
        return _ok("no terminal execution failure occurred")
    for item in observation.events_of("EXECUTION_STARTED"):
        key = (item.get("thread_id"), item.get("cycle_id"))
        if key in failed_at and item.get("seq", 0) > failed_at[key]:
            return _fail(
                f"execution started at seq {item.get('seq')} after failure at "
                f"seq {failed_at[key]} for {key}"
            )
    return _ok(f"{len(failed_at)} terminal failure(s), no attempt after any")


@register("INV-RETRY-BOUNDED", "retry_count never exceeds its configured bound")
def inv_retry_bounded(observation: Observation) -> InvariantResult:
    _require_events(observation)
    bound = 3
    over = [
        (i["data"].get("attempt_id"), i["data"].get("retry_count"))
        for i in observation.events_of("EXECUTION_STARTED")
        if (i["data"].get("retry_count") or 0) > bound
    ]
    if over:
        return _fail(f"attempts over retry bound {bound}: {over}", bound=bound)
    return _ok(f"all attempts within retry bound {bound}", bound=bound)


# -- publication -----------------------------------------------------------


@register("INV-ONE-PUBLICATION", "One PR and one completion comment, created once")
def inv_one_publication(observation: Observation) -> InvariantResult:
    facts = observation.github
    if facts is None:
        raise RuntimeError("INV-ONE-PUBLICATION needs GitHubFacts on the Observation")
    prs = facts.pull_requests()
    pr_calls = facts.create_pull_request_calls()
    if len(prs) != 1:
        return _fail(f"expected exactly 1 pull request, found {len(prs)}")
    if pr_calls != 1:
        return _fail(f"pull request created {pr_calls} times; reuse must not re-create")
    return _ok("one pull request, created once", pull_requests=len(prs))


@register("INV-NO-PUBLICATION", "No pull request or publication comment exists")
def inv_no_publication(observation: Observation) -> InvariantResult:
    """Counts publication artifacts only.

    A posted plan is a comment but is not a publication; counting every comment
    made this fail on any scenario that reached plan approval, which is most of
    them.
    """
    facts = observation.github
    if facts is None:
        raise RuntimeError("INV-NO-PUBLICATION needs GitHubFacts on the Observation")
    published = facts.comments_matching("sweforge:publication:")
    # SWEForge's own publications, from the store rather than from a count of
    # the repository's pull requests. A live repository legitimately contains
    # pull requests SWEForge never opened -- S20 opens one deliberately as its
    # test subject -- and counting those conflates a fixture with a
    # publication.
    store = observation.store
    if store is None:
        raise RuntimeError("INV-NO-PUBLICATION needs the store to attribute a PR")
    rows = _rows(
        store,
        "SELECT publication_id, pr_number FROM logical_publications "
        "WHERE pr_number IS NOT NULL",
    )
    if rows or published:
        return _fail(
            f"{len(rows)} publication(s) with a pull request and "
            f"{len(published)} publication comment(s) exist"
        )
    return _ok("nothing was published")


# -- plan versioning -------------------------------------------------------


@register("INV-PLAN-VERSIONED", "Revisions supersede, they never fork")
def inv_plan_versioned(observation: Observation) -> InvariantResult:
    _require_events(observation)
    per_cycle: dict[tuple, list[int]] = {}
    for item in observation.events:
        if item.get("kind") not in {"PLAN_CREATED", "PLAN_REVISED"}:
            continue
        key = (item.get("thread_id"), item.get("cycle_id"))
        per_cycle.setdefault(key, []).append(item["data"].get("plan_version"))
    for key, versions in per_cycle.items():
        if len(versions) != len(set(versions)):
            return _fail(f"repeated plan version in {key}: {versions}")
        if versions != sorted(versions):
            return _fail(f"plan versions not monotonic in {key}: {versions}")
    return _ok(
        f"{len(per_cycle)} cycle(s) with monotonic plan versions",
        observed=len(per_cycle),
    )


# -- attempts --------------------------------------------------------------


@register("INV-ATTEMPT-TERMINAL", "Every started attempt reaches a terminal outcome")
def inv_attempt_terminal(observation: Observation) -> InvariantResult:
    _require_events(observation)
    started = {
        i["data"].get("attempt_id") for i in observation.events_of("EXECUTION_STARTED")
    }
    finished = {
        i["data"].get("attempt_id")
        for kind in ("EXECUTION_SUCCEEDED", "EXECUTION_FAILED")
        for i in observation.events_of(kind)
    }
    dangling = sorted(a for a in started - finished if a)
    if dangling:
        return _fail(f"attempts left non-terminal: {dangling}")
    return _ok(f"{len(started)} attempt(s), all terminal", observed=len(started))


# -- review ----------------------------------------------------------------


@register("INV-REVIEW-LEDGER-FRESH", "Each review attempt is recorded separately")
def inv_review_ledger_fresh(observation: Observation) -> InvariantResult:
    _require_events(observation)
    attempts = observation.events_of("REVIEW_ATTEMPT")
    if not attempts:
        return _fail("no review attempt was recorded")
    seqs = [i.get("seq") for i in attempts]
    if len(seqs) != len(set(seqs)):
        return _fail(f"review attempts share a sequence number: {seqs}")
    return _ok(
        f"{len(attempts)} review attempt(s), each recorded separately",
        observed=len(attempts),
    )


@register(
    "INV-REVIEW-NO-REPAIR-ON-INFRA",
    "Review infrastructure failure never authorizes a repair",
)
def inv_review_no_repair_on_infra(observation: Observation) -> InvariantResult:
    _require_events(observation)
    infra = observation.events_of("REVIEW_INFRA_FAILED")
    if not infra:
        return _ok("no review infrastructure failure occurred")
    repairs = observation.events_of("REPAIR_AUTHORIZED")
    for failure in infra:
        key = (failure.get("thread_id"), failure.get("cycle_id"))
        for repair in repairs:
            same = (repair.get("thread_id"), repair.get("cycle_id")) == key
            if same and repair.get("seq", 0) == failure.get("seq", 0) + 1:
                return _fail(
                    f"repair authorized immediately after infra failure for {key}"
                )
    return _ok(f"{len(infra)} infra failure(s), none authorized a repair")


# -- provenance and routing ------------------------------------------------


@register(
    "INV-PROVENANCE", "root_event_key is preserved across the lifecycle", "thread"
)
def inv_provenance(observation: Observation) -> InvariantResult:
    _require_events(observation)
    roots: dict[tuple, set[str]] = {}
    for item in observation.events:
        key = item["data"].get("root_event_key")
        if not key:
            continue
        cycle = (item.get("thread_id"), item.get("cycle_id"))
        roots.setdefault(cycle, set()).add(key)
    mixed = {k: sorted(v) for k, v in roots.items() if len(v) > 1}
    if mixed:
        return _fail(f"cycles citing multiple roots: {mixed}")
    return _ok(f"{len(roots)} cycle(s), each citing one root", observed=len(roots))


@register("INV-DEFERRED-PRESERVED", "A residual follow-up keeps its own deferred_id")
def inv_deferred_preserved(observation: Observation) -> InvariantResult:
    _require_events(observation)
    deferred = observation.events_of("INPUT_DEFERRED")
    if not deferred:
        return _ok("nothing was deferred")
    ids = [i["data"].get("deferred_id") for i in deferred]
    if any(not item for item in ids):
        return _fail(f"deferred input without a deferred_id: {ids}")
    if len(ids) != len(set(ids)):
        return _fail(f"deferred ids reused: {ids}")
    return _ok(
        f"{len(ids)} deferred input(s), each with a distinct id", observed=len(ids)
    )


@register("INV-NO-INJECTION", "No live input is delivered during execution or review")
def inv_no_injection(observation: Observation) -> InvariantResult:
    _require_events(observation)
    busy: list[tuple[int, int]] = []
    for started, ending in (
        ("EXECUTION_STARTED", ("EXECUTION_SUCCEEDED", "EXECUTION_FAILED")),
        ("REVIEW_ATTEMPT", ("REVIEW_ACCEPTED", "REVIEW_NEEDS_FIXES", "REVIEW_BLOCKED")),
    ):
        for begin in observation.events_of(started):
            ends = [
                i.get("seq")
                for kind in ending
                for i in observation.events_of(kind)
                if i.get("seq", 0) > begin.get("seq", 0)
            ]
            busy.append((begin.get("seq", 0), min(ends) if ends else 10**9))
    for delivered in observation.events_of("INPUT_DELIVERED"):
        seq = delivered.get("seq", 0)
        for start, end in busy:
            if start < seq < end:
                return _fail(
                    f"input delivered at seq {seq} inside an active window "
                    f"({start}, {end})"
                )
    return _ok(f"no input delivered inside {len(busy)} active window(s)")


# -- isolation -------------------------------------------------------------


@register("INV-THREAD-ISOLATION", "No event outside the scenario's threads", "campaign")
def inv_thread_isolation(observation: Observation) -> InvariantResult:
    _require_events(observation)
    if not observation.thread_ids:
        raise RuntimeError(
            "INV-THREAD-ISOLATION needs thread_ids on the Observation; an empty "
            "set would vacuously pass"
        )
    seen = {i.get("thread_id") for i in observation.events if i.get("thread_id")}
    stray = sorted(seen - set(observation.thread_ids))
    if stray:
        return _fail(f"events for foreign threads: {stray}")
    return _ok(f"all events within {len(observation.thread_ids)} declared thread(s)")


@register(
    "INV-REPO-ISOLATION", "No event outside the scenario's repositories", "campaign"
)
def inv_repo_isolation(observation: Observation) -> InvariantResult:
    _require_events(observation)
    if not observation.repo_ids:
        raise RuntimeError(
            "INV-REPO-ISOLATION needs repo_ids on the Observation; an empty set "
            "would vacuously pass"
        )
    seen = {i.get("repo_id") for i in observation.events if i.get("repo_id")}
    stray = sorted(seen - set(observation.repo_ids))
    if stray:
        return _fail(f"events for foreign repositories: {stray}")
    return _ok(f"all events within {len(observation.repo_ids)} declared repo(s)")


# -- publication shape -----------------------------------------------------


@register("INV-NO-EMPTY-COMMIT", "A no-change lifecycle produces no commit")
def inv_no_empty_commit(observation: Observation) -> InvariantResult:
    _require_events(observation)
    commits = observation.events_of("COMMIT_CREATED")
    if commits:
        shas = [i["data"].get("commit_sha") for i in commits]
        return _fail(f"{len(commits)} commit(s) created: {shas}")
    return _ok("no commit was created")


# -- store-backed helpers --------------------------------------------------


def _require_store(observation: Observation, invariant_id: str):
    if observation.store is None:
        raise RuntimeError(
            f"{invariant_id} needs a store on the Observation; without one it "
            "would pass without checking anything"
        )
    return observation.store


def _rows(store, sql: str, params: tuple = ()) -> list:
    return list(store.connection.execute(sql, params))


# -- execution evidence ----------------------------------------------------


@register(
    "INV-EVIDENCE-CONTIGUOUS", "Evidence forms one unbroken cycle-scoped sequence"
)
def inv_evidence_contiguous(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-EVIDENCE-CONTIGUOUS")
    per_cycle: dict[tuple, list[int]] = {}
    for row in _rows(
        store,
        "SELECT thread_id,cycle_id,sequence_number FROM execution_tool_evidence "
        "ORDER BY thread_id,cycle_id,sequence_number",
    ):
        per_cycle.setdefault((row[0], row[1]), []).append(row[2])
    for key, seq in per_cycle.items():
        if len(seq) != len(set(seq)):
            return _fail(f"duplicate evidence sequence in {key}: {seq}")
        if seq != list(range(seq[0], seq[0] + len(seq))):
            return _fail(f"evidence sequence has a gap in {key}: {seq}")
    return _ok(
        f"{len(per_cycle)} cycle(s) with contiguous evidence", observed=len(per_cycle)
    )


@register("INV-EVIDENCE-TRUSTED", "Every review evidence id resolves to a stored row")
def inv_evidence_trusted(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-EVIDENCE-TRUSTED")
    known = {
        row[0]
        for row in _rows(store, "SELECT evidence_id FROM execution_tool_evidence")
    }
    dangling: list[str] = []
    for review_id, ledger in _rows(
        store, "SELECT review_id,read_ledger_json FROM execution_reviews"
    ):
        import json

        for entry in json.loads(ledger or "[]"):
            source = entry.get("source_id") or ""
            if source.startswith("exec-evidence-") and source not in known:
                dangling.append(f"{review_id}:{source}")
    if dangling:
        return _fail(f"review cites unknown execution evidence: {dangling[:5]}")
    return _ok(
        f"all cited execution evidence resolves ({len(known)} row(s))",
        observed=len(known),
    )


@register(
    "INV-REVIEW-GROUNDED", "An accepted review cites evidence for its requirements"
)
def inv_review_grounded(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-REVIEW-GROUNDED")
    import json

    accepted = _rows(
        store,
        "SELECT review_id,requirement_checks_json FROM execution_reviews "
        "WHERE verdict='ACCEPT'",
    )
    if not accepted:
        return _ok("no accepted review to ground")
    for review_id, checks_json in accepted:
        checks = json.loads(checks_json or "[]")
        if not checks:
            return _fail(f"accepted review {review_id} has no requirement checks")
        for check_item in checks:
            if not check_item.get("evidence_refs"):
                return _fail(
                    f"accepted review {review_id} has an ungrounded requirement: "
                    f"{check_item.get('requirement_id')}"
                )
    return _ok(
        f"{len(accepted)} accepted review(s), every requirement grounded",
        observed=len(accepted),
    )


# -- publication -----------------------------------------------------------


@register("INV-PUB-MAPPING", "PR to IssueThread mapping is exact and unique")
def inv_pub_mapping(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-PUB-MAPPING")
    rows = _rows(store, "SELECT repo_id,pr_number,thread_id FROM pr_thread_mappings")
    by_pr: dict[tuple, set[str]] = {}
    by_thread: dict[str, set[tuple]] = {}
    for repo_id, pr_number, thread_id in rows:
        by_pr.setdefault((repo_id, pr_number), set()).add(thread_id)
        by_thread.setdefault(thread_id, set()).add((repo_id, pr_number))
    shared = {k: sorted(v) for k, v in by_pr.items() if len(v) > 1}
    if shared:
        return _fail(f"pull requests mapped to multiple threads: {shared}")
    multi = {k: sorted(v) for k, v in by_thread.items() if len(v) > 1}
    if multi:
        return _fail(f"threads mapped to multiple pull requests: {multi}")
    return _ok(f"{len(rows)} mapping(s), each exact and unique", observed=len(rows))


@register("INV-PUB-AUTHORIZED", "Every publication cites its own cycle's ACCEPT review")
def inv_pub_authorized(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-PUB-AUTHORIZED")
    publications = _rows(
        store,
        "SELECT publication_id,thread_id,cycle_id,status FROM logical_publications "
        "WHERE status IN ('COMPLETED','COMMENTED','PR_CREATED','PUSHED','COMMITTED')",
    )
    if not publications:
        return _ok("no publication reached a committing state")
    accepted = {
        (row[0], row[1])
        for row in _rows(
            store,
            "SELECT thread_id,cycle_id FROM execution_reviews WHERE verdict='ACCEPT'",
        )
    }
    for publication_id, thread_id, cycle_id, status in publications:
        if (thread_id, cycle_id) not in accepted:
            return _fail(
                f"publication {publication_id} ({status}) has no ACCEPT review "
                f"for cycle {(thread_id, cycle_id)}"
            )
    return _ok(
        f"{len(publications)} publication(s), each authorized by its own cycle",
        observed=len(publications),
    )


# -- learning --------------------------------------------------------------


@register("INV-NO-FALSE-RESOLUTION", "No resolution row for unfinalized work")
def inv_no_false_resolution(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-NO-FALSE-RESOLUTION")
    finalized = {
        (row[0], row[1])
        for row in _rows(
            store,
            "SELECT thread_id,cycle_id FROM logical_publications "
            "WHERE status IN ('COMPLETED','NO_CHANGES')",
        )
    }
    rows = _rows(
        store, "SELECT resolution_id,thread_id,cycle_id FROM issue_resolution_memory"
    )
    orphans = [
        resolution_id
        for resolution_id, thread_id, cycle_id in rows
        if (thread_id, cycle_id) not in finalized
    ]
    if orphans:
        return _fail(f"resolution rows without a finalized publication: {orphans[:5]}")
    return _ok(f"{len(rows)} resolution row(s), all finalized", observed=len(rows))


@register("INV-NO-FALSE-MEMORY", "No memory candidate accepted without cited evidence")
def inv_no_false_memory(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-NO-FALSE-MEMORY")
    rows = _rows(
        store,
        "SELECT candidate_id,evidence_path,evidence_start_line,evidence_end_line "
        "FROM repo_memory_candidates WHERE status='ACCEPTED'",
    )
    ungrounded = [
        candidate_id
        for candidate_id, path, start, end in rows
        if not path or start is None or end is None
    ]
    if ungrounded:
        return _fail(f"accepted memory without cited lines: {ungrounded[:5]}")
    return _ok(
        f"{len(rows)} accepted candidate(s), each citing repository lines",
        observed=len(rows),
    )


@register("INV-NO-MEMORY-WRITTEN", "A failing curator accepts no memory candidate")
def inv_no_memory_written(observation: Observation) -> InvariantResult:
    """Absence is the claim, so zero is the evidence, not the lack of it.

    INV-NO-FALSE-MEMORY says "every accepted candidate cites lines", which is
    trivially true when nothing was accepted. A scenario whose point is that a
    failing curator wrote nothing needs this instead.
    """
    store = _require_store(observation, "INV-NO-MEMORY-WRITTEN")
    rows = _rows(
        store,
        "SELECT candidate_id FROM repo_memory_candidates WHERE status='ACCEPTED'",
    )
    if rows:
        return _fail(
            f"{len(rows)} memory candidate(s) were accepted: "
            f"{[row[0] for row in rows[:5]]}"
        )
    return _ok("no memory candidate was accepted")


@register("INV-NO-RESOLUTION-WRITTEN", "A failing curator writes no resolution row")
def inv_no_resolution_written(observation: Observation) -> InvariantResult:
    """Absence is the claim; see INV-NO-MEMORY-WRITTEN."""
    store = _require_store(observation, "INV-NO-RESOLUTION-WRITTEN")
    rows = _rows(store, "SELECT resolution_id FROM issue_resolution_memory")
    if rows:
        return _fail(
            f"{len(rows)} resolution row(s) were written: "
            f"{[row[0] for row in rows[:5]]}"
        )
    return _ok("no resolution row was written")


@register("INV-LEARNING-ISOLATED", "Curator failure never invalidates a publication")
def inv_learning_isolated(observation: Observation) -> InvariantResult:
    store = _require_store(observation, "INV-LEARNING-ISOLATED")
    failed = _rows(
        store,
        "SELECT learning_id,thread_id,cycle_id FROM repo_memory_learning "
        "WHERE status='FAILED'",
    )
    if not failed:
        return _ok("no curator failure occurred")
    for learning_id, thread_id, cycle_id in failed:
        rows = _rows(
            store,
            "SELECT status FROM logical_publications WHERE thread_id=? AND cycle_id=?",
            (thread_id, cycle_id),
        )
        for (status,) in rows:
            if status not in {"COMPLETED", "NO_CHANGES"}:
                return _fail(
                    f"curator failure {learning_id} left publication {status} "
                    f"for cycle {(thread_id, cycle_id)}"
                )
    return _ok(f"{len(failed)} curator failure(s), publications intact")


# -- filesystem and locking ------------------------------------------------


@register("INV-WORKTREE-CONFINED", "No filesystem effect outside the issue worktree")
def inv_worktree_confined(observation: Observation) -> InvariantResult:
    import hashlib
    from pathlib import Path

    if not observation.outside_markers:
        raise RuntimeError(
            "INV-WORKTREE-CONFINED needs outside_markers on the Observation. "
            "Confinement is only checkable against paths planted deliberately "
            "outside the worktree; with none declared this would pass without "
            "checking anything."
        )
    changed: list[str] = []
    for path, expected in observation.outside_markers:
        target = Path(path)
        if not target.exists():
            changed.append(f"{path} (deleted)")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            changed.append(f"{path} (modified)")
    if changed:
        return _fail(f"paths outside the worktree were touched: {changed}")
    return _ok(
        f"{len(observation.outside_markers)} outside path(s) unchanged",
        observed=len(observation.outside_markers),
    )


@register("INV-LOCK-ORDER", "Thread lock is never taken while holding a repo git lock")
def inv_lock_order(observation: Observation) -> InvariantResult:
    _require_events(observation)
    held: list[str] = []
    for item in observation.events_of("LOCK_ACQUIRED"):
        kind = item["data"].get("lock_kind")
        if kind == "thread" and "repo_git" in held:
            return _fail(
                "thread lock acquired while holding a repository git lock "
                f"(held: {held})"
            )
        held.append(kind)
    return _ok(f"{len(held)} lock acquisition(s) in a legal order", observed=len(held))
