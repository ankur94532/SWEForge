"""Bounded rendering of authoritative accepted workflow lifecycle material."""

from __future__ import annotations

from collections.abc import Iterable

from .github_store import AcceptedTaskLifecycle

MAX_ACCEPTED_HISTORY_CHARS = 28_000
MAX_PLAN_CHARS = 3_000
MAX_EXECUTION_CHARS = 2_000
MAX_VALIDATION_CHARS = 2_000


def _bounded(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)].rstrip() + "\n[deterministically truncated]"


def _cycle_label(item: AcceptedTaskLifecycle) -> str:
    if item.cycle_kind == "INITIAL":
        return "Initial workflow"
    return f"Accepted revision #{item.revision_sequence}"


def _join_bounded(blocks: list[str], *, max_chars: int) -> str:
    if not blocks:
        return ""
    joined = "\n\n".join(blocks)
    if len(joined) <= max_chars:
        return joined
    marker = "[older accepted lifecycle summaries omitted]"
    selected = {0}
    used = len(blocks[0]) + len(marker) + 4
    for index in range(len(blocks) - 1, 0, -1):
        cost = len(blocks[index]) + 2
        if used + cost > max_chars:
            continue
        selected.add(index)
        used += cost
    ordered = [blocks[index] for index in sorted(selected)]
    if len(selected) < len(blocks):
        ordered.insert(1, marker)
    return "\n\n".join(ordered)[:max_chars]


def render_accepted_lifecycle(
    material: Iterable[AcceptedTaskLifecycle],
    *,
    max_chars: int = MAX_ACCEPTED_HISTORY_CHARS,
) -> str:
    """Render accepted facts in cycle/declaration order under a hard bound.

    A compact fallback retains identities for all feasible records and favors
    the newest accepted records when even the per-field rendering exceeds the
    total bound. The original issue request is rendered separately by callers.
    """
    items = list(material)
    if not items:
        return "(none)"

    blocks: list[str] = []
    prior_cycle: int | None = None
    for item in items:
        lines: list[str] = []
        if item.cycle_id != prior_cycle:
            lines.append(f"{_cycle_label(item)} (cycle {item.cycle_id}):")
            prior_cycle = item.cycle_id
        lines.extend(
            [
                f"  Task {item.task_id}",
                f"    Accepted plan [{item.plan_id}]: "
                + _bounded(item.plan_text, MAX_PLAN_CHARS),
                f"    Final execution [{item.execution_id}]: "
                + _bounded(item.execution_summary, MAX_EXECUTION_CHARS),
                f"    Final ACCEPT validation [{item.validation_id}]: "
                + _bounded(item.validation_summary, MAX_VALIDATION_CHARS),
            ]
        )
        blocks.append("\n".join(lines))
    rendered = "\n\n".join(blocks)
    if len(rendered) <= max_chars:
        return rendered

    compact = [
        "\n".join(
            [
                f"{_cycle_label(item)} (cycle {item.cycle_id}), task {item.task_id}",
                "  Plan: " + _bounded(item.plan_text, 500),
                "  Execution: " + _bounded(item.execution_summary, 700),
                "  ACCEPT validation: " + _bounded(item.validation_summary, 700),
            ]
        )
        for item in items
    ]
    joined = "\n\n".join(compact)
    if len(joined) <= max_chars:
        return joined

    # Preserve the first accepted identity, then retain as much recent context
    # as fits. Sorting selected indexes restores declaration/cycle order.
    selected = {0}
    used = len(compact[0])
    marker = "\n\n[older accepted lifecycle summaries omitted]\n\n"
    for index in range(len(compact) - 1, 0, -1):
        cost = len(compact[index]) + 2
        if used + cost + len(marker) > max_chars:
            continue
        selected.add(index)
        used += cost
    ordered = [compact[index] for index in sorted(selected)]
    if len(selected) < len(compact):
        ordered.insert(1, marker.strip())
    return "\n\n".join(ordered)[:max_chars]


def lifecycle_columns(
    material: Iterable[AcceptedTaskLifecycle],
) -> tuple[str, str, str]:
    """Render plan, execution and ACCEPT validation columns deterministically."""
    items = list(material)
    plans = [
        f"{_cycle_label(item)} / {item.task_id}: "
        f"{_bounded(item.plan_text, MAX_PLAN_CHARS)}"
        for item in items
    ]
    executions = [
        f"{_cycle_label(item)} / {item.task_id}: "
        f"{_bounded(item.execution_summary, MAX_EXECUTION_CHARS)}"
        for item in items
    ]
    validations = [
        f"{_cycle_label(item)} / {item.task_id}: ACCEPT — "
        f"{_bounded(item.validation_summary, MAX_VALIDATION_CHARS)}"
        for item in items
    ]
    return (
        _join_bounded(plans, max_chars=20_000),
        _join_bounded(executions, max_chars=20_000),
        _join_bounded(validations, max_chars=20_000),
    )
