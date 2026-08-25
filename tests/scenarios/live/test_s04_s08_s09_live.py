"""S4, S8 and S9 LIVE_GITHUB: tool-failure classes against the real API.

The probes stay local -- they are the controlled tool surface -- while the
issue, the events and the approval are real. What this layer adds over the L1
twins is that the trusted identity SWEForge echoes to a tool comes from a real
repository resolved over the API, not from a fake's constructor argument.
"""

from pathlib import Path

import pytest
from harness.live import LiveCredentialsUnavailable, live_repository, open_live_thread
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario

from acceptance.probes.ledger import LEDGER_ENV, ProbeLedger
from sweforge.github_store import WorkflowPhase

PLAN = "1. exercise the probe\n2. report the result"


def _live_probe_thread(scenario_id: str, root_dir, body: str):
    live = open_live_thread(
        scenario_id,
        root_dir,
        body=body,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    live.world.probe_ledger_path = Path(root_dir) / "probes.db"
    return live


def _probes(live):
    """Point the probe server at this run's ledger, isolated per scenario."""
    import os

    from acceptance.probes import server

    os.environ[LEDGER_ENV] = str(live.world.probe_ledger_path)
    server._connection = None
    return server


def _drive(live, runner, until):
    world = live.world
    world.drive(
        live.thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=10
    )
    live.approve()
    world.drive(
        live.thread_id,
        until=until,
        max_ticks=12,
        execute_kwargs={
            "lock_root": world.root / "locks",
            "runner": runner,
            "checkpointer": object(),
        },
    )


@scenario(
    "S4",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-NO-HOT-RETRY"],
    description="A live retryable tool failure retries inside one attempt.",
)
def s4_live_retryable_probe(root_dir) -> Observation:
    live = _live_probe_thread("S4", root_dir, "@agent use the retryable probe")
    probes = _probes(live)
    world = live.world

    def runner(**kwargs):
        first = probes.retryable_probe(
            "s4-op", world.repo.repo_id, world.repo.full_name
        )
        assert first["ok"] is False and first["retryable"] is True
        second = probes.retryable_probe(
            "s4-op", world.repo.repo_id, world.repo.full_name
        )
        assert second["ok"] is True
        (Path(kwargs["worktree"]) / "NOTES.md").write_text("probe ok\n")
        return "probe succeeded on retry"

    with world.activate():
        _drive(live, runner, WorkflowPhase.AWAITING_PUBLICATION)
        ledger = ProbeLedger(world.probe_ledger_path)
        assert ledger.count("retryable_probe", "s4-op") == 2, "expected exactly 2 calls"
        assert world.store.latest_attempt(live.thread_id, 1).attempt_number == 1
    return world.observation()


@scenario(
    "S8",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-NO-HOT-RETRY", "INV-NO-PUBLICATION"],
    description="A live timeout is bounded and surfaced as a failure.",
)
def s8_live_timeout_probe(root_dir) -> Observation:
    live = _live_probe_thread("S8", root_dir, "@agent use the timeout probe")
    probes = _probes(live)
    world = live.world

    def runner(**kwargs):
        probes.timeout_probe("s8-op", world.repo.repo_id, world.repo.full_name)

    with world.activate():
        _drive(live, runner, WorkflowPhase.EXECUTION_FAILED)
        ledger = ProbeLedger(world.probe_ledger_path)
        assert ledger.count("timeout_probe") == 1, "a timeout must not be retried"
        (call,) = ledger.calls("timeout_probe")
        assert call.result_class == "timeout_exception"
        attempt = world.store.latest_attempt(live.thread_id, 1)
        assert attempt.status == "FAILED", "a timeout must surface as a failure"
        observation = world.observation()
        observation.record_bound(
            "S8_TIMEOUT", actual=ledger.count("timeout_probe"), expected=1
        )
    return observation


@scenario(
    "S9",
    layer=Layer.LIVE_GITHUB,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-REPO-ISOLATION"],
    description="Live trusted identity overrides anything the model supplies.",
)
def s9_live_identity_spoof(root_dir) -> Observation:
    live = _live_probe_thread("S9", root_dir, "@agent use the identity probe")
    probes = _probes(live)
    world = live.world

    def runner(**kwargs):
        # The identity echoed back must be the one resolved from the real
        # repository over the API, never what the model supplied.
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
        (Path(kwargs["worktree"]) / "NOTES.md").write_text("identity checked\n")
        return "identity echoed from trusted context"

    with world.activate():
        _drive(live, runner, WorkflowPhase.AWAITING_PUBLICATION)
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S4", "S8", "S9"])
def test_registered_for_the_live_layer(scenario_id):
    from harness.scenario import SCENARIOS

    assert SCENARIOS[(scenario_id, Layer.LIVE_GITHUB)].layer is Layer.LIVE_GITHUB


@pytest.mark.live
@pytest.mark.parametrize("scenario_id", ["S4", "S8", "S9"])
def test_live(scenario_id, tmp_path):
    try:
        live_repository()
    except LiveCredentialsUnavailable as exc:
        pytest.skip(f"live target unavailable: {exc}")
    result = run(
        scenario_id, tmp_path / f"{scenario_id.lower()}-live", layer=Layer.LIVE_GITHUB
    )
    assert result.ok, "\n" + result.report()
