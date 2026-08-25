"""Command-line runner for repository-local acceptance scenarios.

Run with ``python -m acceptance.runner.cli``.  This module deliberately is not
installed with SWEForge: acceptance tooling must never ship in the production
wheel.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acceptance.runner.allowlist import check_live_target
from acceptance.runner.contamination import audit_snapshot, snapshot_run
from acceptance.runner.exit_conditions import SCENARIO_SET as INTEGRATION_SCENARIO_SET
from acceptance.runner.exit_conditions import evaluate_exit_conditions

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAMPAIGN_ROOT = REPO_ROOT / "acceptance" / "reports" / "campaign"
DEFAULT_STATUS_PATH = DEFAULT_CAMPAIGN_ROOT / "campaign-status.json"
DEFAULT_RUNS_ROOT = DEFAULT_CAMPAIGN_ROOT / "runs"
# A single-scenario run must not default to the campaign-wide aggregate: doing
# so once replaced a 14-scenario campaign with one result.
DEFAULT_SINGLE_STATUS_PATH = DEFAULT_CAMPAIGN_ROOT / "single-run-status.json"
MANIFEST_NAME = "run-manifest.json"
MANIFEST_VERSION = 1
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _harness():
    """Import the test-only runner after making its package root importable."""
    tests_root = str(REPO_ROOT / "tests")
    if tests_root not in sys.path:
        sys.path.insert(0, tests_root)
    from harness import scenario as harness_scenario

    return harness_scenario


def discover_scenarios() -> dict[str, Any]:
    """Import every L1 scenario module and return the populated registry."""
    scenario_root = REPO_ROOT / "tests" / "scenarios"
    modules = sorted(
        # Every layer package under tests/scenarios, not just l1: a live body
        # is invisible to the runner if discovery only walks one directory.
        path
        for path in scenario_root.glob("*/*.py")
        if path.name != "__init__.py"
    )
    if not modules:
        raise RuntimeError(f"no scenario modules found under {scenario_root}")
    _harness()
    for path in modules:
        importlib.import_module(f"scenarios.{path.parent.name}.{path.stem}")
    registry = _harness().SCENARIOS
    if not registry:
        raise RuntimeError(
            f"scenario modules under {scenario_root} registered no scenarios"
        )
    return registry


def _layer(value: str):
    normalized = value.strip().upper().replace("-", "_")
    try:
        return _harness().Layer(normalized)
    except ValueError as exc:
        choices = ", ".join(
            item.value.lower().replace("_", "-") for item in _harness().Layer
        )
        raise argparse.ArgumentTypeError(
            f"unknown layer {value!r}; choose one of: {choices}"
        ) from exc


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _new_run_id(scenario_id: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{scenario_id.lower()}-{stamp}-{uuid.uuid4().hex[:8]}"


def _validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run id must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_' or '-'"
        )
    return run_id


def _scenario_sort_key(scenario_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"S(\d+)", scenario_id)
    return (int(match.group(1)), scenario_id) if match else (sys.maxsize, scenario_id)


def _create_manifest(
    runs_root: Path, run_id: str, scenario_id: str, layer: Any
) -> tuple[Path, dict[str, Any]]:
    root = runs_root.expanduser().resolve()
    run_dir = root / _validate_run_id(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / "workspace"
    workspace.mkdir()
    manifest = {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "layer": str(layer),
        "state": "RUNNING",
        "owner_pid": os.getpid(),
        "created_at": _now(),
        "run_dir": str(run_dir),
        "owned_paths": [str(workspace)],
    }
    manifest_path = run_dir / MANIFEST_NAME
    _atomic_json(manifest_path, manifest)
    return manifest_path, manifest


def _record_manifest(
    path: Path, manifest: dict[str, Any], state: str, **fields: Any
) -> None:
    manifest.update(fields)
    manifest["state"] = state
    manifest["updated_at"] = _now()
    _atomic_json(path, manifest)


def _failure(scenario_id: str, layer: Any, error: Exception | str):
    if isinstance(error, str):
        message = error
    else:
        message = f"{type(error).__name__}: {error}"
    return _harness().ScenarioResult(scenario_id, layer, False, (), message)


def execute_scenario(
    scenario_id: str,
    layer: Any,
    *,
    repo_full_name: str | None,
    status_path: Path,
    runs_root: Path,
    run_id: str | None = None,
) -> Any:
    """Execute one registered scenario and always emit campaign status.

    A registered body is an implementation of one particular layer.  Refusing
    a layer mismatch prevents a deterministic L1 body from being reported as a
    live integration run.
    """
    scenario_id = scenario_id.strip().upper()
    manifest_path: Path | None = None
    manifest: dict[str, Any] | None = None
    result = None
    try:
        # Resolution is by (id, layer): a scenario has one body per layer, and
        # asking for a layer with no body must say so rather than run another.
        # Resolved from the registry discover_scenarios returns, so the lookup
        # stays injectable for the preflight-ordering guard.
        registry = discover_scenarios()
        registered = registry.get((scenario_id, layer))
        if registered is None:
            known = sorted(str(key[1]) for key in registry if key[0] == scenario_id)
            raise KeyError(
                f"{scenario_id} is not registered for {layer}"
                + (f"; it exists at {known}" if known else "")
            )

        # The body is the first operation that can perform a live mutation.
        # Preflight immediately before creating any run state or invoking it.
        if layer is _harness().Layer.LIVE_GITHUB:
            check_live_target(repo_full_name or "")

        manifest_path, manifest = _create_manifest(
            runs_root,
            run_id or _new_run_id(scenario_id),
            scenario_id,
            layer,
        )
        workspace = Path(manifest["owned_paths"][0])
        result = _harness().run(scenario_id, workspace, layer=layer)
    except Exception as exc:
        result = _failure(scenario_id, layer, exc)
    finally:
        if result is None:
            result = _failure(scenario_id, layer, "scenario produced no result")
        # The dedicated single-run file is scratch: each run replaces the
        # last. Any other target keeps the loss guard, so pointing --status
        # at the campaign aggregate still cannot silently destroy it.
        _harness().write_campaign_status(
            status_path,
            [result],
            allow_shrink=status_path == DEFAULT_SINGLE_STATUS_PATH,
        )
        if manifest_path is not None and manifest is not None:
            _record_manifest(
                manifest_path,
                manifest,
                "COMPLETED" if result.ok else "FAILED",
                result_ok=result.ok,
                campaign_status=str(status_path.expanduser().resolve()),
            )
    return result


def _result_signature(result: Any) -> tuple[Any, ...]:
    """Stable, complete comparison surface for repeated scenario results."""
    return (
        result.scenario_id,
        str(result.layer),
        result.ok,
        result.error,
        tuple((check.invariant_id, check.ok, check.detail) for check in result.checks),
    )


def _deterministic_layer(registry, scenario_id: str, live_layer):
    """The offline layer a campaign should run this scenario at.

    A campaign measures deterministic reproducibility, so a scenario that also
    has a LIVE_GITHUB body must still be run at its deterministic layer.
    """
    candidates = [
        key[1] for key in registry if key[0] == scenario_id and key[1] is not live_layer
    ]
    if not candidates:
        raise KeyError(f"{scenario_id} has no deterministic body to run")
    if len(candidates) > 1:
        raise ValueError(
            f"{scenario_id} has several deterministic layers {candidates}; "
            "a campaign cannot choose between them"
        )
    return candidates[0]


def execute_campaign(
    scenario_ids: list[str],
    *,
    repetitions: int,
    status_path: Path,
    runs_root: Path,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Run scenarios sequentially and retain every repetition's evidence."""
    if repetitions < 1:
        raise ValueError("campaign repetitions must be at least 1")
    requested = [item.strip().upper() for item in scenario_ids if item.strip()]
    if not requested:
        raise ValueError("campaign requires at least one scenario id")
    if len(requested) != len(set(requested)):
        raise ValueError("campaign scenario ids must be unique")

    registry = discover_scenarios()
    # The registry is keyed by (id, layer); campaigns are requested by id.
    registered_ids = {key[0] for key in registry}
    unknown = sorted(set(requested) - registered_ids)
    if unknown:
        raise KeyError(f"unknown campaign scenarios: {', '.join(unknown)}")

    identity = _validate_run_id(campaign_id or _new_run_id("campaign"))
    # Deterministic means offline: a LIVE_GITHUB body is an integration run and
    # must not be counted toward deterministic reproducibility coverage.
    live_layer = _harness().Layer.LIVE_GITHUB
    deterministic_scenarios = sorted(
        {key[0] for key in registry if key[1] is not live_layer},
        key=_scenario_sort_key,
    )
    campaign_runs_root = runs_root.expanduser().resolve() / identity
    repeated_results: list[list[Any]] = []
    contamination_snapshots = []
    contamination_checks = 0
    contamination_violations: list[dict[str, Any]] = []
    contamination_observed: set[str] = set()
    for repetition in range(1, repetitions + 1):
        current = []
        for scenario_id in requested:
            run_id = f"pass-{repetition}-{scenario_id.lower()}"
            result = execute_scenario(
                scenario_id,
                _deterministic_layer(registry, scenario_id, live_layer),
                repo_full_name=None,
                status_path=(campaign_runs_root / "per-run" / f"{run_id}-status.json"),
                runs_root=campaign_runs_root / "scenario-runs",
                run_id=run_id,
            )
            current.append(result)
            workspace = campaign_runs_root / "scenario-runs" / run_id / "workspace"
            try:
                captured = snapshot_run(scenario_id, repetition, workspace)
            except Exception as exc:
                contamination_violations.append(
                    {
                        "scenario_id": scenario_id,
                        "repetition": repetition,
                        "kind": "unevaluable",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                contamination_snapshots.append(captured)
                contamination_checks += captured.checks
                contamination_violations.extend(captured.violations)
                contamination_observed.add(scenario_id)

            # Recheck every earlier run after each scenario. A scenario that
            # writes into a prior workspace is a campaign isolation failure,
            # even if both scenarios pass their own invariants.
            for captured in contamination_snapshots:
                checks, violations = audit_snapshot(
                    captured, after_scenario_id=scenario_id
                )
                contamination_checks += checks
                contamination_violations.extend(violations)
        repeated_results.append(current)

    per_scenario = {}
    for index, scenario_id in enumerate(requested):
        signatures = [_result_signature(results[index]) for results in repeated_results]
        per_scenario[scenario_id] = {
            "identical": all(item == signatures[0] for item in signatures[1:]),
            "runs": repetitions,
        }
    reproducible = all(item["identical"] for item in per_scenario.values())

    harness = _harness()
    final_results = repeated_results[-1]
    status = harness.write_campaign_status(status_path, final_results)
    repetition_statuses = []
    for repetition, results in enumerate(repeated_results, start=1):
        run_status = harness.campaign_status(results)
        for item in run_status["scenarios"]:
            # This runner never performs a harness-level retry. SWEForge's own
            # retry behavior remains inside the scenario and is not counted here.
            item["harness_retries"] = 0
            item["outcome"] = "PASS" if item["ok"] else "FAIL"
        repetition_statuses.append({"index": repetition, "status": run_status})
    final_by_id = {
        item["id"]: item for item in repetition_statuses[-1]["status"]["scenarios"]
    }
    for item in status["scenarios"]:
        item["harness_retries"] = final_by_id[item["id"]]["harness_retries"]
        item["outcome"] = final_by_id[item["id"]]["outcome"]
    status.update(
        {
            "schema_version": 3,
            "campaign_id": identity,
            "generated_at": _now(),
            "requested_scenarios": requested,
            "deterministic_scenarios": deterministic_scenarios,
            "repetitions": repetition_statuses,
            "execution_integrity": {
                "observed_ids": requested,
                "skipped": [],
                "substitutions": [],
            },
            "reproducibility": {
                "required_runs": repetitions,
                "identical": reproducible,
                "scenarios": per_scenario,
            },
            "contamination": {
                "checks": contamination_checks,
                "violations": contamination_violations,
                "observed_ids": sorted(
                    contamination_observed & INTEGRATION_SCENARIO_SET
                ),
                "auxiliary_observed_ids": sorted(
                    contamination_observed - INTEGRATION_SCENARIO_SET
                ),
                "snapshots": [item.payload() for item in contamination_snapshots],
            },
        }
    )
    _atomic_json(status_path, status)
    return status


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owned_path(value: Any, *, run_dir: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("owned_paths entries must be non-empty strings")
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError(f"refusing owned path outside run directory: {path}") from exc
    if path == run_dir:
        raise ValueError("refusing to reap the run directory itself")
    return path


def reap_runs(runs_root: Path) -> tuple[list[str], list[str]]:
    """Reap explicitly owned local paths for crashed runs.

    Completed/failed runs are evidence and are retained.  A RUNNING manifest is
    reaped only when its recorded owner PID no longer exists.  Malformed or
    out-of-root ownership fails closed and is reported without deleting data.
    """
    root = runs_root.expanduser().resolve()
    if not root.exists():
        return [], []
    reaped: list[str] = []
    errors: list[str] = []
    for manifest_path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
        try:
            run_dir = manifest_path.parent.resolve()
            run_dir.relative_to(root)
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("version") != MANIFEST_VERSION:
                raise ValueError("unsupported or missing manifest version")
            if Path(str(manifest.get("run_dir", ""))).resolve() != run_dir:
                raise ValueError("manifest run_dir does not match its directory")
            if manifest.get("state") != "RUNNING":
                continue
            owner_pid = manifest.get("owner_pid")
            if not isinstance(owner_pid, int):
                raise ValueError("manifest owner_pid is not an integer")
            if _pid_alive(owner_pid):
                continue
            owned = manifest.get("owned_paths")
            if not isinstance(owned, list) or not owned:
                raise ValueError("RUNNING manifest has no observable owned_paths")
            paths = [_owned_path(item, run_dir=run_dir) for item in owned]
            for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path)
            _record_manifest(manifest_path, manifest, "REAPED", reaped_at=_now())
            reaped.append(str(manifest.get("run_id", run_dir.name)))
        except Exception as exc:
            errors.append(f"{manifest_path}: {type(exc).__name__}: {exc}")
    return reaped, errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SWEForge acceptance scenarios")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="run one registered scenario")
    run_parser.add_argument("scenario_id")
    run_parser.add_argument("--layer", required=True, type=_layer)
    run_parser.add_argument(
        "--repo",
        dest="repo_full_name",
        help="owner/name target; required and allowlisted for LIVE_GITHUB",
    )
    run_parser.add_argument("--status", type=Path, default=DEFAULT_SINGLE_STATUS_PATH)
    run_parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    run_parser.add_argument("--run-id")

    campaign_parser = commands.add_parser(
        "campaign", help="run scenarios sequentially with repeatability evidence"
    )
    campaign_parser.add_argument("scenario_ids", nargs="+")
    campaign_parser.add_argument("--repetitions", type=int, default=2)
    campaign_parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    campaign_parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    campaign_parser.add_argument("--campaign-id")

    reap_parser = commands.add_parser(
        "reap", help="clean explicitly owned paths left by crashed runs"
    )
    reap_parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)

    exit_parser = commands.add_parser(
        "check-exit", help="evaluate the eight campaign exit conditions"
    )
    exit_parser.add_argument(
        "status", type=Path, nargs="?", default=DEFAULT_STATUS_PATH
    )
    exit_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        result = execute_scenario(
            args.scenario_id,
            args.layer,
            repo_full_name=args.repo_full_name,
            status_path=args.status,
            runs_root=args.runs_root,
            run_id=args.run_id,
        )
        print(result.report())
        return 0 if result.ok else 1

    if args.command == "campaign":
        try:
            status = execute_campaign(
                args.scenario_ids,
                repetitions=args.repetitions,
                status_path=args.status,
                runs_root=args.runs_root,
                campaign_id=args.campaign_id,
            )
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        for repetition in status["repetitions"]:
            print(f"REPETITION {repetition['index']}")
            for result in repetition["status"]["scenarios"]:
                verdict = "PASS" if result["ok"] else "FAIL"
                print(f"{result['id']}  {verdict}  [{result['layer']}]")
                if result["error"]:
                    print(f"  scenario body raised: {result['error']}")
                for check in result["checks"]:
                    check_verdict = check.get(
                        "status", "PASS" if check["ok"] else "FAIL"
                    )
                    print(
                        f"  {check['invariant']:<32} {check_verdict}  {check['detail']}"
                    )
        reproducible = status["reproducibility"]["identical"]
        print(f"REPRODUCIBILITY  {'IDENTICAL' if reproducible else 'DIFFERENT'}")
        vacuous = any(
            check.get("status") == "VACUOUS"
            for repetition in status["repetitions"]
            for scenario in repetition["status"]["scenarios"]
            for check in scenario["checks"]
        )
        return 0 if not status["failed"] and reproducible and not vacuous else 1

    if args.command == "check-exit":
        try:
            status = json.loads(args.status.read_text())
            report = evaluate_exit_conditions(status)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if args.output is not None:
            _atomic_json(args.output, report)
        for condition in report["conditions"]:
            print(
                f"{condition['condition_id']}  {condition['state']:<15} "
                f"{condition['detail']}"
            )
            print(f"  evidence: {json.dumps(condition['evidence'], sort_keys=True)}")
        print(f"CAMPAIGN EXIT  {'READY' if report['ready'] else 'NOT READY'}")
        return 0

    reaped, errors = reap_runs(args.runs_root)
    for run_id in reaped:
        print(f"REAPED {run_id}")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    if not reaped and not errors:
        print("No crashed runs to reap.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
