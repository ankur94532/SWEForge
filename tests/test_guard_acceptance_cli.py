"""Acceptance CLI safety, reporting, discovery and crash-reaper guards."""

import json
import os
from pathlib import Path

from harness.scenario import Layer

from acceptance.runner import cli


def test_cli_discovers_and_runs_a_registered_scenario(tmp_path):
    status = tmp_path / "campaign-status.json"
    result = cli.execute_scenario(
        "S14",
        Layer.L1_PROCESS,
        repo_full_name=None,
        status_path=status,
        runs_root=tmp_path / "runs",
        run_id="s14-test",
    )
    assert result.ok, result.report()
    written = json.loads(status.read_text())
    assert written["total"] == written["passed"] == 1
    assert written["scenarios"][0]["id"] == "S14"


def test_layer_mismatch_fails_instead_of_misreporting_l1_as_live(tmp_path):
    """S5 has a deterministic body and no live one.

    S1 was used here until it gained a LIVE_GITHUB body of its own; asking for
    a layer that does exist would then test the allowlist, not resolution.
    """
    status = tmp_path / "campaign-status.json"
    result = cli.execute_scenario(
        "S5",
        Layer.LIVE_GITHUB,
        repo_full_name="example/acceptance",
        status_path=status,
        runs_root=tmp_path / "runs",
    )
    assert not result.ok
    assert "not registered for" in result.error
    assert "L1" in result.error, "the error must say where the body does exist"
    assert json.loads(status.read_text())["failed"] == ["S5"]
    assert not (tmp_path / "runs").exists()


def test_live_preflight_occurs_before_body_or_run_state(monkeypatch, tmp_path):
    class Registered:
        layer = Layer.LIVE_GITHUB

    monkeypatch.setattr(
        cli,
        "discover_scenarios",
        lambda: {("X1", Layer.LIVE_GITHUB): Registered()},
    )

    def refused(repo):
        assert repo == "example/primary"
        raise RuntimeError("PRIMARY refused")

    monkeypatch.setattr(cli, "check_live_target", refused)
    status = tmp_path / "campaign-status.json"
    result = cli.execute_scenario(
        "X1",
        Layer.LIVE_GITHUB,
        repo_full_name="example/primary",
        status_path=status,
        runs_root=tmp_path / "runs",
    )
    assert not result.ok
    assert "PRIMARY refused" in result.error
    assert not (tmp_path / "runs").exists(), "run state preceded live preflight"
    assert json.loads(status.read_text())["failed"] == ["X1"]


def _manifest(run_dir: Path, *, owner_pid: int, state: str = "RUNNING") -> Path:
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "evidence.tmp").write_text("owned")
    path = run_dir / cli.MANIFEST_NAME
    path.write_text(
        json.dumps(
            {
                "version": cli.MANIFEST_VERSION,
                "run_id": run_dir.name,
                "scenario_id": "S14",
                "layer": "L1_PROCESS",
                "state": state,
                "owner_pid": owner_pid,
                "run_dir": str(run_dir.resolve()),
                "owned_paths": [str(workspace.resolve())],
            }
        )
    )
    return path


def test_standalone_reaper_cleans_a_crashed_runs_owned_paths(tmp_path):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "dead-run"
    manifest_path = _manifest(run_dir, owner_pid=2**30)

    reaped, errors = cli.reap_runs(runs_root)

    assert reaped == ["dead-run"]
    assert errors == []
    assert not (run_dir / "workspace").exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["state"] == "REAPED"
    assert manifest["reaped_at"], "reaping was not recorded as observable evidence"


def test_reaper_never_cleans_an_active_run(tmp_path):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "active-run"
    manifest_path = _manifest(run_dir, owner_pid=os.getpid())

    reaped, errors = cli.reap_runs(runs_root)

    assert reaped == []
    assert errors == []
    assert (run_dir / "workspace" / "evidence.tmp").read_text() == "owned"
    assert json.loads(manifest_path.read_text())["state"] == "RUNNING"


def test_reaper_fails_closed_on_an_out_of_run_owned_path(tmp_path):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "malformed-run"
    manifest_path = _manifest(run_dir, owner_pid=2**30)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-survive").write_text("safe")
    manifest = json.loads(manifest_path.read_text())
    manifest["owned_paths"] = [str(outside)]
    manifest_path.write_text(json.dumps(manifest))

    reaped, errors = cli.reap_runs(runs_root)

    assert reaped == []
    assert len(errors) == 1 and "outside run directory" in errors[0]
    assert (outside / "must-survive").read_text() == "safe"
