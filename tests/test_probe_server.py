"""Probes must be deterministic and their counts must survive a restart."""

import os

import pytest

from acceptance.probes.ledger import LEDGER_ENV, ProbeLedger


@pytest.fixture
def probes(tmp_path, monkeypatch):
    monkeypatch.setenv(LEDGER_ENV, str(tmp_path / "probes.db"))
    from acceptance.probes import server

    server._connection = None  # a fresh ledger per test
    yield server
    server._connection = None


def test_retryable_fails_once_then_succeeds(probes):
    assert probes.retryable_probe("op-1", 1, "e/r")["ok"] is False
    assert probes.retryable_probe("op-1", 1, "e/r")["ok"] is True


def test_retryable_is_keyed_by_operation_id(probes):
    """A different operation starts its own sequence."""
    probes.retryable_probe("op-1", 1, "e/r")
    assert probes.retryable_probe("op-2", 1, "e/r")["ok"] is False


def test_counts_survive_a_process_restart(probes, tmp_path):
    """JSON state could not do this; S15-S17 kill the dispatcher mid-run."""
    probes.retryable_probe("op-1", 1, "e/r")
    probes._connection = None  # simulate the process dying and coming back
    assert probes.retryable_probe("op-1", 1, "e/r")["ok"] is True
    assert ProbeLedger(os.environ[LEDGER_ENV]).count("retryable_probe", "op-1") == 2


def test_nonretryable_never_succeeds(probes):
    for _ in range(3):
        result = probes.nonretryable_probe("op-1", 1, "e/r")
        assert result["ok"] is False and result["retryable"] is False


def test_warning_is_distinct_from_success_and_failure(probes):
    result = probes.warning_probe("op-1", 1, "e/r")
    assert result["severity"] == "warning"
    assert result["continue_allowed"] is True
    assert result["ok"] is False


def test_fatal_raises(probes):
    with pytest.raises(RuntimeError, match="ACCEPTANCE_FATAL_TOOL_FAILURE"):
        probes.fatal_probe("op-1", 1, "e/r")


def test_timeout_raises_a_timeout_class_error(probes):
    with pytest.raises(TimeoutError, match="ACCEPTANCE_TOOL_TIMEOUT"):
        probes.timeout_probe("op-1", 1, "e/r")


def test_fatal_and_timeout_are_still_recorded(probes):
    with pytest.raises(RuntimeError):
        probes.fatal_probe("op-1", 1, "e/r")
    with pytest.raises(TimeoutError):
        probes.timeout_probe("op-1", 1, "e/r")
    ledger = ProbeLedger(os.environ[LEDGER_ENV])
    assert ledger.count("fatal_probe") == 1
    assert ledger.count("timeout_probe") == 1


def test_identity_echo_returns_only_authoritative_identity(probes):
    result = probes.identity_echo(
        "op-1", 1, "example/repo", workspace_root="/spoof", repo_path="/x", tenant="t"
    )
    assert result == {"repo_id": 1, "repo_full_name": "example/repo"}


def test_identity_echo_records_what_spoofable_fields_arrived(probes):
    """Recording them is what proves the interceptor stripped them."""
    probes.identity_echo("op-1", 1, "example/repo", workspace_root="/spoof")
    (call,) = ProbeLedger(os.environ[LEDGER_ENV]).calls("identity_echo")
    assert call.args_received["workspace_root"] == "/spoof"
    assert call.repo_id == 1


def test_ledger_refuses_to_report_zero_when_it_does_not_exist(tmp_path):
    """An absent ledger means unobservable, not 'no probe calls happened'."""
    ledger = ProbeLedger(tmp_path / "never-created.db")
    with pytest.raises(RuntimeError, match="does not exist"):
        ledger.calls()


def test_call_numbers_are_sequential_per_operation(probes):
    for _ in range(3):
        probes.nonretryable_probe("op-1", 1, "e/r")
    numbers = [
        call.call_number
        for call in ProbeLedger(os.environ[LEDGER_ENV]).calls("nonretryable_probe")
    ]
    assert numbers == [1, 2, 3]
