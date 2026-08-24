import json
from pathlib import Path

import pytest

from sweforge.review_fixture import (
    FixtureQuarantined,
    ReviewerContext,
    capture_failure_count,
    load_fixture,
    parse_dispatcher_failure,
    reset_capture_failure_count,
    review_requirement_contract,
    write_fixture,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "acceptance/fixtures/review/v1"


@pytest.mark.parametrize("fixture_path", sorted(FIXTURE_ROOT.iterdir()))
def test_committed_fixture_round_trip_and_offline_replay(fixture_path, tmp_path):
    fixture = load_fixture(fixture_path)
    assert review_requirement_contract(fixture["evidence"]) == fixture["contract"]

    from sweforge.review_fixture_cli import replay_main

    report = tmp_path / "replay.json"
    code = replay_main(
        [str(fixture_path), "--offline", "--runs", "1", "--report", str(report)]
    )
    result = json.loads(report.read_text())["results"][0]
    assert code == (0 if fixture["outcome"] else 1)
    assert result["ok"] is bool(fixture["outcome"])


def test_dispatcher_diagnostic_maps_guard_sites_and_preserves_unknowns():
    parsed = parse_dispatcher_failure(
        "ReviewFinalizationError: inspector failed; diagnostic="
        + json.dumps(
            {
                "artifact_problems": [
                    "missing direct code observation for plan:step:1",
                    "new guard detail",
                ],
                "attempt": 2,
                "reads": [{"path": "README.md", "offset": 0}],
            }
        )
    )
    assert parsed["exception_type"] == "ReviewFinalizationError"
    assert parsed["message"] == "inspector failed"
    assert parsed["guard_codes"] == [
        "IA-MISSING-DIRECT-CODE-OBSERVATION",
        "UNMAPPED:new guard detail",
    ]
    assert parsed["reads"]


def test_unparseable_dispatcher_diagnostic_is_not_invented():
    parsed = parse_dispatcher_failure("ReviewFinalizationError: truncated")
    assert parsed == {"reason": "missing ; diagnostic= delimiter"}


def test_secret_scan_quarantines_fixture(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "config.txt").write_text("api_key=definitely-fake-secret\n")
    context = ReviewerContext(worktree=str(worktree))
    with pytest.raises(FixtureQuarantined):
        write_fixture(
            tmp_path / "fixtures",
            fixture_id="RF-secret",
            evidence={"source": {}},
            context=context,
            provenance={},
        )
    assert (tmp_path / "fixtures/RF-secret.quarantine").is_dir()


def test_capture_failure_is_nonfatal(monkeypatch, tmp_path):
    from test_review_execution_completion import execution_ready_fixture

    from sweforge.reviewer import ExecutionReviewResult

    _store, engine, _repo, _root, thread_id, execute_kwargs = execution_ready_fixture(
        tmp_path
    )
    execute_kwargs["runner"] = lambda **_: "executor response"
    first = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert first.phase.value == "REVIEW_EXECUTION"
    engine.reviewer = lambda **_: ExecutionReviewResult(
        verdict="ACCEPT", summary="accepted"
    )
    monkeypatch.setenv("SWEFORGE_REVIEW_FIXTURE_DIR", str(tmp_path / "fixtures"))
    monkeypatch.setattr(
        "sweforge.workflow.write_fixture",
        lambda **_: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    reset_capture_failure_count()
    result = engine.advance(
        thread_id=thread_id,
        model="planning-sonnet",
        review_model="review-sonnet",
        repo_paths=execute_kwargs["repo_paths"],
        workspace_root=execute_kwargs["workspace_root"],
        execute_kwargs=execute_kwargs,
    )
    assert result.phase.value == "AWAITING_PUBLICATION"
    assert capture_failure_count() == 1
