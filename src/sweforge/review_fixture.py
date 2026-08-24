"""Versioned, provider-neutral review fixtures.

This module deliberately owns only review evidence.  It is safe to import from
the production workflow because capture is inert unless explicitly configured.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .context import RepoAgentContext
from .execution_evidence import sanitize
from .github_store import SQLiteGitHubStore
from .guard_codes import GuardCode
from .reviewer import (
    ReviewerContext,
    ReviewFinalizationError,
    review_requirement_contract,
)
from .workspace import Workspace

SCHEMA_VERSION = 1
FIXTURE_FILES = (
    "fixture.json",
    "provenance.json",
    "evidence.json",
    "contract.json",
    "context.json",
    "worktree.tar.zst",
    "outcome.json",
    "ledger.json",
    "diagnostics.json",
    "expected.json",
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:api[_-]?key|token|password|secret|authorization|private[_-]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_EXCLUDED = (".git/config", ".git/credentials", ".env", ".env.*", "*.pem", "id_*")


class FixtureError(ValueError):
    """A fixture is malformed or could not be safely published."""


class FixtureQuarantined(FixtureError):
    """A post-write secret scan found content that must not be published."""


# These phrases are the detail strings emitted at the corresponding guard sites
# in reviewer.py.  Matching the site text keeps diagnostics evidence-based and
# makes an unrecognised new guard visible instead of silently classifying it.
_GUARD_DETAIL_PREFIXES: tuple[tuple[str, GuardCode], ...] = (
    ("requirement coverage does not exactly match", GuardCode.RC_REQUIREMENT_COVERAGE),
    ("duplicate requirement IDs:", GuardCode.RC_DUPLICATE_REQUIREMENT),
    ("unexpected requirement IDs:", GuardCode.RC_UNEXPECTED_REQUIREMENT),
    ("non-satisfied requirement IDs:", GuardCode.RC_UNSATISFIED_REQUIREMENT),
    ("duplicate evidence cluster IDs:", GuardCode.SP_DUPLICATE_EVIDENCE_CLUSTER),
    ("evidence cluster hash mismatch:", GuardCode.SP_CLUSTER_HASH_MISMATCH),
    (
        "required specialist stage is incomplete:",
        GuardCode.SP_REQUIRED_STAGE_INCOMPLETE,
    ),
    (
        "inapplicable specialist stage was not skipped:",
        GuardCode.SP_INAPPLICABLE_STAGE_NOT_SKIPPED,
    ),
    (
        "specialist stage applicability mismatch:",
        GuardCode.SP_STAGE_APPLICABILITY_MISMATCH,
    ),
    ("invalid finding provenance:", GuardCode.SP_INVALID_FINDING_PROVENANCE),
    ("current blocking finding:", GuardCode.SP_CURRENT_BLOCKING_FINDING),
    ("duplicate finding IDs:", GuardCode.SP_DUPLICATE_FINDING),
    ("unknown associated finding IDs:", GuardCode.SP_UNKNOWN_ASSOCIATED_FINDING),
    ("duplicate finding association:", GuardCode.SP_DUPLICATE_ASSOCIATION),
    ("missing finding association:", GuardCode.SP_MISSING_ASSOCIATION),
    ("wrong-requirement evidence for", GuardCode.IA_WRONG_REQUIREMENT_REFERENCE),
    ("invalid read reference", GuardCode.IA_INVALID_READ_REFERENCE),
    ("untrusted diff path for", GuardCode.IA_UNTRUSTED_DIFF_PATH),
    ("missing execution source for", GuardCode.IA_MISSING_EXECUTION_SOURCE),
    ("unknown execution source for", GuardCode.IA_UNKNOWN_EXECUTION_SOURCE),
    ("invalid observation reference for", GuardCode.IA_INVALID_OBSERVATION_REFERENCE),
    (
        "observation has wrong requirement for",
        GuardCode.IA_WRONG_OBSERVATION_REQUIREMENT,
    ),
    ("observation path mismatch for", GuardCode.IA_OBSERVATION_PATH_MISMATCH),
    ("invalid evidence range for", GuardCode.IA_INVALID_EVIDENCE_RANGE),
    ("ungrounded ", GuardCode.IA_UNGROUNDED_OBSERVATION),
    (
        "missing direct code observation for",
        GuardCode.IA_MISSING_DIRECT_CODE_OBSERVATION,
    ),
    ("missing assertion or signal for", GuardCode.IA_MISSING_ASSERTION_OR_SIGNAL),
    (
        "missing direct execution evidence for",
        GuardCode.IA_MISSING_DIRECT_EXECUTION_EVIDENCE,
    ),
    ("missing structural evidence for", GuardCode.IA_MISSING_STRUCTURAL_EVIDENCE),
    ("unknown observation requirement:", GuardCode.II_UNKNOWN_OBSERVATION_REQUIREMENT),
    ("unknown inspection requirement:", GuardCode.II_UNKNOWN_INSPECTION_REQUIREMENT),
    ("duplicate ", GuardCode.FA_DUPLICATE_IDENTITY),
    ("inspection coverage does not exactly match", GuardCode.FA_INSPECTION_COVERAGE),
    ("challenge coverage does not exactly match", GuardCode.FA_CHALLENGE_COVERAGE),
    ("missing inspection for", GuardCode.FA_MISSING_INSPECTION),
    ("inspection is not VERIFIED for", GuardCode.FA_INSPECTION_NOT_VERIFIED),
    ("missing evidence for", GuardCode.FA_MISSING_EVIDENCE),
    ("missing challenge for", GuardCode.FA_MISSING_CHALLENGE),
    ("challenge is not SUPPORTED for", GuardCode.FA_CHALLENGE_NOT_SUPPORTED),
)


def _guard_code_for_problem(problem: str) -> GuardCode | None:
    for prefix, code in _GUARD_DETAIL_PREFIXES:
        if problem.startswith(prefix):
            return code
    return None


def parse_dispatcher_failure(last_error: str | None) -> dict:
    """Parse one durable dispatcher diagnostic without inventing evidence."""
    if not last_error:
        return {}
    prefix, separator, payload = last_error.partition("; diagnostic=")
    if not separator:
        return {"reason": "missing ; diagnostic= delimiter"}
    exception_type, separator, message = prefix.partition(": ")
    if not separator:
        return {"reason": "missing exception prefix delimiter"}
    try:
        diagnostic = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {"reason": f"unparseable diagnostic JSON: {exc.msg}"}
    if not isinstance(diagnostic, dict):
        return {"reason": "diagnostic payload is not an object"}
    problems = diagnostic.get("artifact_problems", [])
    if not isinstance(problems, list):
        return {"reason": "artifact_problems is not a list"}
    guard_codes = []
    for problem in problems:
        text = str(problem)
        code = _guard_code_for_problem(text)
        guard_codes.append(code.value if code else f"UNMAPPED:{text}")
    result = {
        "exception_type": exception_type,
        "message": message,
        "artifact_problems": problems,
        "attempt": diagnostic.get("attempt"),
        "reads": diagnostic.get("reads", []),
        "guard_codes": guard_codes,
    }
    return result


def structured_outcome(row: Any) -> dict | None:
    """Convert an execution_reviews row into ROADMAP §E.2's result shape."""
    if row is None:
        return None

    def parse_field(name: str, default: Any) -> Any:
        value = getattr(row, name, default)
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    return {
        "verdict": row.verdict,
        "summary": row.summary,
        "requirement_checks": parse_field("requirement_checks_json", []),
        "findings": parse_field("findings_json", []),
        "repair_instructions": parse_field("repair_instructions_json", []),
        "inspection_report": parse_field("inspection_json", {}),
        "challenge": parse_field("challenge_json", {}),
        "semantic_artifact": parse_field("challenge_json", {}),
    }


def ledger_from_sources(
    *, outcome: Any = None, diagnostics: dict | None = None
) -> list[dict]:
    if outcome is not None:
        raw = getattr(outcome, "read_ledger_json", "[]")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list) and parsed:
            return parsed
    reads = (diagnostics or {}).get("reads", [])
    return reads if isinstance(reads, list) else []


def record_capture_failure() -> None:
    global _CAPTURE_FAILURES
    _CAPTURE_FAILURES += 1


_CAPTURE_FAILURES = 0


def capture_failure_count() -> int:
    return _CAPTURE_FAILURES


def reset_capture_failure_count() -> None:
    global _CAPTURE_FAILURES
    _CAPTURE_FAILURES = 0


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _jsonable(value.value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def build_review_evidence(
    store: SQLiteGitHubStore,
    *,
    thread_id: str,
    cycle_id: int,
    workspace_path: str | Path | None = None,
    attempt_id: str | None = None,
) -> tuple[dict, ReviewerContext, dict]:
    """Build the exact evidence bundle used by the workflow and freeze CLI."""
    state = store.workflow_state(thread_id)
    if state is None or state.cycle_id != cycle_id:
        raise FixtureError("requested thread/cycle is not the current durable state")
    plan = store.plan_for_cycle(thread_id, cycle_id)
    execution = store.execution_for_cycle(
        thread_id=thread_id,
        cycle_id=cycle_id,
        root_event_key=state.root_event_key,
        root_input_id=state.root_input_id,
    )
    workspace = store.thread_workspace(thread_id)
    if plan is None or execution is None or workspace is None:
        raise FixtureError("review evidence is unavailable")
    attempt = (
        store.execution_attempt(attempt_id)
        if attempt_id
        else store.latest_attempt(thread_id, cycle_id)
    )
    if attempt is None:
        raise FixtureError("review attempt is unavailable")
    evidence = {
        "plan": {"id": plan.plan_id, "version": plan.version, "text": plan.plan_text},
        "execution": dict(execution),
        "attempt": _jsonable(attempt),
        "current_head": execution["end_head_sha"],
        "base_head": workspace.base_commit,
        "execution_observations": [
            _jsonable(record)
            for record in store.execution_tool_evidence_for_cycle(thread_id, cycle_id)
        ],
    }
    path = Path(workspace_path or workspace.workspace_path)
    inspection = Workspace(path, path, workspace.base_commit)
    evidence["changed_files"] = inspection.changed_files()[:500]
    evidence["diff"] = inspection.diff()[:60_000]
    evidence["dirty"] = not inspection.is_clean()
    source = store.source_event(state.root_event_key)
    evidence["source"] = dict(source) if source else {}
    if source:
        from .execution import normalize_task
        from .github_models import format_source_context

        evidence["source_request"] = format_source_context(
            source, normalize_task(source["body"])
        )
    if attempt.parent_review_id:
        previous = store.execution_review(attempt.parent_review_id)
        evidence["previous_review"] = _jsonable(previous) if previous else {}
    context = ReviewerContext(
        worktree=str(path),
        repo_context=RepoAgentContext(
            repo_id=state.repo_id,
            repo_full_name=state.repo_full_name,
            thread_id=state.thread_id,
        ),
        live_input_provider=None,
        live_delivered_event_keys=set(),
    )
    provenance = {
        "repo_full_name": state.repo_full_name,
        "repo_id": state.repo_id,
        "issue_number": workspace.issue_number,
        "thread_id": thread_id,
        "cycle_id": cycle_id,
        "root_event_key": state.root_event_key,
        "root_input_id": state.root_input_id,
        "execution_id": execution["execution_id"]
        if "execution_id" in execution.keys()
        else None,
        "attempt_id": attempt.attempt_id,
        "attempt_kind": attempt.kind.value,
        "review_iteration": attempt.attempt_number,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
    }
    return _jsonable(evidence), context, provenance


def _archive_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _scan_tree(source)
    excludes = [item for pattern in _EXCLUDED for item in ("--exclude", pattern)]
    command = ["tar", "-C", str(source), *excludes, "-cf", "-", "."]
    tar = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert tar.stdout is not None
    try:
        result = subprocess.run(
            ["zstd", "-q", "-o", str(destination)], stdin=tar.stdout
        )
    finally:
        tar.stdout.close()
        tar.wait()
    if result.returncode or tar.returncode:
        raise FixtureError("could not create worktree.tar.zst")


def _scan(path: Path) -> None:
    secret_values = tuple(
        value
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "SWEFORGE_GITHUB_TOKEN",
        )
        if (value := os.environ.get(name))
    )
    for item in path.rglob("*"):
        if item.is_file() and item.name != "worktree.tar.zst":
            text = item.read_text(errors="ignore")
            if _SECRET_TEXT.search(text) or any(
                value in text for value in secret_values
            ):
                raise FixtureQuarantined(f"secret detected in {item.name}")


def _scan_tree(path: Path) -> None:
    """Scan snapshot content before compression (compressed bytes are opaque)."""
    for item in path.rglob("*"):
        relative = item.relative_to(path).as_posix()
        if not item.is_file() or any(
            fnmatch.fnmatch(relative, pattern) for pattern in _EXCLUDED
        ):
            continue
        text = item.read_text(errors="ignore")
        if _SECRET_TEXT.search(text):
            raise FixtureQuarantined(f"secret detected in snapshot: {relative}")


def write_fixture(
    destination: str | Path,
    *,
    fixture_id: str,
    evidence: dict,
    context: ReviewerContext,
    provenance: dict,
    outcome: Any = None,
    error: BaseException | None = None,
    diagnostics: dict | None = None,
    expected: dict | None = None,
    captured_at: str | None = None,
    sweforge_git_sha: str | None = None,
    review_model: str | None = None,
    capture_reason: str = "review",
    source: dict | None = None,
    ledger: list[dict] | None = None,
) -> Path:
    """Write all ten schema files atomically; raise on unsafe content."""
    root = Path(destination) / fixture_id
    if root.exists():
        raise FixtureError(f"fixture already exists: {root}")
    Path(destination).mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{fixture_id}-", dir=Path(destination)))
    try:
        now = captured_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "fixture_id": fixture_id,
            "captured_at": now,
            "sweforge_git_sha": sweforge_git_sha or _git_sha(Path(context.worktree)),
            "review_model": review_model,
            "capture_reason": capture_reason,
        }
        files: dict[str, Any] = {
            "fixture.json": envelope,
            "provenance.json": provenance,
            "evidence.json": evidence,
            "contract.json": review_requirement_contract(evidence),
            "context.json": {
                "worktree": ".",
                "repo_context": _jsonable(context.repo_context),
                "memory_namespace": context.memory_namespace,
                "memory_snapshot": None,
            },
            "outcome.json": structured_outcome(outcome)
            if outcome is not None
            else None,
            "ledger.json": _jsonable(
                ledger_from_sources(outcome=outcome, diagnostics=diagnostics)
                if ledger is None
                else ledger
            ),
            "diagnostics.json": diagnostics
            if diagnostics is not None
            else _jsonable(error.diagnostic)
            if isinstance(error, ReviewFinalizationError)
            else (
                {
                    "exception_type": type(error).__name__,
                    "message": sanitize(str(error)),
                }
                if error
                else {}
            ),
            "expected.json": expected or {},
        }
        if source:
            files["provenance.json"] = {**provenance, **source}
        for name, value in files.items():
            (temporary / name).write_text(
                json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
            )
        _archive_snapshot(Path(context.worktree), temporary / "worktree.tar.zst")
        _scan(temporary)
        temporary.rename(root)
        return root
    except FixtureQuarantined:
        quarantine = root.with_name(root.name + ".quarantine")
        if quarantine.exists():
            shutil.rmtree(quarantine, ignore_errors=True)
        temporary.rename(quarantine)
        raise
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_fixture(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    if not (root / "fixture.json").is_file():
        raise FixtureError(f"not a fixture directory: {root}")
    envelope = json.loads((root / "fixture.json").read_text())
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError("unsupported fixture schema")
    missing = [name for name in FIXTURE_FILES if not (root / name).exists()]
    if missing:
        raise FixtureError(f"fixture is missing: {', '.join(missing)}")
    return {
        name.removesuffix(".json"): json.loads((root / name).read_text())
        for name in FIXTURE_FILES
        if name.endswith(".json")
    } | {"path": root, "worktree_archive": root / "worktree.tar.zst"}


def _git_sha(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"
