"""E8's evidence is what the allowlist decided, not what a scenario claims.

A run that never touched PRIMARY and a run that was never audited look the
same in a summary, so the audit records every decision the guard made.
"""

import pytest
from harness.scenario import Layer, ScenarioResult, campaign_status

from acceptance.runner.allowlist import (
    PrimaryRepositoryRefused,
    check_live_target,
    drain_audit,
)

SANDBOX = "owner/sandbox"
PRIMARY = "owner/primary"


@pytest.fixture(autouse=True)
def _clean_audit(monkeypatch):
    monkeypatch.setenv("SWEFORGE_ACCEPTANCE_REPOS", SANDBOX)
    monkeypatch.setenv("SWEFORGE_PRIMARY_REPOS", PRIMARY)
    drain_audit()
    yield
    drain_audit()


def test_an_allowed_target_is_recorded_as_non_primary():
    check_live_target(SANDBOX)
    (entry,) = drain_audit()
    assert entry == {
        "repository": SANDBOX,
        "target_is_primary": False,
        "allowed": True,
    }


def test_a_refused_primary_target_is_recorded_as_refused():
    """The refusal is the evidence; a silent guard would prove nothing."""
    with pytest.raises(PrimaryRepositoryRefused):
        check_live_target(PRIMARY)
    (entry,) = drain_audit()
    assert entry["target_is_primary"] is True
    assert entry["allowed"] is False


def test_draining_resets_so_runs_do_not_share_entries():
    check_live_target(SANDBOX)
    assert drain_audit()
    assert drain_audit() == []


def test_campaign_status_attributes_entries_to_their_scenario():
    result = ScenarioResult(
        "S1",
        Layer.LIVE_GITHUB,
        True,
        primary_audit=[
            {"repository": SANDBOX, "target_is_primary": False, "allowed": True}
        ],
    )
    audit = campaign_status([result])["primary_audit"]
    assert audit["checks"][0]["scenario_id"] == "S1"
    assert audit["primary_mutations"] == []


def test_an_allowed_primary_target_surfaces_as_a_mutation():
    """The failure this exists to catch: PRIMARY reached and permitted."""
    result = ScenarioResult(
        "S1",
        Layer.LIVE_GITHUB,
        True,
        primary_audit=[
            {"repository": PRIMARY, "target_is_primary": True, "allowed": True}
        ],
    )
    audit = campaign_status([result])["primary_audit"]
    assert audit["primary_mutations"], "an allowed PRIMARY target was not flagged"


def test_a_refused_primary_target_is_not_a_mutation():
    """Refused means protected, not violated."""
    result = ScenarioResult(
        "S1",
        Layer.LIVE_GITHUB,
        True,
        primary_audit=[
            {"repository": PRIMARY, "target_is_primary": True, "allowed": False}
        ],
    )
    assert campaign_status([result])["primary_audit"]["primary_mutations"] == []
