"""Frozen-fixture conformance measurement for execution review."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sweforge.context import RepoAgentContext
from sweforge.review_fixture import FixtureError, load_fixture
from sweforge.reviewer import (
    ExecutionReviewResult,
    InspectionReport,
    ReviewAttemptObservation,
    ReviewerContext,
    ReviewFinalizationError,
    SemanticReviewArtifact,
    _accept_coverage_problems,
    _apply_accept_coverage_guard,
    _canonical_finalizer_provenance,
    _canonical_inspection_provenance,
    _fail_closed_unavailable_inspections,
    _guard_repairability,
    _inspection_artifact_problems,
    _resolved_evidence,
    review_execution,
    review_requirement_contract,
)

Classification = Literal["OK", "A", "B", "UNCLASSIFIED", "ERROR", "OPERATIONAL"]

# Infrastructure faults are not measurements of the reviewer. A run killed by
# missing credentials or a rate limit says nothing about guard behaviour, and
# folding it into the rates makes an outage look like a regression.
_OPERATIONAL_SIGNATURES = (
    "missing credentials",
    "api_key",
    "rate_limit",
    "rate limit",
    "error code: 429",
    "error code: 500",
    "error code: 502",
    "error code: 503",
    "error code: 529",
    "connection error",
    "timed out",
    "temporarily unavailable",
    # SWEForge's own marker for a provider/structured-output finalizer failure.
    # Empty guard_codes cannot serve here: ReviewFinalizationError populates
    # that key as [] even when no diagnostic was supplied, so it cannot
    # distinguish "no guard rejected" from "diagnostic never filled in".
    "failed operationally",
)


def is_operational_error(error: BaseException | None) -> bool:
    """True when a run failed for infrastructure reasons, not reviewer reasons."""
    if error is None:
        return False
    text = f"{type(error).__name__}: {error}".lower()
    return any(token in text for token in _OPERATIONAL_SIGNATURES)


FixtureClass = Literal["RF016", "STABLE", "CONTESTED"]

RF016 = "RF-016-inspector-authority"
STABLE_FIXTURES = frozenset({"RF-10-5ad6d25f", "RF-12-deb019b0", "RF-14-41504759"})
CONTESTED_FIXTURES = frozenset(
    {"RF-7-a34d33a8", "RF-11-8cb404e7", "RF-13-7fda18a4", "RF-14-52e49d5a"}
)
KNOWN_FIXTURES = frozenset({RF016, *STABLE_FIXTURES, *CONTESTED_FIXTURES})
DEFAULT_CLASSIFICATIONS = Path(__file__).parents[1] / "guard_classification.json"
REPLAY_CAVEAT = (
    "Replay proves what current code does with previously observed model output. "
    "It cannot predict how the model responds to a changed prompt or schema."
)

RunOnce = Callable[[Callable[[ReviewAttemptObservation], None]], ExecutionReviewResult]


def load_guard_classifications(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("guard classification file must contain an object")
    result: dict[str, str] = {}
    for code, entry in raw.items():
        if not isinstance(entry, dict) or entry.get("class") not in {
            "A",
            "B",
            "UNKNOWN",
        }:
            raise ValueError(f"invalid classification for {code}")
        result[str(code)] = str(entry["class"])
    return result


def classify_guard_codes(
    codes: Iterable[str], classifications: dict[str, str]
) -> Classification:
    observed = tuple(codes)
    if not observed:
        return "OK"
    resolved = [classifications.get(code) for code in observed]
    if any(value is None or value == "UNKNOWN" for value in resolved):
        return "UNCLASSIFIED"
    if any(value == "A" for value in resolved):
        return "A"
    return "B"


def fixture_class(fixture_id: str) -> FixtureClass:
    if fixture_id == RF016:
        return "RF016"
    if fixture_id in STABLE_FIXTURES:
        return "STABLE"
    if fixture_id in CONTESTED_FIXTURES:
        return "CONTESTED"
    raise FixtureError(f"fixture has no conformance policy: {fixture_id}")


def _guard_codes(observation: ReviewAttemptObservation) -> list[str]:
    return [problem.code.value for problem in observation.guard_problems]


def _observation_dict(observation: ReviewAttemptObservation) -> dict[str, Any]:
    ledger = [
        {
            key: entry[key]
            for key in ("read_id", "normalized_path", "offset", "returned_lines")
            if key in entry
        }
        for entry in observation.ledger
    ]
    return {
        "stage": observation.stage,
        "attempt": observation.attempt,
        "artifact": observation.artifact,
        "evaluated_artifact": observation.evaluated_artifact,
        "guard_problems": [
            {"code": problem.code.value, "detail": problem.detail}
            for problem in observation.guard_problems
        ],
        "ledger": ledger,
        "error_type": observation.error_type,
        "error_message": observation.error_message,
    }


def _first_pass_classification(
    observations: list[ReviewAttemptObservation], classifications: dict[str, str]
) -> Classification:
    first = next((item for item in observations if item.stage == "INSPECTION"), None)
    if first is None:
        return "ERROR"
    if first.error_type:
        return "B"
    return classify_guard_codes(_guard_codes(first), classifications)


def _eventual_classification(
    observations: list[ReviewAttemptObservation],
    classifications: dict[str, str],
    error: BaseException | None,
) -> Classification:
    final = next(
        (item for item in reversed(observations) if item.stage == "FINALIZATION"),
        None,
    )
    if final is not None:
        return classify_guard_codes(_guard_codes(final), classifications)
    rejected = [
        item
        for item in observations
        if item.stage == "INSPECTION" and item.guard_problems
    ]
    if isinstance(error, ReviewFinalizationError) and rejected:
        return classify_guard_codes(_guard_codes(rejected[-1]), classifications)
    return "ERROR"


def _fixture_success(
    kind: FixtureClass,
    result: ExecutionReviewResult | None,
    expected_verdict: str | None,
) -> bool:
    if result is None:
        return False
    if kind == "STABLE":
        return result.verdict == expected_verdict
    return True


def evaluate_runs(
    *,
    fixture_id: str,
    model: str,
    runs: int,
    expected_verdict: str | None,
    classifications: dict[str, str],
    run_once: RunOnce,
    run_workers: int = 1,
) -> dict[str, Any]:
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if run_workers < 1:
        raise ValueError("run_workers must be at least 1")
    kind = fixture_class(fixture_id)
    occurrence_histogram: Counter[str] = Counter()
    run_histogram: Counter[str] = Counter()

    def measure(index: int) -> tuple[dict[str, Any], list[str]]:
        observations: list[ReviewAttemptObservation] = []
        result: ExecutionReviewResult | None = None
        error: BaseException | None = None
        try:
            result = run_once(observations.append)
        except Exception as exc:  # each failed invocation is measurement, not a retry
            error = exc

        observed_codes = [
            code for observation in observations for code in _guard_codes(observation)
        ]
        per_run_codes = set(observed_codes)

        operational = is_operational_error(error)
        if operational:
            first_pass = eventual = "OPERATIONAL"
        else:
            first_pass = _first_pass_classification(observations, classifications)
            eventual = _eventual_classification(observations, classifications, error)
        success = _fixture_success(kind, result, expected_verdict)
        final = next(
            (item for item in reversed(observations) if item.stage == "FINALIZATION"),
            None,
        )
        raw_verdict = (
            final.artifact.get("finalizer", {}).get("verdict") if final else None
        )
        return (
            {
                "run": index,
                "operational": operational,
                "first_pass": first_pass,
                "eventual": eventual,
                "eventual_success": success,
                "result_verdict": result.verdict if result else None,
                "raw_model_verdict": raw_verdict,
                "guard_veto": bool(
                    final
                    and raw_verdict == "ACCEPT"
                    and result is not None
                    and result.verdict == "BLOCKED"
                    and final.guard_problems
                ),
                "guard_codes": sorted(per_run_codes),
                "exception": (
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                        "diagnostic": getattr(error, "diagnostic", None),
                    }
                    if error
                    else None
                ),
                "observations": [_observation_dict(item) for item in observations],
            },
            observed_codes,
        )

    if run_workers == 1:
        measurements = [measure(index) for index in range(1, runs + 1)]
    else:
        with ThreadPoolExecutor(max_workers=min(run_workers, runs)) as executor:
            measurements = list(executor.map(measure, range(1, runs + 1)))
    records = [record for record, _codes in measurements]
    for _record, observed_codes in measurements:
        occurrence_histogram.update(observed_codes)
        run_histogram.update(set(observed_codes))

    first_counts = Counter(item["first_pass"] for item in records)
    eventual_counts = Counter(item["eventual"] for item in records)
    eventual_ok = sum(bool(item["eventual_success"]) for item in records)
    all_classifications = [
        value for item in records for value in (item["first_pass"], item["eventual"])
    ]
    # Rates are measured over the effective sample: runs that actually reached
    # the reviewer. An operational fault contributes no evidence either way.
    operational_runs = sum(bool(item["operational"]) for item in records)
    effective_runs = runs - operational_runs
    first_rate = first_counts["OK"] / effective_runs if effective_runs else 0.0
    eventual_rate = eventual_ok / effective_runs if effective_runs else 0.0
    class_a_codes = {
        code for code in occurrence_histogram if classifications.get(code) == "A"
    }
    unclassified_codes = {
        code
        for code in occurrence_histogram
        if classifications.get(code) in {None, "UNKNOWN"}
    }
    class_a_runs = sum(
        bool(set(item["guard_codes"]) & class_a_codes) for item in records
    )
    unclassified_runs = sum(
        bool(set(item["guard_codes"]) & unclassified_codes) for item in records
    )
    failures = []
    if first_rate < 0.95:
        failures.append(f"first-pass {first_rate:.3f} < 0.950")
    if eventual_rate != 1.0:
        failures.append(f"bounded-eventual {eventual_rate:.3f} != 1.000")
    if class_a_codes:
        failures.append("Class A guard rejection observed")
    if unclassified_codes:
        failures.append("unclassified guard rejection observed")
    if "ERROR" in all_classifications:
        failures.append("unclassified operational error observed")

    return {
        "fixture": fixture_id,
        "fixture_class": kind,
        "model": model,
        "runs": runs,
        "expected_verdict": expected_verdict,
        "first_pass": {
            "counts": dict(sorted(first_counts.items())),
            "rate": first_rate,
        },
        "eventual": {
            "counts": dict(sorted(eventual_counts.items())),
            "ok": eventual_ok,
            "rate": eventual_rate,
        },
        "guard_histogram": dict(sorted(occurrence_histogram.items())),
        "guard_run_histogram": dict(sorted(run_histogram.items())),
        "class_a": {
            "codes": sorted(class_a_codes),
            "occurrences": sum(occurrence_histogram[code] for code in class_a_codes),
            "runs": class_a_runs,
            "rate": class_a_runs / runs,
        },
        "unclassified": {
            "codes": sorted(unclassified_codes),
            "occurrences": sum(
                occurrence_histogram[code] for code in unclassified_codes
            ),
            "runs": unclassified_runs,
        },
        "operational": {
            "runs": operational_runs,
            "effective_runs": effective_runs,
            "requested_runs": runs,
        },
        # A reduced sample is not the measurement that was requested. Reporting
        # PASS or FAIL on it would either certify on a biased sample or blame
        # the reviewer for an outage, so an incomplete run is neither.
        "verdict": (
            f"INCOMPLETE: {operational_runs}/{runs} runs failed operationally; "
            f"effective sample {effective_runs} < requested {runs}"
            if operational_runs
            else ("PASS" if not failures else "FAIL: " + "; ".join(failures))
        ),
        "results": records,
    }


def _extract_worktree(archive: Path, destination: Path) -> None:
    tar_path = destination / "worktree.tar"
    with tar_path.open("wb") as output:
        subprocess.run(
            ["zstd", "-q", "-d", "-c", str(archive)],
            stdout=output,
            check=True,
        )
    subprocess.run(["tar", "-C", str(destination), "-xf", str(tar_path)], check=True)
    tar_path.unlink()


def run_fixture(
    path: Path,
    *,
    model: str,
    runs: int,
    classifications: dict[str, str],
    run_workers: int = 1,
) -> dict[str, Any]:
    fixture = load_fixture(path)
    derived_contract = review_requirement_contract(fixture["evidence"])
    if derived_contract != fixture["contract"]:
        raise FixtureError(f"contract drift for {path.name}")
    fixture_id = str(fixture["fixture"]["fixture_id"])
    expected_verdict = fixture["expected"].get("verdict")
    kind = fixture_class(fixture_id)
    if kind == "STABLE" and expected_verdict != "NEEDS_FIXES":
        raise FixtureError(f"STABLE fixture has invalid expectation: {fixture_id}")

    with tempfile.TemporaryDirectory(prefix="sweforge-conformance-") as temp:
        worktree = Path(temp)
        _extract_worktree(fixture["worktree_archive"], worktree)
        authority = fixture["context"].get("repo_context")

        def invoke(observer):
            context = ReviewerContext(
                worktree=str(worktree),
                repo_context=RepoAgentContext(**authority) if authority else None,
                live_input_provider=None,
                live_delivered_event_keys=set(),
            )
            return review_execution(
                context=context,
                model=model,
                evidence=fixture["evidence"],
                attempt_observer=observer,
            )

        report = evaluate_runs(
            fixture_id=fixture_id,
            model=model,
            runs=runs,
            expected_verdict=expected_verdict,
            classifications=classifications,
            run_once=invoke,
            run_workers=run_workers,
        )
    report["execution_evidence_ids"] = sorted(
        str(item["evidence_id"])
        for item in fixture["evidence"].get("execution_observations", [])
        if item.get("evidence_id")
    )
    return report


def replay_fixture_offline(path: Path, *, runs: int) -> dict[str, Any]:
    """Validate and inventory stored fixture outcomes without claiming conformance."""
    fixture = load_fixture(path)
    fixture_id = str(fixture["fixture"]["fixture_id"])
    kind = fixture_class(fixture_id)
    if review_requirement_contract(fixture["evidence"]) != fixture["contract"]:
        raise FixtureError(f"contract drift for {fixture_id}")
    stored = fixture["outcome"]
    expected_verdict = fixture["expected"].get("verdict")
    if kind == "STABLE" and (
        stored is None or stored.get("verdict") != expected_verdict
    ):
        raise FixtureError(f"STABLE stored verdict mismatch: {fixture_id}")
    if kind == "CONTESTED" and (stored is None or stored.get("verdict") != "BLOCKED"):
        raise FixtureError(f"CONTESTED stored verdict mismatch: {fixture_id}")
    if kind == "RF016" and stored is not None:
        raise FixtureError("RF-016 unexpectedly has a stored outcome")
    guard_codes = fixture["diagnostics"].get("guard_codes", [])
    return {
        "fixture": fixture_id,
        "fixture_class": kind,
        "runs": runs,
        "stored_verdict": stored.get("verdict") if stored else None,
        "stored_failure": fixture["diagnostics"] if stored is None else None,
        "expected_verdict": expected_verdict,
        "first_pass": {"counts": {"UNMEASURED": runs}, "rate": None},
        "eventual": {"counts": {"UNMEASURED": runs}, "ok": None, "rate": None},
        "guard_histogram": dict(sorted(Counter(guard_codes).items())),
        "guard_run_histogram": {code: runs for code in sorted(set(guard_codes))},
        "class_a": {"codes": [], "occurrences": 0, "runs": 0, "rate": 0.0},
        "unclassified": {"codes": [], "occurrences": 0, "runs": 0},
        "verdict": "OFFLINE_BASELINE_VALID",
        "results": [],
    }


def _problem_dicts(problems) -> list[dict[str, str]]:
    return [
        {"code": problem.code.value, "detail": problem.detail} for problem in problems
    ]


def _ledger_with_excerpts(ledger: list[dict], worktree: Path) -> list[dict]:
    restored = []
    for item in ledger:
        current = dict(item)
        path = str(current.get("normalized_path", ""))
        returned_lines = current.get("returned_lines", [])
        if path and isinstance(returned_lines, list) and len(returned_lines) == 2:
            target = (worktree / path).resolve()
            if target.is_relative_to(worktree.resolve()) and target.is_file():
                lines = target.read_text(errors="replace").splitlines(keepends=True)
                start, end = (int(value) for value in returned_lines)
                current["excerpt"] = "".join(lines[start - 1 : end])
        restored.append(current)
    return restored


def replay_recorded_run(
    recorded: dict[str, Any],
    *,
    fixture_id: str,
    expected_verdict: str | None,
    evidence: dict[str, Any],
    contract: list[dict[str, str]],
    classifications: dict[str, str],
    worktree: Path,
) -> dict[str, Any]:
    """Re-adjudicate saved model artifacts without making a provider call."""
    kind = fixture_class(fixture_id)
    replayed_observations: list[dict[str, Any]] = []
    accepted_inspection: InspectionReport | None = None
    accepted_ledger: list[dict] = []
    observed_codes: list[str] = []
    first_codes: list[str] | None = None
    last_rejected_codes: list[str] = []

    for observation in recorded.get("observations", []):
        if observation.get("stage") != "INSPECTION":
            continue
        error_type = observation.get("error_type")
        problems = []
        evaluated: dict[str, Any] = {}
        parsed: InspectionReport | None = None
        ledger = _ledger_with_excerpts(list(observation.get("ledger", [])), worktree)
        if not error_type:
            try:
                parsed = InspectionReport.model_validate(
                    observation.get("artifact", {})
                )
                parsed = _canonical_inspection_provenance(
                    parsed, evidence=evidence, ledger=ledger
                )
                resolved = _resolved_evidence(evidence, parsed, ledger)
                parsed = _fail_closed_unavailable_inspections(
                    parsed, set(resolved.unavailable_requirement_ids)
                )
                problems = _inspection_artifact_problems(
                    contract, parsed, ledger=ledger, evidence=evidence
                )
                evaluated = parsed.model_dump(mode="json")
            except Exception as exc:
                error_type = type(exc).__name__
                evaluated = {"error": str(exc)}
        codes = [problem.code.value for problem in problems]
        if first_codes is None:
            first_codes = codes
        observed_codes.extend(codes)
        if codes:
            last_rejected_codes = codes
        if (
            accepted_inspection is None
            and parsed is not None
            and not problems
            and not error_type
        ):
            accepted_inspection = parsed
            accepted_ledger = ledger
        replayed_observations.append(
            {
                "stage": "INSPECTION",
                "attempt": observation.get("attempt"),
                "guard_problems": _problem_dicts(problems),
                "evaluated_artifact": evaluated,
                "error_type": error_type,
            }
        )

    first_pass = (
        "B"
        if not replayed_observations or replayed_observations[0]["error_type"]
        else classify_guard_codes(first_codes or [], classifications)
    )
    current_result: ExecutionReviewResult | None = None
    raw_verdict: str | None = None
    final_codes: list[str] = []
    final_error: str | None = None
    final_observation = next(
        (
            item
            for item in reversed(recorded.get("observations", []))
            if item.get("stage") == "FINALIZATION"
        ),
        None,
    )
    if accepted_inspection is not None and final_observation is not None:
        try:
            artifact = final_observation.get("artifact", {})
            raw = ExecutionReviewResult.model_validate(artifact.get("finalizer", {}))
            raw_verdict = raw.verdict
            parsed_result = _canonical_finalizer_provenance(raw, evidence=evidence)
            parsed_result = _guard_repairability(parsed_result)
            semantic_raw = artifact.get("semantic_review")
            semantic = (
                SemanticReviewArtifact.model_validate(semantic_raw)
                if semantic_raw is not None
                else None
            )
            final_problems = _accept_coverage_problems(
                parsed_result,
                contract,
                inspection=accepted_inspection,
                semantic_review=semantic,
                ledger=accepted_ledger,
                evidence=evidence,
            )
            final_codes = [problem.code.value for problem in final_problems]
            observed_codes.extend(final_codes)
            current_result = _apply_accept_coverage_guard(parsed_result, final_problems)
            replayed_observations.append(
                {
                    "stage": "FINALIZATION",
                    "attempt": final_observation.get("attempt"),
                    "guard_problems": _problem_dicts(final_problems),
                    "evaluated_artifact": current_result.model_dump(mode="json"),
                    "error_type": None,
                }
            )
        except Exception as exc:
            final_error = type(exc).__name__
            replayed_observations.append(
                {
                    "stage": "FINALIZATION",
                    "attempt": final_observation.get("attempt"),
                    "guard_problems": [],
                    "evaluated_artifact": {"error": str(exc)},
                    "error_type": final_error,
                }
            )

    if current_result is not None:
        eventual = classify_guard_codes(final_codes, classifications)
    elif last_rejected_codes:
        eventual = classify_guard_codes(last_rejected_codes, classifications)
    else:
        eventual = "ERROR"
    current_guard_codes = sorted(set(observed_codes))
    current = {
        "first_pass": first_pass,
        "eventual": eventual,
        "eventual_success": _fixture_success(kind, current_result, expected_verdict),
        "result_verdict": current_result.verdict if current_result else None,
        "raw_model_verdict": raw_verdict,
        "guard_veto": bool(
            raw_verdict == "ACCEPT"
            and current_result is not None
            and current_result.verdict == "BLOCKED"
            and final_codes
        ),
        "guard_codes": current_guard_codes,
        "exception_type": final_error,
    }
    previous = {
        key: recorded.get(key)
        for key in (
            "first_pass",
            "eventual",
            "eventual_success",
            "result_verdict",
            "raw_model_verdict",
            "guard_veto",
            "guard_codes",
        )
    }
    changed_fields = sorted(
        key for key in previous if previous[key] != current.get(key)
    )
    return {
        "run": recorded.get("run"),
        "recorded": previous,
        "replayed": current,
        "changed_fields": changed_fields,
        "outcome_changed": bool(changed_fields),
        "verdict_changed": previous["result_verdict"] != current["result_verdict"],
        "observations": replayed_observations,
    }


def replay_saved_report(
    paths: list[Path],
    *,
    source_report: Path,
    classifications: dict[str, str],
) -> dict[str, Any]:
    """Replay a paid report as a deterministic cassette corpus."""
    source = json.loads(source_report.read_text())
    fixtures_by_id = {}
    for path in paths:
        fixture = load_fixture(path)
        fixture_id = str(fixture["fixture"]["fixture_id"])
        fixtures_by_id[fixture_id] = fixture
    replayed_fixtures = []
    for recorded_fixture in source.get("fixtures", []):
        fixture_id = str(recorded_fixture.get("fixture", ""))
        fixture = fixtures_by_id.get(fixture_id)
        if fixture is None:
            raise FixtureError(f"source report fixture is unavailable: {fixture_id}")
        contract = review_requirement_contract(fixture["evidence"])
        if contract != fixture["contract"]:
            raise FixtureError(f"contract drift for {fixture_id}")
        expected_verdict = fixture["expected"].get("verdict")
        with tempfile.TemporaryDirectory(prefix="sweforge-report-replay-") as temp:
            worktree = Path(temp)
            _extract_worktree(fixture["worktree_archive"], worktree)
            results = [
                replay_recorded_run(
                    item,
                    fixture_id=fixture_id,
                    expected_verdict=expected_verdict,
                    evidence=fixture["evidence"],
                    contract=contract,
                    classifications=classifications,
                    worktree=worktree,
                )
                for item in recorded_fixture.get("results", [])
            ]
        replayed_fixtures.append(
            {
                "fixture": fixture_id,
                "fixture_class": fixture_class(fixture_id),
                "model": source.get("model"),
                "runs": len(results),
                "outcome_changes": sum(item["outcome_changed"] for item in results),
                "verdict_changes": sum(item["verdict_changed"] for item in results),
                "changed_runs": [
                    item["run"] for item in results if item["outcome_changed"]
                ],
                "verdict_changed_runs": [
                    item["run"] for item in results if item["verdict_changed"]
                ],
                "results": results,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": "report-replay",
        "source_report": str(source_report.resolve()),
        "source_generated_at": source.get("generated_at"),
        "model": source.get("model"),
        "runs_per_fixture": source.get("runs_per_fixture"),
        "replay_model_calls": 0,
        "caveat": REPLAY_CAVEAT,
        "fixtures": replayed_fixtures,
        "outcome_changes": sum(item["outcome_changes"] for item in replayed_fixtures),
        "verdict_changes": sum(item["verdict_changes"] for item in replayed_fixtures),
        "verdict": "REPLAY_COMPLETE",
    }


def discover_fixtures(paths: Iterable[Path]) -> list[Path]:
    discovered: dict[str, Path] = {}
    for path in paths:
        if (path / "fixture.json").is_file():
            discovered[path.name] = path
            continue
        if not path.is_dir():
            raise FixtureError(f"fixture path does not exist: {path}")
        for child in path.iterdir():
            if child.is_dir() and (child / "fixture.json").is_file():
                discovered[child.name] = child
    unknown = sorted(set(discovered) - KNOWN_FIXTURES)
    if unknown:
        raise FixtureError("unknown fixtures: " + ", ".join(unknown))
    return [discovered[name] for name in sorted(discovered)]


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("mode") == "report-replay":
        return {
            "mode": report["mode"],
            "source_report": report["source_report"],
            "model": report["model"],
            "runs_per_fixture": report["runs_per_fixture"],
            "replay_model_calls": report["replay_model_calls"],
            "caveat": report["caveat"],
            "outcome_changes": report["outcome_changes"],
            "verdict_changes": report["verdict_changes"],
            "verdict": report["verdict"],
            "fixtures": [
                {
                    key: fixture[key]
                    for key in (
                        "fixture",
                        "fixture_class",
                        "runs",
                        "outcome_changes",
                        "verdict_changes",
                        "changed_runs",
                        "verdict_changed_runs",
                    )
                }
                for fixture in report["fixtures"]
            ],
        }
    return {
        "model": report["model"],
        "runs_per_fixture": report["runs_per_fixture"],
        "verdict": report["verdict"],
        "fixtures": [
            {
                key: fixture[key]
                for key in (
                    "fixture",
                    "fixture_class",
                    "first_pass",
                    "eventual",
                    "guard_histogram",
                    "guard_run_histogram",
                    "class_a",
                    "unclassified",
                    "verdict",
                )
            }
            for fixture in report["fixtures"]
        ],
    }


def run_live_fixtures(
    paths: list[Path],
    *,
    model: str,
    runs: int,
    classifications: dict[str, str],
    workers: int,
    run_workers: int = 1,
) -> list[dict[str, Any]]:
    """Run independent fixture jobs concurrently, preserving input order."""
    if workers < 1:
        raise ValueError("workers must be at least 1")

    def run(path: Path) -> dict[str, Any]:
        return run_fixture(
            path,
            model=model,
            runs=runs,
            classifications=classifications,
            run_workers=run_workers,
        )

    with ThreadPoolExecutor(max_workers=min(workers, len(paths))) as executor:
        return list(executor.map(run, paths))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path, nargs="+")
    parser.add_argument("--model")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--replay-report",
        type=Path,
        help="re-adjudicate all saved artifacts in a prior conformance report",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--run-workers", type=int, default=1)
    parser.add_argument("--classifications", type=Path, default=DEFAULT_CLASSIFICATIONS)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    if args.offline and args.replay_report is not None:
        parser.error("--offline and --replay-report are mutually exclusive")
    if args.replay_report is not None and args.model:
        parser.error("--replay-report never accepts --model or makes model calls")
    if not args.offline and args.replay_report is None and not args.model:
        parser.error("--model is required unless --offline is selected")

    paths = discover_fixtures(args.fixtures)
    if args.replay_report is not None:
        classifications = load_guard_classifications(args.classifications)
        report = replay_saved_report(
            paths,
            source_report=args.replay_report,
            classifications=classifications,
        )
    elif args.offline:
        fixtures = [replay_fixture_offline(path, runs=args.runs) for path in paths]
        model = "offline-stored-outcome"
        overall_verdict = "OFFLINE_BASELINE_VALID"
    else:
        classifications = load_guard_classifications(args.classifications)
        fixtures = run_live_fixtures(
            paths,
            model=args.model,
            runs=args.runs,
            classifications=classifications,
            workers=args.workers,
            run_workers=args.run_workers,
        )
        model = args.model
        # An incomplete fixture outranks a failing one: if any fixture could not
        # be measured, the batch did not produce the requested measurement and
        # must not be reported as a reviewer FAIL.
        incomplete = [
            item for item in fixtures if item["verdict"].startswith("INCOMPLETE")
        ]
        if incomplete:
            overall_verdict = (
                f"INCOMPLETE: {len(incomplete)}/{len(fixtures)} fixtures "
                "failed operationally; no conformance conclusion available"
            )
        elif fixtures and all(item["verdict"] == "PASS" for item in fixtures):
            overall_verdict = "PASS"
        else:
            overall_verdict = "FAIL"
    if args.replay_report is None:
        report = {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "model": model,
            "runs_per_fixture": args.runs,
            "workers": args.workers,
            "run_workers": args.run_workers,
            "fixtures": fixtures,
            "verdict": overall_verdict,
        }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(_summary(report), indent=2, sort_keys=True))
    return (
        0
        if report["verdict"] in {"PASS", "OFFLINE_BASELINE_VALID", "REPLAY_COMPLETE"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
