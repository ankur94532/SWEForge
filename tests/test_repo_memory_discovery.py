"""Repository-memory learning: discovery coverage and retry containment.

Curation runs after publication over the cumulative diff, so the evidence it
may cite is bounded by what the task changed.  These tests pin that boundary
(so the known coverage gap cannot widen silently) and pin the containment
rules that keep a failing curator from holding an IssueThread.
"""

import sqlite3

import pytest
from test_publication_identity import THREAD_ID, Harness, approval, source_event

from sweforge.github_store import (
    MAX_ISSUE_RESOLUTION_ATTEMPTS,
    MAX_MEMORY_LEARNING_ATTEMPTS,
    SQLiteGitHubStore,
    WorkflowPhase,
)
from sweforge.memory_learning import (
    RepoMemoryCuratorResponse,
    RepoMemoryProposal,
    curate_repository_memory,
)
from sweforge.workflow import UNCONFIGURED_MEMORY_LEARNING


def repository_with_unchanged_knowledge(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    # Durable repository knowledge that a task would read but never modify.
    (root / "Makefile").write_text("test:\n\tuv run pytest -q --strict-markers\n")
    (root / "CONTRIBUTING.md").write_text("Always run `make test` before pushing.\n")
    long_file = ["# changelog"] * 200
    long_file[150] = "Deploys require the us-east-1 bastion."
    (root / "CHANGELOG.md").write_text("\n".join(long_file) + "\n")
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    return root


def capture_curator_prompt(monkeypatch, captured):
    def fake_init(model, **kwargs):
        class Curator:
            def with_structured_output(self, schema):
                return self

            def invoke(self, prompt):
                captured["prompt"] = prompt
                return RepoMemoryCuratorResponse(proposals=[])

        return Curator()

    monkeypatch.setattr("sweforge.memory_learning.init_chat_model", fake_init)


def test_evidence_catalog_is_bounded_to_changed_file_windows(tmp_path, monkeypatch):
    """Known coverage boundary: only changed files, only their first window.

    Knowledge discovered in files the task did not modify -- build commands,
    conventions, configuration locations -- is not citable.  See the
    `propose_repo_memory` milestone.
    """
    root = repository_with_unchanged_knowledge(tmp_path)
    captured: dict[str, str] = {}
    capture_curator_prompt(monkeypatch, captured)
    curate_repository_memory(
        model="fake:model",
        repo_id=1,
        worktree=root,
        changed_files=["src/app.py", "CHANGELOG.md"],
        diff="--- a/src/app.py\n+++ b/src/app.py\n+    return 1\n",
        existing_memory="",
        plan_text="make main return 1",
    )
    catalog = captured["prompt"].split("Evidence catalog (line-numbered):")[1]
    cited = [line for line in catalog.splitlines() if line.startswith("[")]
    assert any("src/app.py" in line for line in cited)
    # Unchanged files never enter the catalog at all.
    assert "Makefile" not in catalog
    assert "CONTRIBUTING.md" not in catalog
    # Even a changed file is truncated, so later durable lines are uncitable.
    assert "us-east-1 bastion" not in catalog
    assert not any("CHANGELOG.md:81" in line for line in cited)


def test_citing_uncatalogued_evidence_rejects_the_whole_batch(tmp_path, monkeypatch):
    """Evidence IDs are application-generated, so out-of-catalog facts cannot land."""
    root = repository_with_unchanged_knowledge(tmp_path)

    def fake_init(model, **kwargs):
        class Curator:
            def with_structured_output(self, schema):
                return self

            def invoke(self, prompt):
                return RepoMemoryCuratorResponse(
                    proposals=[
                        RepoMemoryProposal(
                            category="TOOLING",
                            fact="Run tests with `uv run pytest -q --strict-markers`.",
                            evidence_ids=["0123456789abcdef0123"],
                            durability_reason="The Makefile defines the test target.",
                        )
                    ]
                )

        return Curator()

    monkeypatch.setattr("sweforge.memory_learning.init_chat_model", fake_init)
    with pytest.raises(ValueError, match="unknown evidence ID"):
        curate_repository_memory(
            model="fake:model",
            repo_id=1,
            worktree=root,
            changed_files=["src/app.py"],
            diff="x",
            existing_memory="",
            plan_text="p",
        )


class NullMemoryStore:
    def get(self, *args, **kwargs):
        return None

    def put(self, *args, **kwargs):
        return None

    def search(self, *args, **kwargs):
        return []


def published_thread(tmp_path):
    harness = Harness(tmp_path)
    origin = source_event(harness.repo, "1", "@agent fix it", "2026-01-01T00:00:00Z")
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("a.txt")
    assert harness.publisher.publish_one().status == "COMPLETED"
    harness.engine.memory_learner = None
    return harness, origin


def advance(harness, *, memory_model, resolution_model=None):
    return harness.engine.advance(
        thread_id=THREAD_ID,
        model="planning-sonnet",
        review_model="review-sonnet",
        memory_model=memory_model,
        resolution_model=resolution_model,
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
        memory_store=NullMemoryStore(),
        execute_kwargs=harness.execute_kwargs("a.txt"),
    )


def test_a_failing_curator_cannot_hold_the_thread_at_idle(tmp_path, monkeypatch):
    """Repo memory is an optimization; it must never strand queued work."""
    harness, origin = published_thread(tmp_path)
    calls: list[str] = []

    def exploding(model, **kwargs):
        calls.append(model)
        raise RuntimeError("missing ANTHROPIC_API_KEY")

    monkeypatch.setattr("sweforge.memory_learning.init_chat_model", exploding)
    # Historical-case learning is a separate lane and must also stay offline.
    monkeypatch.setattr("sweforge.issue_resolution.init_chat_model", exploding)
    assert advance(harness, memory_model="anthropic:model").phase is WorkflowPhase.IDLE

    # A new actionable input is waiting behind the failing learning records.
    harness.record(
        source_event(harness.repo, "9", "@agent now add tests", "2026-01-05T00:00:00Z")
    )
    assert harness.engine.next_workflow_input(THREAD_ID) is not None

    results = [advance(harness, memory_model="anthropic:model") for _ in range(8)]
    messages = [item.message for item in results]
    assert messages.count("repository learning retried") == (
        MAX_MEMORY_LEARNING_ATTEMPTS - 1
    )
    assert messages.count("issue resolution learning retried") == (
        MAX_ISSUE_RESOLUTION_ATTEMPTS - 1
    )
    # Both lanes are bounded, then the queued logical input finally runs.
    assert len(calls) == (MAX_MEMORY_LEARNING_ATTEMPTS + MAX_ISSUE_RESOLUTION_ATTEMPTS)
    assert harness.store.workflow_state(THREAD_ID).cycle_id == 2
    assert harness.store.pending_memory_learning(THREAD_ID) is None
    assert harness.store.pending_issue_resolution(THREAD_ID) is None

    # The failure stays visible for diagnosis rather than being erased.
    record = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == "FAILED"
    assert record.attempt_count == MAX_MEMORY_LEARNING_ATTEMPTS
    assert "ANTHROPIC_API_KEY" in record.error_message
    harness.store.close()


def test_learning_without_a_configured_curator_is_recorded_explicitly(tmp_path):
    """`memory_model=None` must not look like "curated, nothing durable found"."""
    harness, origin = published_thread(tmp_path)
    assert advance(harness, memory_model=None).phase is WorkflowPhase.IDLE
    record = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert record.status == "NO_UPDATE"
    assert record.error_message == UNCONFIGURED_MEMORY_LEARNING
    harness.store.close()


def test_attempt_counter_is_added_to_pre_existing_databases(tmp_path):
    path = tmp_path / "legacy.db"
    SQLiteGitHubStore(path).close()
    raw = sqlite3.connect(path)
    raw.executescript(
        """CREATE TABLE tmp AS SELECT learning_id, source_event_key, thread_id,
               cycle_id, root_input_id, repo_id, status, accepted_candidates,
               rejected_candidates, proposal_json, error_message, created_at,
               updated_at FROM repo_memory_learning;
           DROP TABLE repo_memory_learning;
           ALTER TABLE tmp RENAME TO repo_memory_learning;"""
    )
    raw.commit()
    raw.close()

    migrated = SQLiteGitHubStore(path)
    columns = {
        row[1]: row
        for row in migrated.connection.execute(
            "PRAGMA table_info(repo_memory_learning)"
        )
    }
    assert "attempt_count" in columns
    assert columns["attempt_count"][3] == 1  # NOT NULL
    assert columns["attempt_count"][4] == "0"  # defaults to an unused budget
    migrated.close()
    SQLiteGitHubStore(path).close()  # reopening stays idempotent
