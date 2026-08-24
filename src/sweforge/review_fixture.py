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
            "sweforge_git_sha": "unknown",
            "review_model": None,
            "capture_reason": "review",
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
            "outcome.json": _jsonable(outcome) if outcome is not None else None,
            "ledger.json": _jsonable(
                getattr(outcome, "read_ledger", []) if outcome is not None else []
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
        for name, value in files.items():
            (temporary / name).write_text(
                json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
            )
        _archive_snapshot(Path(context.worktree), temporary / "worktree.tar.zst")
        _scan(temporary)
        temporary.rename(root)
        return root
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
