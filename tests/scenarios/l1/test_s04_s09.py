"""L1 scenarios S4-S9: the six acceptance probes.

At L1 the runner double calls the probe functions directly rather than going
through MCP transport — the transport is what the LIVE-GITHUB form of S4
proves. What these assert is SWEForge's reaction to each result class, and the
ProbeLedger records every call so "exactly N times" is checkable rather than
assumed.
"""

from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from acceptance.probes.ledger import LEDGER_ENV, ProbeLedger
from sweforge.github_store import WorkflowPhase

PLAN = "1. call the probe\n2. report the result"


def _probe_world(root_dir: Path) -> World:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    world.probe_ledger_path = root_dir / "probes.db"
    return world


def _with_probes(world: World):
    """Point the probe server at this world's ledger, isolated per scenario."""
    import os

    from acceptance.probes import server

    os.environ[LEDGER_ENV] = str(world.probe_ledger_path)
    server._connection = None
    return server


def _drive_to(world: World, thread_id: str, runner, until):
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    approval = world.event("2", "@agent approve", world.later())
    world.ingest(approval)
    world.engine.approve(event_key=approval.event_key)
    world.drive(
        thread_id,
        until=until,
        max_ticks=10,
        execute_kwargs={"runner": runner, "checkpointer": object()},
    )


def _start(world: World) -> str:
    world.ingest(world.event("1", "@agent use the probe", "2026-01-01T00:00:00Z"))
    return next(iter(world.thread_ids))


@scenario(
    "S4",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-NO-HOT-RETRY"],
    description="A retryable tool failure retries within one execution attempt.",
)
def s4_retryable_probe(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        # The agent retries the same logical operation; SWEForge must not turn
        # that into a second execution attempt.
        first = probes.retryable_probe(
            "s4-op", world.repo.repo_id, world.repo.full_name
        )
        assert first["ok"] is False and first["retryable"] is True
        second = probes.retryable_probe(
            "s4-op", world.repo.repo_id, world.repo.full_name
        )
        assert second["ok"] is True
        (Path(kwargs["worktree"]) / "README.md").write_text("probe ok\n")
        return "probe succeeded on retry"

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.AWAITING_PUBLICATION)
        ledger = ProbeLedger(world.probe_ledger_path)
        assert ledger.count("retryable_probe", "s4-op") == 2, "expected exactly 2 calls"
        assert world.store.latest_attempt(thread_id, 1).attempt_number == 1
    return world.observation()


@scenario(
    "S5",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-NO-HOT-RETRY"],
    description="A permanent tool failure is attempted once and not retried.",
)
def s5_nonretryable_probe(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        result = probes.nonretryable_probe(
            "s5-op", world.repo.repo_id, world.repo.full_name
        )
        assert result["retryable"] is False
        (Path(kwargs["worktree"]) / "README.md").write_text("fell back\n")
        return "permanent failure, used the documented fallback"

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.AWAITING_PUBLICATION)
        assert ProbeLedger(world.probe_ledger_path).count("nonretryable_probe") == 1
    return world.observation()


@scenario(
    "S6",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL"],
    description="A warning is neither success nor failure and does not stop execution.",
)
def s6_warning_probe(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        result = probes.warning_probe("s6-op", world.repo.repo_id, world.repo.full_name)
        assert result["severity"] == "warning" and result["continue_allowed"] is True
        (Path(kwargs["worktree"]) / "README.md").write_text("continued\n")
        return "advisory warning observed, work continued"

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.AWAITING_PUBLICATION)
        ledger = ProbeLedger(world.probe_ledger_path)
        assert ledger.count("warning_probe") == 1
        (call,) = ledger.calls("warning_probe")
        assert call.result_class == "warning", "warning collapsed into another class"
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "SUCCEEDED", "a warning must not fail the execution"
    return world.observation()


@scenario(
    "S7",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-NO-HOT-RETRY", "INV-NO-PUBLICATION"],
    description="A fatal tool failure is terminal: no retry, no review.",
)
def s7_fatal_probe(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        probes.fatal_probe("s7-op", world.repo.repo_id, world.repo.full_name)

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.EXECUTION_FAILED)
        assert ProbeLedger(world.probe_ledger_path).count("fatal_probe") == 1
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "FAILED"
        assert world.store.execution_review_for_attempt(attempt.attempt_id) is None, (
            "a fatally failed execution must not be reviewed"
        )
    return world.observation()


@scenario(
    "S8",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-NO-HOT-RETRY", "INV-NO-PUBLICATION"],
    description="A timeout is bounded and surfaced as a failure.",
)
def s8_timeout_probe(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        probes.timeout_probe("s8-op", world.repo.repo_id, world.repo.full_name)

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.EXECUTION_FAILED)
        ledger = ProbeLedger(world.probe_ledger_path)
        assert ledger.count("timeout_probe") == 1, "a timeout must not be retried"
        (call,) = ledger.calls("timeout_probe")
        assert call.result_class == "timeout_exception"
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "FAILED", "a timeout must surface as a failure"
        observation = world.observation()
        # The bound on a timeout is that it is spent once and not retried
        # hot. Recorded from the ledger the run actually produced.
        observation.record_bound(
            "S8_TIMEOUT", actual=ledger.count("timeout_probe"), expected=1
        )
    return observation


@scenario(
    "S9",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-REPO-ISOLATION"],
    description="Spoofed identity args are discarded; only trusted context is echoed.",
)
def s9_identity_spoof(root_dir) -> Observation:
    world = _probe_world(root_dir)
    probes = _with_probes(world)

    def runner(**kwargs):
        # The model attempts to supply identity and location it must not control.
        echoed = probes.identity_echo(
            "s9-op",
            world.repo.repo_id,
            world.repo.full_name,
            workspace_root="/etc",
            repo_path="/somewhere/else",
            tenant="other-tenant",
        )
        assert echoed == {
            "repo_id": world.repo.repo_id,
            "repo_full_name": world.repo.full_name,
        }, f"identity was not authoritative: {echoed}"
        (Path(kwargs["worktree"]) / "README.md").write_text("identity checked\n")
        return "identity echoed from trusted context"

    with world.activate():
        thread_id = _start(world)
        _drive_to(world, thread_id, runner, WorkflowPhase.AWAITING_PUBLICATION)
        (call,) = ProbeLedger(world.probe_ledger_path).calls("identity_echo")
        assert call.repo_id == world.repo.repo_id
        assert call.args_received["workspace_root"] == "/etc", (
            "the spoof attempt was not recorded, so nothing proves it was discarded"
        )
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S4", "S5", "S6", "S7", "S8", "S9"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
