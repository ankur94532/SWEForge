"""Offline review fixture freeze and replay commands."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .context import RepoAgentContext
from .github_store import SQLiteGitHubStore
from .review_fixture import (
    build_review_evidence,
    ledger_from_sources,
    load_fixture,
    parse_dispatcher_failure,
    write_fixture,
)
from .reviewer import ReviewerContext, review_execution, review_requirement_contract


def freeze_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze durable review evidence")
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--cycle-id", type=int, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-id", required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="sweforge-freeze-") as temp:
        local_db = Path(temp) / "state.db"
        shutil.copyfile(args.state_db, local_db)
        local_db.chmod(0o600)
        store = SQLiteGitHubStore(local_db)
        evidence, context, provenance = build_review_evidence(
            store,
            thread_id=args.thread_id,
            cycle_id=args.cycle_id,
            workspace_path=args.workspace,
        )
        failure = store.connection.execute(
            "SELECT failure_count, last_error FROM dispatcher_failures "
            "WHERE thread_id = ?",
            (args.thread_id,),
        ).fetchone()
        diagnostics = parse_dispatcher_failure(
            failure["last_error"] if failure else None
        )
        if failure:
            diagnostics["failure_count"] = failure["failure_count"]
    write_fixture(
        args.output,
        fixture_id=args.fixture_id,
        evidence=evidence,
        context=context,
        provenance=provenance,
        diagnostics=diagnostics,
        ledger=ledger_from_sources(diagnostics=diagnostics),
        capture_reason="backfill",
        source={
            "source_archive": str(args.state_db.parent),
            "source_database": str(args.state_db),
        },
        expected={},
    )
    return 0


def replay_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a review fixture offline")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--model")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="replay the stored outcome without loading a model provider",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.offline and not args.model:
        parser.error("--model is required unless --offline is selected")
    fixture = load_fixture(args.fixture)
    evidence = fixture["evidence"]
    contract = review_requirement_contract(evidence)
    stored_contract = fixture["contract"]
    if contract != stored_contract:
        raise SystemExit("fixture contract no longer matches current derivation")
    results = []
    if args.offline:
        outcome = fixture["outcome"]
        diagnostics = fixture["diagnostics"]
        for _ in range(args.runs):
            if outcome:
                results.append({"ok": True, "verdict": outcome["verdict"]})
            else:
                results.append(
                    {
                        "ok": False,
                        "exception": diagnostics.get(
                            "exception_type", "ReviewFinalizationError"
                        ),
                        "message": diagnostics.get("message", "stored failure"),
                    }
                )
        report = {
            "fixture": fixture["fixture"]["fixture_id"],
            "runs": args.runs,
            "offline": True,
            "results": results,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if all(item["ok"] for item in results) else 1
    for _ in range(args.runs):
        with tempfile.TemporaryDirectory(prefix="sweforge-review-replay-") as temp:
            archive = Path(temp) / "worktree.tar"
            with archive.open("wb") as output:
                subprocess.run(
                    ["zstd", "-q", "-d", "-c", str(fixture["worktree_archive"])],
                    stdout=output,
                    check=True,
                )
            subprocess.run(["tar", "-C", temp, "-xf", archive], check=True)
            authority = fixture["context"].get("repo_context")
            context = ReviewerContext(
                worktree=temp,
                repo_context=(RepoAgentContext(**authority) if authority else None),
                live_input_provider=None,
                live_delivered_event_keys=set(),
            )
            try:
                result = review_execution(
                    context=context, model=args.model, evidence=evidence
                )
                results.append({"ok": True, "verdict": result.verdict})
            except Exception as exc:
                results.append(
                    {"ok": False, "exception": type(exc).__name__, "message": str(exc)}
                )
    report = {
        "fixture": fixture["fixture"]["fixture_id"],
        "runs": args.runs,
        "results": results,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(item["ok"] for item in results) else 1
