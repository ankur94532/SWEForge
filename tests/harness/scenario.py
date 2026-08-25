"""Scenario registry and per-invariant reporting.

A scenario declares which invariants it requires; the runner evaluates each one
and reports them individually. The point is attribution: a failure names the
invariant that broke, so "S16 failed" becomes "S16: INV-RETRY-BOUNDED failed,
retry_count=4, bound=3" without anyone reading a traceback.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from harness.invariants import REGISTRY, InvariantResult
from harness.observation import Observation


class Layer(StrEnum):
    L1 = "L1"
    L1_PROCESS = "L1_PROCESS"
    LIVE_PROCESS = "LIVE_PROCESS"
    LIVE_GITHUB = "LIVE_GITHUB"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One invariant's verdict inside one scenario run."""

    invariant_id: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return (
            f"  {self.invariant_id:<32} {'PASS' if self.ok else 'FAIL'}  {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    layer: Layer
    ok: bool
    checks: tuple[CheckOutcome, ...] = ()
    error: str | None = None

    def failures(self) -> tuple[CheckOutcome, ...]:
        return tuple(item for item in self.checks if not item.ok)

    def report(self) -> str:
        head = f"{self.scenario_id}  {'PASS' if self.ok else 'FAIL'}  [{self.layer}]"
        if self.error:
            return f"{head}\n  scenario body raised: {self.error}"
        return "\n".join([head, *(item.line() for item in self.checks)])


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    layer: Layer
    invariants: tuple[str, ...]
    faults: tuple[str, ...]
    body: Callable[..., Observation]
    description: str = ""


SCENARIOS: dict[str, Scenario] = {}


def scenario(
    scenario_id: str,
    *,
    layer: Layer,
    invariants: Sequence[str],
    faults: Sequence[str] = (),
    description: str = "",
) -> Callable[[Callable[..., Observation]], Scenario]:
    """Register one scenario. Unknown invariant ids are rejected at import."""

    def wrap(body: Callable[..., Observation]) -> Scenario:
        if scenario_id in SCENARIOS:
            raise ValueError(f"duplicate scenario id: {scenario_id}")
        unknown = [item for item in invariants if item not in REGISTRY]
        if unknown:
            raise ValueError(f"{scenario_id} declares unknown invariants: {unknown}")
        if not invariants:
            raise ValueError(
                f"{scenario_id} declares no invariants; a scenario that asserts "
                "nothing cannot fail and would report PASS unconditionally"
            )
        item = Scenario(
            scenario_id,
            layer,
            tuple(invariants),
            tuple(faults),
            body,
            description or (body.__doc__ or "").strip().splitlines()[0]
            if body.__doc__
            else "",
        )
        SCENARIOS[scenario_id] = item
        return item

    return wrap


def _drained(declared: Sequence[str], observation: Observation) -> InvariantResult:
    """A declared fault that never fired must not produce a silent PASS."""
    if not declared:
        return InvariantResult(True, "no faults declared")
    ledger = observation.faults
    if ledger is None:
        raise RuntimeError(
            "scenario declares faults but the Observation carries no fault "
            "ledger; without one an unfired fault would pass unnoticed"
        )
    fired = {getattr(item, "point", item) for item in ledger}
    missing = sorted(set(declared) - set(fired))
    if missing:
        return InvariantResult(False, f"declared faults never fired: {missing}")
    return InvariantResult(True, f"{len(declared)} declared fault(s) all fired")


def run(scenario_id: str, *args, **kwargs) -> ScenarioResult:
    """Execute one scenario body and evaluate every invariant it declared."""
    item = SCENARIOS.get(scenario_id)
    if item is None:
        raise KeyError(f"unknown scenario: {scenario_id}")
    try:
        observation = item.body(*args, **kwargs)
    except Exception as exc:  # the body failing is the scenario failing
        return ScenarioResult(
            scenario_id, item.layer, False, (), f"{type(exc).__name__}: {exc}"
        )
    checks: list[CheckOutcome] = []
    for invariant_id in item.invariants:
        try:
            result = REGISTRY[invariant_id].check(observation)
        except Exception as exc:
            # An invariant that cannot observe is a scenario failure, never a
            # pass: that distinction is the whole point of raising rather than
            # returning empty.
            checks.append(CheckOutcome(invariant_id, False, f"unevaluable: {exc}"))
            continue
        checks.append(CheckOutcome(invariant_id, result.ok, result.detail))
    drained = _drained(item.faults, observation)
    checks.append(CheckOutcome("FAULTS-DRAINED", drained.ok, drained.detail))
    return ScenarioResult(
        scenario_id, item.layer, all(item.ok for item in checks), tuple(checks)
    )


def campaign_status(results: Iterable[ScenarioResult]) -> dict:
    """Machine-checkable campaign state; the exit condition reads this."""
    ordered = sorted(results, key=lambda item: item.scenario_id)
    return {
        "scenarios": [
            {
                "id": item.scenario_id,
                "layer": str(item.layer),
                "ok": item.ok,
                "error": item.error,
                "checks": [
                    {"invariant": c.invariant_id, "ok": c.ok, "detail": c.detail}
                    for c in item.checks
                ],
            }
            for item in ordered
        ],
        "total": len(ordered),
        "passed": sum(1 for item in ordered if item.ok),
        "failed": [item.scenario_id for item in ordered if not item.ok],
    }


def write_campaign_status(path: str | Path, results: Iterable[ScenarioResult]) -> dict:
    status = campaign_status(results)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    return status
