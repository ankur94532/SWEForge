"""Emissions must fire on durable transitions and carry only schema fields."""

from sweforge.events import LOG_ENV, STRICT_ENV, EventKind, read_log


def test_emission_sites_use_only_schema_fields(tmp_path, monkeypatch):
    """Strict mode turns a mistyped emission field into a test failure."""
    import inspect

    from sweforge import workflow
    from sweforge.events import ALLOWED_DATA_FIELDS, ENVELOPE_FIELDS

    source = inspect.getsource(workflow)
    for chunk in source.split("emit(\n")[1:]:
        body = chunk.split(")", 1)[0]
        kind_line = next(
            (line for line in body.splitlines() if "EventKind." in line), None
        )
        assert kind_line, "every emit() must name an EventKind"
        kind = EventKind[kind_line.strip().rstrip(",").split(".")[-1]]
        allowed = ALLOWED_DATA_FIELDS[kind] | ENVELOPE_FIELDS
        for line in body.splitlines():
            stripped = line.strip()
            if "=" not in stripped or "EventKind." in stripped:
                continue
            field = stripped.split("=", 1)[0].strip()
            if not field.isidentifier():
                continue
            assert field in allowed, f"{kind}: {field!r} is not in the schema"


def test_log_round_trips(tmp_path, monkeypatch):
    from sweforge.events import emit

    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(LOG_ENV, str(path))
    monkeypatch.setenv(STRICT_ENV, "1")
    emit(
        EventKind.EXECUTION_STARTED,
        thread_id="t",
        cycle_id=1,
        attempt_id="a1",
        attempt_kind="INITIAL",
        permit_id="p1",
    )
    (event,) = read_log(path)
    assert event["kind"] == "EXECUTION_STARTED"
    assert event["data"]["attempt_id"] == "a1"


def test_root_ingested_is_emitted_when_a_thread_is_first_created(tmp_path, monkeypatch):
    """The schema had this kind with no emitter, so invariants reading it
    passed vacuously."""
    import sys

    sys.path.insert(0, str(tmp_path.parents[0]))
    from harness.world import World

    from sweforge.events import read_log

    world = World.build(tmp_path / "w")
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        roots = [
            item
            for item in read_log(world.event_log)
            if item["kind"] == "ROOT_INGESTED"
        ]
    assert len(roots) == 1, f"expected exactly one root event, got {len(roots)}"
    assert roots[0]["thread_id"] in world.thread_ids
    assert roots[0]["repo_id"] == world.repo.repo_id


def test_root_ingested_fires_once_per_thread_not_per_event(tmp_path):
    """A second comment on the same issue is not a new root."""
    from harness.world import World

    from sweforge.events import read_log

    world = World.build(tmp_path / "w2")
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        world.ingest(world.event("2", "@agent also this", "2026-01-01T00:00:05Z"))
        roots = [
            item
            for item in read_log(world.event_log)
            if item["kind"] == "ROOT_INGESTED"
        ]
    assert len(roots) == 1, f"a follow-up comment created {len(roots)} roots"


def test_input_delivered_is_emitted_when_an_input_is_consumed(tmp_path):
    """Without this emitter INV-NO-INJECTION could never observe a delivery.

    A scripted planner would bypass delivery entirely: the delivered-key set is
    populated by LiveInputMiddleware, so this planner runs the real middleware
    over the real provider and lets the workflow acknowledge what it reports.
    """
    from harness.world import World

    from sweforge.agent import LiveInputMiddleware
    from sweforge.events import read_log
    from sweforge.github_store import WorkflowPhase

    def planner(*, context, model, task, historical_cases):
        middleware = LiveInputMiddleware(
            context.live_input_provider, context.live_delivered_event_keys
        )
        middleware.before_model({"messages": []}, None)
        return "1. a\n2. b"

    world = World.build(tmp_path / "w3", planner=planner)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        world.ingest(
            world.event("2", "@agent also check the config", "2026-01-01T00:00:05Z")
        )
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        delivered = [
            item
            for item in read_log(world.event_log)
            if item["kind"] == "INPUT_DELIVERED"
        ]
    assert delivered, "no input delivery was recorded; the emitter is unreachable"
    assert all(item["data"].get("event_key") for item in delivered)
    assert {item["data"]["purpose"] for item in delivered} == {"PLANNING_INPUT"}


def test_input_delivered_is_not_emitted_when_nothing_is_delivered(tmp_path):
    """Positive control: the emitter must not fire on an empty delivery set,
    or the assertion above would pass even if delivery were broken."""
    from harness.models import ScriptedPlanner
    from harness.world import World

    from sweforge.events import read_log
    from sweforge.github_store import WorkflowPhase

    world = World.build(tmp_path / "w4", planner=ScriptedPlanner(plans=["1. a\n2. b"]))
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        world.ingest(
            world.event("2", "@agent also check the config", "2026-01-01T00:00:05Z")
        )
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        kinds = [item["kind"] for item in read_log(world.event_log)]
    # Prove the log was live, so absence means "not emitted", not "not logging".
    assert "PLAN_CREATED" in kinds, f"event log was not recording at all: {kinds}"
    assert "INPUT_DELIVERED" not in kinds, "delivery claimed without a consumer"
