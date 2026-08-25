"""Structural redaction: the event log must not be able to carry free text."""

import json

import pytest

from sweforge.events import (
    ALLOWED_DATA_FIELDS,
    LOG_ENV,
    MAX_VALUE_CHARS,
    STRICT_ENV,
    EventKind,
    EventSchemaError,
    build_event,
    emit,
    read_log,
)


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(LOG_ENV, str(path))
    monkeypatch.setenv(STRICT_ENV, "1")
    return path


def test_inert_when_unconfigured(monkeypatch):
    monkeypatch.delenv(LOG_ENV, raising=False)
    assert emit(EventKind.PLAN_CREATED, plan_id="plan-1") is None


def test_emits_one_jsonl_line_per_event(log):
    emit(EventKind.PLAN_CREATED, thread_id="t1", cycle_id=2, plan_id="plan-1")
    emit(EventKind.PERMIT_CREATED, thread_id="t1", cycle_id=2, permit_id="p1")
    events = read_log(log)
    assert [item["kind"] for item in events] == ["PLAN_CREATED", "PERMIT_CREATED"]
    assert [item["seq"] for item in events] == sorted(item["seq"] for item in events)
    assert events[0]["thread_id"] == "t1"
    assert events[0]["data"] == {"plan_id": "plan-1"}


def test_every_kind_has_a_schema():
    assert set(ALLOWED_DATA_FIELDS) == set(EventKind)


def test_disallowed_field_raises_under_strict(log):
    with pytest.raises(EventSchemaError, match="not in the schema"):
        emit(EventKind.PLAN_CREATED, plan_text="the entire approved plan body")
    assert not log.exists() or read_log(log) == []


def test_disallowed_field_is_dropped_when_not_strict(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(LOG_ENV, str(path))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    # Production must never crash on a bad emission, but must never write it.
    assert emit(EventKind.PLAN_CREATED, plan_text="secret") is None
    assert not path.exists()


@pytest.mark.parametrize(
    "value",
    [
        {"nested": "dict"},
        ["a", "list"],
        b"bytes",
    ],
)
def test_non_scalar_values_are_unrepresentable(value):
    with pytest.raises(EventSchemaError, match="must be a scalar"):
        build_event(EventKind.PLAN_CREATED, plan_id=value)


def test_long_scalars_are_rejected_so_a_field_cannot_smuggle_text():
    with pytest.raises(EventSchemaError, match="exceeds"):
        build_event(EventKind.PLAN_CREATED, plan_id="x" * (MAX_VALUE_CHARS + 1))


def test_envelope_fields_land_in_the_envelope_not_in_data():
    """Structural, not checked: they are named parameters, bound before **data."""
    event = build_event(EventKind.PLAN_CREATED, plan_id="p", thread_id="t", cycle_id=3)
    assert event["thread_id"] == "t"
    assert event["cycle_id"] == 3
    assert event["data"] == {"plan_id": "p"}


def test_no_schema_permits_free_text_fields():
    """No kind may allowlist a field that could hold a body, diff, or message."""
    banned = {
        "body",
        "text",
        "plan_text",
        "diff",
        "summary",
        "message",
        "content",
        "output",
        "prompt",
        "reasoning",
    }
    for kind, allowed in ALLOWED_DATA_FIELDS.items():
        assert not (allowed & banned), (
            f"{kind} allowlists free text: {allowed & banned}"
        )


def test_log_lines_are_valid_json_with_stable_keys(log):
    emit(EventKind.PR_CREATED, thread_id="t1", pr_number=7, branch="sweforge/issue-7")
    line = log.read_text().strip()
    parsed = json.loads(line)
    assert set(parsed) == {
        "v",
        "ts",
        "seq",
        "run_id",
        "acceptance_mode",
        "kind",
        "thread_id",
        "cycle_id",
        "repo_id",
        "data",
    }
