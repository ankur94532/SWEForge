"""Campaign aggregation must retain every run and never substitute results."""

import json

from acceptance.runner import cli


def test_campaign_runs_registered_layer_twice_and_retains_evidence(tmp_path):
    status_path = tmp_path / "campaign" / "campaign-status.json"
    status = cli.execute_campaign(
        ["S14"],
        repetitions=2,
        status_path=status_path,
        runs_root=tmp_path / "campaign" / "runs",
        campaign_id="guard-campaign",
    )

    assert status["total"] == status["passed"] == 1
    assert status["failed"] == []
    assert status["requested_scenarios"] == ["S14"]
    assert status["execution_integrity"] == {
        "observed_ids": ["S14"],
        "skipped": [],
        "substitutions": [],
    }
    assert len(status["repetitions"]) == 2
    for repetition in status["repetitions"]:
        (result,) = repetition["status"]["scenarios"]
        assert result["id"] == "S14"
        assert result["layer"] == "L1_PROCESS"
        assert result["checks"], "per-invariant evidence was discarded"
        assert all(check["ok"] for check in result["checks"])
        assert result["harness_retries"] == 0
        assert result["outcome"] == "PASS"
    assert status["reproducibility"] == {
        "required_runs": 2,
        "identical": True,
        "scenarios": {"S14": {"identical": True, "runs": 2}},
    }
    assert json.loads(status_path.read_text()) == status


def test_campaign_rejects_duplicate_ids_instead_of_substituting_results(tmp_path):
    try:
        cli.execute_campaign(
            ["S14", "S14"],
            repetitions=2,
            status_path=tmp_path / "status.json",
            runs_root=tmp_path / "runs",
        )
    except ValueError as exc:
        assert "must be unique" in str(exc)
    else:
        raise AssertionError("duplicate scenario ids were accepted")
