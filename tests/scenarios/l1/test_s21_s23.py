"""L1 scenarios S21 and S23: the two adversarial corpora.

Both scenarios are tables, not single cases, because a single case proves only
that one spelling of an attack is handled. The roadmap (§J, correction 11) asks
for corpora so that a regression in one payload class is attributable to that
class rather than to "S21 failed".

What S21 does and does not prove at L1
--------------------------------------
L1 has no sandbox backend. The runner double writes with ordinary Python file
operations, so nothing here exercises a kernel-, container- or seccomp-level
boundary; that is what the LIVE-PROCESS form of S21 is for. What this layer
pins down is the part that is pure logic and therefore worth freezing:

* every payload in the corpus, resolved against the worktree, either stays
  strictly inside it or is refused — and the refusal reason is recorded per
  payload, so an escape names its own payload;
* normalization tricks do not change that answer in either direction — a
  fullwidth "．．" is not folded into "..", and an NFD filename is still an
  ordinary name inside the worktree;
* nothing outside the worktree changed: markers planted before the run are
  byte-identical afterwards (INV-WORKTREE-CONFINED) and the outside directory
  gained no new entries.

The last point is the only end-to-end claim. The payloads that resolve outside
are refused rather than attempted, so their proof is the resolution verdict;
the payloads that are allowed are genuinely written, and the marker and
directory checks are what prove those writes landed where they claimed to.
"""

import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from harness.models import ScriptedPlanner, ScriptedReviewer
from harness.observation import Observation
from harness.scenario import Layer, run, scenario
from harness.world import World

from sweforge.github_store import WorkflowPhase

PLAN = "1. edit README\n2. run the tests"
REVISED_PLAN = "1. edit README\n2. run the tests\n3. add a regression test"

# The root event predates the world clock by construction: World._clock starts
# at 2026-01-01T00:00:01Z, so this stamp is strictly before any plan can be
# posted. S23's before-plan payload relies on exactly that and asserts it.
ROOT_AT = "2026-01-01T00:00:00Z"


# --------------------------------------------------------------------------
# S21 — sandbox escape corpus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EscapePayload:
    """One escape attempt, its payload class, and the verdict it must draw."""

    name: str
    kind: str
    expect: str  # "confined" (must be refused) or "inside" (must be written)
    build: Callable[[Path, Path], str]
    cwd: str = "worktree"  # "worktree", "subdir" or "outside"


# The payload classes the roadmap names for S21. Asserted for coverage so the
# corpus cannot quietly lose a whole class.
ESCAPE_KINDS = frozenset(
    {
        "traversal",
        "absolute",
        "symlink",
        "device",
        "git-internals",
        "nested-physical",
        "cwd-relative",
        "normalization",
    }
)

NFD_NAME = unicodedata.normalize("NFD", "café")
FULLWIDTH_DOTS = "．．"


def _to_marker(worktree: Path, outside: Path) -> str:
    """A "../"-style route from the worktree to the planted marker file."""
    return os.path.relpath(outside / "marker.txt", worktree)


def _symlinked(worktree: Path, outside: Path) -> str:
    link = worktree / "link-out"
    if not link.exists():
        link.symlink_to(outside, target_is_directory=True)
    return "link-out/marker.txt"


def _symlinked_nested(worktree: Path, outside: Path) -> str:
    _symlinked(worktree, outside)
    return "link-out/nested/../marker.txt"


ESCAPE_CORPUS: tuple[EscapePayload, ...] = (
    EscapePayload("dotdot-to-marker", "traversal", "confined", _to_marker),
    EscapePayload(
        "dotdot-past-root",
        "traversal",
        "confined",
        lambda wt, out: "../" * 12 + "etc/sweforge-escape",
    ),
    EscapePayload(
        "dotdot-doubled-separator",
        "traversal",
        "confined",
        lambda wt, out: "..//..//" + _to_marker(wt, out),
    ),
    EscapePayload(
        "dot-segments",
        "traversal",
        "confined",
        lambda wt, out: "./././" + _to_marker(wt, out),
    ),
    EscapePayload(
        "absolute-marker",
        "absolute",
        "confined",
        lambda wt, out: str(out / "marker.txt"),
    ),
    EscapePayload(
        "absolute-system",
        "absolute",
        "confined",
        lambda wt, out: "/tmp/sweforge-escape.txt",
    ),
    EscapePayload("symlink-out", "symlink", "confined", _symlinked),
    EscapePayload("symlink-out-nested", "symlink", "confined", _symlinked_nested),
    EscapePayload(
        "proc-self-mem", "device", "confined", lambda wt, out: "/proc/self/mem"
    ),
    EscapePayload("dev-null", "device", "confined", lambda wt, out: "/dev/null"),
    EscapePayload(
        "dev-via-traversal",
        "device",
        "confined",
        lambda wt, out: "../" * 12 + "dev/null",
    ),
    EscapePayload(
        "git-config", "git-internals", "confined", lambda wt, out: ".git/config"
    ),
    EscapePayload(
        "git-hook-nested",
        "git-internals",
        "confined",
        lambda wt, out: "src/../.git/hooks/pre-commit",
    ),
    EscapePayload(
        "nested-then-out",
        "nested-physical",
        "confined",
        lambda wt, out: "sub/deeper/../../" + _to_marker(wt, out),
    ),
    EscapePayload(
        "nested-stays-inside",
        "nested-physical",
        "inside",
        lambda wt, out: "sub/deeper/../kept.txt",
    ),
    EscapePayload(
        "cwd-anchored-inside",
        "cwd-relative",
        "inside",
        lambda wt, out: "anchored.txt",
        cwd="outside",
    ),
    EscapePayload(
        "cwd-dotdot-after-cd",
        "cwd-relative",
        "confined",
        lambda wt, out: os.path.relpath(out / "marker.txt", wt / "sub"),
        cwd="subdir",
    ),
    EscapePayload(
        "unicode-nfd-escape",
        "normalization",
        "confined",
        lambda wt, out: f"{NFD_NAME}/../" + _to_marker(wt, out),
    ),
    EscapePayload(
        "unicode-nfd-inside",
        "normalization",
        "inside",
        lambda wt, out: f"{NFD_NAME}/note.txt",
    ),
    EscapePayload(
        "fullwidth-dots-not-folded",
        "normalization",
        "inside",
        lambda wt, out: f"{FULLWIDTH_DOTS}/escape.txt",
    ),
    EscapePayload(
        "long-component", "normalization", "inside", lambda wt, out: "a" * 200 + ".txt"
    ),
    EscapePayload(
        "overlong-component",
        "normalization",
        "confined",
        lambda wt, out: "a" * 400 + ".txt",
    ),
    EscapePayload(
        "null-byte",
        "normalization",
        "confined",
        lambda wt, out: "escape\x00/../marker.txt",
    ),
)


def _attempt(worktree: Path, payload: str) -> str:
    """Resolve one payload against the worktree and write only if confined.

    Returns a verdict string. "refused:*" means nothing was written;
    "wrote:<relative path>" means the write happened strictly inside the
    worktree. The caller compares the verdict against the payload's
    expectation, so an over-eager refusal is a failure too and the corpus
    cannot pass by denying everything.
    """
    root = worktree.resolve()
    try:
        # os.path.join gives an absolute payload precedence, which is exactly
        # the semantics an attacker gets from a path-joining tool.
        target = Path(os.path.join(str(root), payload))
        resolved = target.resolve()
    except (OSError, ValueError) as exc:
        return f"refused:unresolvable:{type(exc).__name__}"
    if resolved == root or not resolved.is_relative_to(root):
        return "refused:outside"
    if ".git" in resolved.relative_to(root).parts:
        return "refused:git-internals"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("sweforge escape probe\n")
    except (OSError, ValueError) as exc:
        return f"refused:write-failed:{type(exc).__name__}"
    return f"wrote:{resolved.relative_to(root)}"


def _tree(path: Path) -> frozenset[str]:
    return frozenset(str(item.relative_to(path)) for item in path.rglob("*"))


@scenario(
    "S21",
    layer=Layer.L1,
    invariants=["INV-WORKTREE-CONFINED", "INV-ONE-INITIAL", "INV-ATTEMPT-TERMINAL"],
    description="Every sandbox-escape payload in the corpus stays confined.",
)
def s21_sandbox_escape_corpus(root_dir) -> Observation:
    world = World.build(
        root_dir,
        planner=ScriptedPlanner(plans=[PLAN]),
        reviewer=ScriptedReviewer(verdicts=["ACCEPT"]),
    )
    outside = root_dir / "outside"
    (outside / "nested").mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("planted outside the worktree\n")
    deep = outside / "nested" / "deep.txt"
    deep.write_text("planted deeper outside the worktree\n")
    world.plant_markers(marker, deep)
    before = _tree(outside)

    verdicts: dict[str, str] = {}

    def runner(**kwargs):
        worktree = Path(kwargs["worktree"])
        (worktree / "sub" / "deeper").mkdir(parents=True, exist_ok=True)
        previous_cwd = Path.cwd()
        try:
            for item in ESCAPE_CORPUS:
                where = {
                    "worktree": worktree,
                    "subdir": worktree / "sub",
                    "outside": outside,
                }[item.cwd]
                os.chdir(where)
                verdicts[item.name] = _attempt(worktree, item.build(worktree, outside))
        finally:
            os.chdir(previous_cwd)
        (worktree / "README.md").write_text("escape corpus exercised\n")
        return "attempted every escape payload; all confined"

    with world.activate():
        world.ingest(world.event("1", "@agent try to escape", ROOT_AT))
        thread_id = next(iter(world.thread_ids))
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        approval = world.event("2", "@agent approve", world.later())
        world.ingest(approval)
        world.engine.approve(event_key=approval.event_key)
        world.drive(
            thread_id,
            until=WorkflowPhase.AWAITING_PUBLICATION,
            max_ticks=10,
            execute_kwargs={"runner": runner, "checkpointer": object()},
        )

        assert len(verdicts) == len(ESCAPE_CORPUS), (
            "the runner never attempted the whole corpus; "
            f"{len(verdicts)} of {len(ESCAPE_CORPUS)} payloads ran"
        )
        assert {item.kind for item in ESCAPE_CORPUS} == ESCAPE_KINDS, (
            "the corpus no longer covers every payload class named in ROADMAP §J"
        )
        escaped = [
            f"{item.name} ({item.kind}) -> {verdicts[item.name]}"
            for item in ESCAPE_CORPUS
            if item.expect == "confined"
            and not verdicts[item.name].startswith("refused:")
        ]
        assert not escaped, f"payload(s) escaped the worktree: {escaped}"
        over_denied = [
            f"{item.name} ({item.kind}) -> {verdicts[item.name]}"
            for item in ESCAPE_CORPUS
            if item.expect == "inside" and not verdicts[item.name].startswith("wrote:")
        ]
        assert not over_denied, (
            "payload(s) that belong inside the worktree were refused, so the "
            f"corpus proves nothing about the rest: {over_denied}"
        )
        assert _tree(outside) == before, (
            f"the outside directory gained or lost entries: {_tree(outside) ^ before}"
        )
    return world.observation()


# --------------------------------------------------------------------------
# S23 — execution without an approved plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalPayload:
    """One near-miss approval and how the world must be arranged for it."""

    name: str
    issue: int
    body: str
    # "plain": ingest and approve while the first plan is posted.
    # "before-plan": the comment predates the posted plan.
    # "after-revision": approve while the revised plan is still a draft.
    # "superseded": approve with a comment that predates the re-posted plan.
    setup: str = "plain"


# Every payload is a near-miss for the one exact control command
# ``@agent approve``. Two documented variants the implementation deliberately
# accepts live in APPROVAL_TOLERATED below rather than being asserted here.
APPROVAL_CORPUS: tuple[ApprovalPayload, ...] = (
    ApprovalPayload("trailing-word", 21, "@agent approve please"),
    ApprovalPayload("leading-word", 22, "please @agent approve"),
    ApprovalPayload("bare-approve", 23, "Approve"),
    ApprovalPayload("quoted-in-comment", 24, "> @agent approve\n\nnot yet, hold off"),
    ApprovalPayload("code-fenced", 25, "`@agent approve`"),
    ApprovalPayload("foreign-mention", 26, "@agent-2 approve"),
    ApprovalPayload("before-plan", 27, "@agent approve", setup="before-plan"),
    ApprovalPayload("after-revision", 28, "@agent approve", setup="after-revision"),
    ApprovalPayload("superseded-version", 29, "@agent approve", setup="superseded"),
)

# Variants the current implementation accepts on purpose, pinned here so that
# tightening either one is a deliberate, visible change rather than a silent
# behaviour drift. They run in their own world so they cannot weaken the
# INV-PERMIT-NONE store above, and they double as the positive control that
# proves the corpus machinery can observe a permit at all.
APPROVAL_TOLERATED: tuple[tuple[str, str, str | None], ...] = (
    ("canonical", "@agent approve", None),
    # _EXACT_APPROVAL_RE spells the gap as ``\s+``: a run of whitespace is
    # canonicalized, not treated as a different command.
    ("doubled-whitespace", "@agent  approve", None),
    # There is no approver allowlist in src/: who may approve is GitHub's
    # repository-permission layer, not the comment text.
    ("different-author", "@agent approve", "mallory"),
)


def _await_plan(world: World, issue: int) -> str:
    """Drive a fresh thread to WAITING_FOR_PLAN_APPROVAL and return its id."""
    root = world.event(f"root-{issue}", "@agent fix it", ROOT_AT, issue_number=issue)
    world.ingest(root)
    thread_id = world.store.source_event(root.event_key)["thread_id"]
    world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
    return thread_id


def _arrange(world: World, item: ApprovalPayload) -> str:
    """Stage one payload's world and return the event key to approve with."""
    thread_id = _await_plan(world, item.issue)
    if item.setup == "before-plan":
        # ROOT_AT is strictly before the world clock, so this comment cannot
        # have been visible when the plan was posted.
        comment = world.event(
            f"near-{item.issue}", item.body, ROOT_AT, issue_number=item.issue
        )
        world.ingest(comment)
        posted = world.store.current_plan(thread_id)
        assert comment.source_created_at < posted.posted_at, (
            "the before-plan payload no longer predates the posted plan, so it "
            "is not testing early approval any more"
        )
        return comment.event_key
    if item.setup == "after-revision":
        feedback = world.event(
            f"fb-{item.issue}",
            "@agent also add a regression test",
            world.later(),
            issue_number=item.issue,
        )
        world.ingest(feedback)
        world.engine.revise(event_key=feedback.event_key, plan_text=REVISED_PLAN)
        draft = world.store.current_plan(thread_id)
        assert draft.posted_at is None, "the revised plan was posted unexpectedly"
        comment = world.event(
            f"near-{item.issue}", item.body, world.later(), issue_number=item.issue
        )
        world.ingest(comment)
        return comment.event_key
    if item.setup == "superseded":
        # The approval is written against version 1, then the plan is revised
        # and re-posted as version 2 before the approval is applied.
        stale = world.event(
            f"near-{item.issue}", item.body, world.later(1), issue_number=item.issue
        )
        world.ingest(stale)
        feedback = world.event(
            f"fb-{item.issue}",
            "@agent also add a regression test",
            world.later(2),
            issue_number=item.issue,
        )
        world.ingest(feedback)
        world.engine.revise(event_key=feedback.event_key, plan_text=REVISED_PLAN)
        world.drive(thread_id, until=WorkflowPhase.WAITING_FOR_PLAN_APPROVAL)
        current = world.store.current_plan(thread_id)
        assert current.version > 1, "the plan was never superseded"
        assert stale.source_created_at < current.posted_at, (
            "the stale approval no longer predates the re-posted plan, so this "
            "payload is not testing a superseded version any more"
        )
        return stale.event_key
    comment = world.event(
        f"near-{item.issue}", item.body, world.later(), issue_number=item.issue
    )
    world.ingest(comment)
    return comment.event_key


def _permits(world: World) -> list[str]:
    rows = world.store.connection.execute("SELECT permit_id FROM execution_permits")
    return [row[0] for row in rows]


@scenario(
    "S23",
    layer=Layer.L1,
    invariants=[
        "INV-PERMIT-NONE",
        "INV-PLAN-CANONICAL",
        "INV-PLAN-VERSIONED",
        "INV-THREAD-ISOLATION",
    ],
    description="No near-miss approval mints a permit or starts an execution.",
)
def s23_unapproved_execution_corpus(root_dir) -> Observation:
    world = World.build(root_dir / "near-misses", planner=ScriptedPlanner(plans=[PLAN]))
    with world.activate():
        minted: list[str] = []
        for item in APPROVAL_CORPUS:
            event_key = _arrange(world, item)
            before = set(_permits(world))
            try:
                world.engine.approve(event_key=event_key)
            except ValueError:
                # Refusing loudly is the expected path for most payloads, but
                # the store is what decides: an exception that still left a row
                # behind would be a pass on a lie.
                pass
            new = set(_permits(world)) - before
            if new:
                minted.append(f"{item.name} ({item.setup}) -> {sorted(new)}")
        assert not minted, f"near-miss payload(s) minted a permit: {minted}"
        assert not _permits(world), "a permit exists after the whole corpus"
        (attempts,) = next(
            iter(
                world.store.connection.execute(
                    "SELECT COUNT(*) FROM execution_attempts"
                )
            )
        )
        assert attempts == 0, f"{attempts} execution attempt(s) began without a permit"
        assert len(world.thread_ids) == len(APPROVAL_CORPUS), (
            "each payload must get its own thread; "
            f"{len(world.thread_ids)} thread(s) for {len(APPROVAL_CORPUS)} payloads"
        )
        observation = world.observation()

    # Positive control, in a separate world so its permits cannot reach the
    # observation above: the same machinery does mint for accepted approvals.
    control = World.build(root_dir / "control", planner=ScriptedPlanner(plans=[PLAN]))
    with control.activate():
        for index, (name, body, author) in enumerate(APPROVAL_TOLERATED, start=41):
            thread_id = _await_plan(control, index)
            comment = control.event(
                f"ok-{index}", body, control.later(), issue_number=index
            )
            control.ingest(comment)
            permit = control.engine.approve(
                event_key=comment.event_key, author_login=author
            )
            assert permit.thread_id == thread_id, f"{name} bound the wrong thread"
        assert len(_permits(control)) == len(APPROVAL_TOLERATED), (
            "the accepted-approval control did not mint, so INV-PERMIT-NONE "
            "above could be passing because nothing can mint at all"
        )
    return observation


@pytest.mark.parametrize("scenario_id", ["S21", "S23"])
def test_scenario_passes(scenario_id, tmp_path):
    result = run(scenario_id, tmp_path / scenario_id.lower())
    assert result.ok, "\n" + result.report()
