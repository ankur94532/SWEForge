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
