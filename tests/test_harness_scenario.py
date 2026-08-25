"""A scenario failure must name the invariant, never surface a bare traceback."""

import json
from dataclasses import dataclass

import pytest
from harness.github_fake import FakeGitHub
from harness.observation import LedgerGitHubFacts, Observation
from harness.scenario import (
    SCENARIOS,
    Layer,
    campaign_status,
    run,
    scenario,
    write_campaign_status,
)


@dataclass(frozen=True)
class FiredFault:
    point: str


def ev(kind, seq=1, thread="t1", cycle=1, **data):
    return {
        "kind": kind,
        "seq": seq,
        "thread_id": thread,
        "cycle_id": cycle,
        "data": data,
    }


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Scenario ids are global; keep each test's registrations local."""
    before = dict(SCENARIOS)
    yield
    SCENARIOS.clear()
    SCENARIOS.update(before)


def test_unknown_invariant_is_rejected_at_registration():
    with pytest.raises(ValueError, match="unknown invariants"):
        scenario("X1", layer=Layer.L1, invariants=["INV-NOPE"])(lambda: Observation())


def test_a_scenario_asserting_nothing_is_rejected():
    """It could never fail, so it would report PASS unconditionally."""
    with pytest.raises(ValueError, match="asserts nothing|declares no invariants"):
        scenario("X2", layer=Layer.L1, invariants=[])(lambda: Observation())


def test_duplicate_scenario_id_is_rejected():
    scenario("X3", layer=Layer.L1, invariants=["INV-ONE-ROOT"])(
        lambda: Observation(events=[ev("ROOT_INGESTED")])
    )
    with pytest.raises(ValueError, match="duplicate scenario"):
        scenario("X3", layer=Layer.L1, invariants=["INV-ONE-ROOT"])(
            lambda: Observation(events=[ev("ROOT_INGESTED")])
        )


def test_unknown_scenario_cannot_be_run():
    with pytest.raises(KeyError, match="unknown scenario"):
        run("NOT-REGISTERED")


def test_passing_scenario_reports_each_invariant():
    scenario("X4", layer=Layer.L1, invariants=["INV-ONE-ROOT", "INV-PERMIT-NONE"])(
        lambda: Observation(events=[ev("ROOT_INGESTED")])
    )
    result = run("X4")
    assert result.ok
    assert [c.invariant_id for c in result.checks] == [
        "INV-ONE-ROOT",
        "INV-PERMIT-NONE",
        "FAULTS-DRAINED",
    ]
    assert "INV-ONE-ROOT" in result.report()


def test_failing_scenario_names_the_broken_invariant():
    scenario("X5", layer=Layer.L1, invariants=["INV-ONE-ROOT"])(
        lambda: Observation(events=[ev("ROOT_INGESTED", 1), ev("ROOT_INGESTED", 2)])
    )
    result = run("X5")
    assert not result.ok
    assert [c.invariant_id for c in result.failures()] == ["INV-ONE-ROOT"]
    assert "multiple roots" in result.report()
    assert "Traceback" not in result.report()


def test_a_raising_body_fails_the_scenario_with_its_message():
    def boom():
        raise RuntimeError("workspace vanished")

    scenario("X6", layer=Layer.L1, invariants=["INV-ONE-ROOT"])(boom)
    result = run("X6")
    assert not result.ok
    assert "workspace vanished" in result.report()


def test_an_unevaluable_invariant_fails_rather_than_passes():
    """INV-THREAD-ISOLATION raises without declared threads; that is a FAIL."""
    scenario("X7", layer=Layer.L1, invariants=["INV-THREAD-ISOLATION"])(
        lambda: Observation(events=[ev("PLAN_CREATED")])
    )
    result = run("X7")
    assert not result.ok
    (failure,) = result.failures()
    assert failure.invariant_id == "INV-THREAD-ISOLATION"
    assert "unevaluable" in failure.detail


def test_declared_fault_that_never_fired_fails_the_scenario():
    scenario(
        "X8",
        layer=Layer.L1,
        invariants=["INV-ONE-ROOT"],
        faults=["review.before_inspector"],
    )(lambda: Observation(events=[ev("ROOT_INGESTED")], faults=[]))
    result = run("X8")
    assert not result.ok
    assert any("never fired" in c.detail for c in result.failures())


def test_declared_fault_that_fired_passes():
    scenario(
        "X9",
        layer=Layer.L1,
        invariants=["INV-ONE-ROOT"],
        faults=["review.before_inspector"],
    )(
        lambda: Observation(
            events=[ev("ROOT_INGESTED")], faults=[FiredFault("review.before_inspector")]
        )
    )
    assert run("X9").ok


def test_declaring_faults_without_a_ledger_is_unevaluable():
    scenario(
        "X10",
        layer=Layer.L1,
        invariants=["INV-ONE-ROOT"],
        faults=["review.before_inspector"],
    )(lambda: Observation(events=[ev("ROOT_INGESTED")]))
    with pytest.raises(RuntimeError, match="no fault ledger"):
        run("X10")


def test_scenario_body_receives_arguments():
    scenario("X11", layer=Layer.L1, invariants=["INV-NO-PUBLICATION"])(
        lambda facts: Observation(github=facts)
    )
    assert run("X11", LedgerGitHubFacts(FakeGitHub())).ok


def test_campaign_status_is_machine_checkable(tmp_path):
    scenario("X12", layer=Layer.L1, invariants=["INV-ONE-ROOT"])(
        lambda: Observation(events=[ev("ROOT_INGESTED")])
    )
    scenario("X13", layer=Layer.LIVE_GITHUB, invariants=["INV-ONE-ROOT"])(
        lambda: Observation(events=[ev("ROOT_INGESTED", 1), ev("ROOT_INGESTED", 2)])
    )
    results = [run("X12"), run("X13")]
    status = campaign_status(results)
    assert status["total"] == 2
    assert status["passed"] == 1
    assert status["failed"] == ["X13"]

    path = tmp_path / "reports" / "campaign-status.json"
    written = write_campaign_status(path, results)
    assert json.loads(path.read_text()) == written
    assert written["scenarios"][0]["layer"] == "L1"
