"""S42-S45: failures outside the tool surface.

S7 covers a tool failing and S26 covers review infrastructure failing. These
cover the model itself failing, and the two workspace states a crash can leave
behind. All four must fail closed: no permit consumed into a fabricated
success, no worktree recreated over a live branch, no silent fallback when the
checkout a run was authorized against is gone.
"""

import subprocess

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import WorkflowPhase
from sweforge.workspace import ThreadWorkspace, WorkspaceError


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@scenario(
    "S42",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="A planner model failure produces no plan and no authorization.",
)
def s42_planner_model_failure(root_dir) -> Observation:
    def failing_planner(**kwargs):
        raise RuntimeError("planner model provider is unavailable")

    world = World.build(root_dir, planner=failing_planner)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        thread_id = next(iter(world.thread_ids))

        with pytest.raises(RuntimeError, match="planner model"):
            world.drive(
                thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL, max_ticks=2
            )

        posted = world.store.current_plan(thread_id)
        assert posted is None or posted.status != "POSTED", (
            "a failed planner still posted a plan"
        )
        permits = world.store.connection.execute(
            "SELECT COUNT(*) FROM execution_permits"
        ).fetchone()[0]
        assert permits == 0, f"a failed planner led to {permits} permits"
    return world.observation()


@scenario(
    "S43",
    layer=Layer.L1,
    invariants=["INV-ONE-INITIAL", "INV-NO-HOT-RETRY", "INV-NO-PUBLICATION"],
    description="An execution model failure is terminal, not silently retried.",
)
def s43_execution_model_failure(root_dir) -> Observation:
    calls = {"n": 0}

    def failing_runner(**kwargs):
        calls["n"] += 1
        raise RuntimeError("execution model provider is unavailable")

    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=["1. a\n2. b"]),
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
            until=WorkflowPhase.EXECUTION_FAILED,
            max_ticks=8,
            execute_kwargs={
                "lock_root": world.root / "locks",
                "runner": failing_runner,
                "checkpointer": object(),
            },
        )
        attempt = world.store.latest_attempt(thread_id, 1)
        assert attempt.status == "FAILED", (
            f"a model failure recorded {attempt.status}, not FAILED"
        )
        assert calls["n"] == 1, (
            f"the failing model was invoked {calls['n']} times inside one attempt"
        )
    return world.observation()


@scenario(
    "S44",
    layer=Layer.L1,
    invariants=["INV-ONE-ROOT", "INV-THREAD-ISOLATION"],
    # INV-WORKTREE-CONFINED is deliberately absent: it needs outside markers
    # planted to check anything, and this scenario is about reattaching to a
    # surviving branch rather than about escaping the worktree. The
    # reattachment properties are asserted directly in the body.
    description="A branch left without a worktree is reattached, not recreated.",
)
def s44_branch_without_worktree(root_dir) -> Observation:
    world = World.build(root_dir)
    source = world.root / "source"
    workspaces = world.root / "workspaces"

    first = ThreadWorkspace.create(
        repository=source, workspace_root=workspaces, repo_id=1, issue_number=7
    )
    branch = first.branch_name
    marker = "reattached\n"
    (first.path / "MARKER.md").write_text(marker)
    _git(first.path, "add", "-A")
    _git(
        first.path,
        "-c",
        "user.email=h@e.com",
        "-c",
        "user.name=H",
        "commit",
        "-qm",
        "w",
    )
    head = _git(first.path, "rev-parse", "HEAD")

    # The crash: the worktree directory vanishes, the branch survives.
    subprocess.run(["rm", "-rf", str(first.path)], check=True)
    assert _git(source, "rev-parse", "--verify", branch) == head, (
        "the branch did not survive the lost worktree"
    )

    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))
        # Recreating must reattach to the surviving branch, not fork a new one.
        again = ThreadWorkspace.create(
            repository=source, workspace_root=workspaces, repo_id=1, issue_number=7
        )
        assert again.branch_name == branch, "reattachment renamed the branch"
        assert _git(again.path, "rev-parse", "HEAD") == head, (
            "reattachment discarded the branch's commit"
        )
        assert (again.path / "MARKER.md").read_text() == marker, (
            "reattachment recreated the worktree instead of restoring it"
        )
    return world.observation()


@scenario(
    "S45",
    layer=Layer.L1,
    invariants=["INV-PERMIT-NONE", "INV-NO-PUBLICATION"],
    description="A missing local checkout fails closed rather than falling back.",
)
def s45_missing_local_checkout(root_dir) -> Observation:
    world = World.build(root_dir)
    with world.activate():
        world.ingest(world.event("1", "@agent fix it", "2026-01-01T00:00:00Z"))

        missing = world.root / "not-a-checkout"
        with pytest.raises(WorkspaceError, match="not a directory"):
            ThreadWorkspace.create(
                repository=missing,
                workspace_root=world.root / "workspaces",
                repo_id=1,
                issue_number=7,
            )

        # A directory that exists but is not a repository also fails closed.
        plain = world.root / "plain"
        plain.mkdir()
        with pytest.raises(WorkspaceError, match="Not a Git repository"):
            ThreadWorkspace.create(
                repository=plain,
                workspace_root=world.root / "workspaces",
                repo_id=1,
                issue_number=7,
            )
    return world.observation()


@pytest.mark.parametrize("scenario_id", ["S42", "S43", "S44", "S45"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower(), layer=Layer.L1)
    assert result.ok, "\n" + result.report()
