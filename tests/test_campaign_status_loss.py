"""Campaign evidence must not be destroyed by a narrower write.

A single-scenario `cli run` overwrote a 14-scenario campaign aggregate with
its own one result. The only visible symptom was an exit condition silently
dropping from 14/26 to 1/26.
"""

import json

import pytest
from harness.scenario import (
    CampaignStatusLoss,
    Layer,
    ScenarioResult,
    write_campaign_status,
)

from acceptance.runner.cli import DEFAULT_SINGLE_STATUS_PATH, DEFAULT_STATUS_PATH


def _result(scenario_id: str) -> ScenarioResult:
    return ScenarioResult(scenario_id, Layer.L1, True)


def _campaign(tmp_path):
    path = tmp_path / "campaign-status.json"
    write_campaign_status(path, [_result(f"S{n}") for n in (3, 5, 6, 7)])
    return path


def test_a_narrower_write_is_refused(tmp_path):
    path = _campaign(tmp_path)
    with pytest.raises(CampaignStatusLoss) as excinfo:
        write_campaign_status(path, [_result("S22")])
    assert "S3" in str(excinfo.value), "the refusal must name what would be lost"


def test_the_refusal_leaves_the_recorded_evidence_intact(tmp_path):
    path = _campaign(tmp_path)
    with pytest.raises(CampaignStatusLoss):
        write_campaign_status(path, [_result("S22")])
    kept = {item["id"] for item in json.loads(path.read_text())["scenarios"]}
    assert kept == {"S3", "S5", "S6", "S7"}, "evidence was damaged despite refusing"


def test_a_superset_write_is_allowed(tmp_path):
    """A campaign re-run that adds scenarios must not be blocked."""
    path = _campaign(tmp_path)
    status = write_campaign_status(path, [_result(f"S{n}") for n in (3, 5, 6, 7, 12)])
    assert len(status["scenarios"]) == 5


def test_an_identical_rewrite_is_allowed(tmp_path):
    path = _campaign(tmp_path)
    write_campaign_status(path, [_result(f"S{n}") for n in (3, 5, 6, 7)])


def test_shrinking_is_possible_when_asked_for_explicitly(tmp_path):
    path = _campaign(tmp_path)
    status = write_campaign_status(path, [_result("S22")], allow_shrink=True)
    assert [item["id"] for item in status["scenarios"]] == ["S22"]


def test_writing_a_fresh_path_is_unaffected(tmp_path):
    status = write_campaign_status(tmp_path / "new.json", [_result("S22")])
    assert len(status["scenarios"]) == 1


def test_a_single_run_does_not_default_to_the_campaign_aggregate():
    """The second, independent fix: even without the guard above, `run` must
    not aim at the campaign-wide file."""
    assert DEFAULT_SINGLE_STATUS_PATH != DEFAULT_STATUS_PATH
    assert DEFAULT_SINGLE_STATUS_PATH.name == "single-run-status.json"
