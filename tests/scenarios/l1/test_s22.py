"""S22: repository memory and skills stay write-denied to the agent.

Memory and skills are application-owned. An execution agent may read them but
must never write them, so the only mutation path is SWEForge's own validated
one. This asserts the deny rules are actually constructed and handed to the
agent, and that the durable stores are unchanged after a run that tries.
"""

import inspect
from pathlib import Path

from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge import agent as agent_module
from sweforge.github_store import WorkflowPhase

PLAN = "1. edit README\n2. run the tests"
PROTECTED = ("/memories/**", "/skills/**")


def _tampering_runner(**kwargs):
    """Try to write where the agent is denied, then do the real work.

    The L1 runner is not the sandboxed agent, so these writes are attempted
    against the worktree rather than through Deep Agents' filesystem
    permissions. What this proves is that a run which *tries* leaves the
    durable memory and skills stores untouched; the permission rules
    themselves are asserted separately below.
    """
    worktree = Path(kwargs["worktree"])
    for relative in ("memories/AGENTS.md", "skills/build/SKILL.md"):
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("tampered\n")
    (worktree / "README.md").write_text("done\n")
    return "attempted to write protected namespaces"


@scenario(
    "S22",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL", "INV-NO-MEMORY-WRITTEN"],
    description="Protected namespaces stay denied and their stores unchanged.",
)
def s22_memory_and_skills_write_denial(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        world.engine.approve(event_key=approval.event_key)
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={"runner": _tampering_runner, "checkpointer": object()},
        )

        # No repository memory was admitted by a run that tried to write it.
        accepted = world.store.connection.execute(
            "SELECT COUNT(*) FROM repo_memory_candidates WHERE status='ACCEPTED'"
        ).fetchone()[0]
        assert accepted == 0, "a tampering run got repository memory accepted"
        learned = world.store.connection.execute(
            "SELECT COUNT(*) FROM repo_memory_learning WHERE status='UPDATED'"
        ).fetchone()[0]
        assert learned == 0, "a tampering run recorded a memory update"
    return world.observation()


def test_protected_namespaces_are_denied_for_writes():
    """The deny rules must exist, name both namespaces, and cover writes."""
    source = inspect.getsource(agent_module)
    for path in PROTECTED:
        assert (
            f'paths=["{path}"], mode="deny"'
            in source.replace("\n", " ").replace("                ", "")
            or path in source
        ), f"{path} has no deny rule"
    assert source.count('mode="deny"') >= 2, "a deny rule was removed"


def test_deny_rules_are_write_scoped_not_read_scoped():
    """Agents must still be able to READ memory; only writes are denied."""
    source = inspect.getsource(agent_module)
    window = source[source.index("permissions = ") : source.index("middleware = ")]
    assert 'operations=["write"]' in window
    assert 'operations=["read"]' not in window, (
        "reads were denied; memory is meant to be readable by execution agents"
    )


def test_scenario_passes(tmp_path):
    result = run("S22", tmp_path / "s22", layer=Layer.L1)
    assert result.ok, "\n" + result.report()
