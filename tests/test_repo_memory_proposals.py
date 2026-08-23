"""Execution-time repository-memory proposals.

A proposal nominates WHERE evidence lives.  The application reads those lines
itself and the existing validator remains the sole writer of AGENTS.md, so the
coverage gap closes without letting a model assert repository truth.
"""

import subprocess
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore
from test_publication_identity import THREAD_ID, Harness, approval, source_event

from sweforge.github_store import (
    RepoMemoryCandidateRecord,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
    WorkflowPhase,
    repo_memory_candidate_id_for,
)
from sweforge.memory_learning import (
    apply_memory_candidates,
    candidate_from_proposal,
)
from sweforge.repo_memory import read_repo_memory, repo_memory_namespace

REPO_ID = 1


def knowledge_repo(tmp_path):
    """A repository whose durable knowledge lives in files a task won't touch."""
    root = tmp_path / "knowledge"
    (root / "src").mkdir(parents=True)
    (root / "Makefile").write_text(
        "test:\n\tuv run pytest -q --strict-markers\n\nlint:\n\tuv run ruff check .\n"
    )
    (root / "CONTRIBUTING.md").write_text(
        "# Contributing\n\nAlways run `make test` before pushing.\n"
    )
    deep = ["# changelog"] * 200
    deep[150] = "Deploys require the us-east-1 bastion host."
    (root / "CHANGELOG.md").write_text("\n".join(deep) + "\n")
    (root / "secrets.env").write_text("API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n")
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    return root


def test_unchanged_makefile_knowledge_becomes_a_candidate(tmp_path):
    root = knowledge_repo(tmp_path)
    candidate = candidate_from_proposal(
        repo_id=REPO_ID,
        worktree=root,
        category="TOOLING",
        fact="Run tests with `uv run pytest -q --strict-markers`.",
        durability_reason="The Makefile defines the canonical test target.",
        path="Makefile",
        start_line=1,
        end_line=2,
    )
    assert candidate.evidence[0].path == "Makefile"
    assert "uv run pytest" in candidate.evidence[0].excerpt


def test_unchanged_contributing_conventions_become_a_candidate(tmp_path):
    root = knowledge_repo(tmp_path)
    candidate = candidate_from_proposal(
        repo_id=REPO_ID,
        worktree=root,
        category="CONVENTION",
        fact="Contributors run `make test` before pushing.",
        durability_reason="CONTRIBUTING.md states the convention.",
        path="CONTRIBUTING.md",
        start_line=3,
        end_line=3,
    )
    assert "make test" in candidate.evidence[0].excerpt


def test_evidence_beyond_the_diff_catalog_window_is_reachable(tmp_path):
    root = knowledge_repo(tmp_path)
    candidate = candidate_from_proposal(
        repo_id=REPO_ID,
        worktree=root,
        category="OPERATIONS",
        fact="Deploys go through the us-east-1 bastion.",
        durability_reason="The changelog documents the deploy path.",
        path="CHANGELOG.md",
        start_line=151,
        end_line=151,
    )
    assert "us-east-1 bastion" in candidate.evidence[0].excerpt


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside.txt", "src/../../escape.txt"],
)
def test_paths_escaping_the_worktree_are_rejected(tmp_path, path):
    root = knowledge_repo(tmp_path)
    with pytest.raises(ValueError):
        candidate_from_proposal(
            repo_id=REPO_ID,
            worktree=root,
            category="X",
            fact="f",
            durability_reason="r",
            path=path,
            start_line=1,
            end_line=1,
        )


@pytest.mark.parametrize(
    ("start_line", "end_line"),
    [(5, 2), (900, 901), (1, 900)],
)
def test_invalid_line_ranges_are_rejected(tmp_path, start_line, end_line):
    root = knowledge_repo(tmp_path)
    with pytest.raises(ValueError):
        candidate_from_proposal(
            repo_id=REPO_ID,
            worktree=root,
            category="X",
            fact="f",
            durability_reason="r",
            path="Makefile",
            start_line=start_line,
            end_line=end_line,
        )


def test_secret_evidence_is_rejected(tmp_path):
    root = knowledge_repo(tmp_path)
    with pytest.raises(ValueError, match="secret"):
        candidate_from_proposal(
            repo_id=REPO_ID,
            worktree=root,
            category="CONFIG",
            fact="The API key lives in secrets.env.",
            durability_reason="It is checked in.",
            path="secrets.env",
            start_line=1,
            end_line=1,
        )


def test_evidence_is_derived_by_the_application_not_asserted(tmp_path):
    """A proposer cannot make the record claim content the file does not have."""
    root = knowledge_repo(tmp_path)
    candidate = candidate_from_proposal(
        repo_id=REPO_ID,
        worktree=root,
        category="TOOLING",
        fact="Tests are run with `npm test`.",  # deliberately wrong claim
        durability_reason="asserted, not shown",
        path="Makefile",
        start_line=1,
        end_line=2,
    )
    # The stored evidence is what the file actually says.
    assert "npm test" not in candidate.evidence[0].excerpt
    assert "uv run pytest" in candidate.evidence[0].excerpt
    import hashlib

    actual = "\n".join(Path(root, "Makefile").read_text().splitlines()[0:2])
    assert (
        candidate.evidence[0].content_hash
        == hashlib.sha256(actual.encode()).hexdigest()
    )


def test_duplicate_proposals_are_one_durable_candidate(tmp_path):
    store = SQLiteGitHubStore(tmp_path / "state.db")
    store.upsert_repository(REPO_ID, "example/repo", "now")
    event = source_event(
        type("R", (), {"repo_id": REPO_ID, "full_name": "example/repo"}),
        "1",
        "@agent fix",
        "2026-01-01T00:00:00Z",
    )
    store.record_batch(
        REPO_ID, "issue_comments", [event], since="now", etag=None, polled_at="now"
    )
    identity = {
        "repo_id": REPO_ID,
        "thread_id": THREAD_ID,
        "cycle_id": 1,
        "root_input_id": event.event_key,
        "fact": "Run tests with make test.",
        "evidence_path": "Makefile",
        "evidence_start_line": 1,
        "evidence_end_line": 2,
    }
    candidate_id = repo_memory_candidate_id_for(**identity)
    # Whitespace-only differences must not create a second candidate.
    assert (
        repo_memory_candidate_id_for(
            **{**identity, "fact": "Run tests  with make test."}
        )
        == candidate_id
    )
    record = RepoMemoryCandidateRecord(
        candidate_id=candidate_id,
        repo_id=REPO_ID,
        thread_id=THREAD_ID,
        cycle_id=1,
        root_input_id=event.event_key,
        source_event_key=event.event_key,
        category="TOOLING",
        fact=identity["fact"],
        durability_reason="Makefile",
        evidence_path="Makefile",
        evidence_start_line=1,
        evidence_end_line=2,
        status=RepoMemoryCandidateStatus.PROPOSED.value,
        created_at="now",
        updated_at="now",
    )
    store.save_repo_memory_candidate(record)
    store.save_repo_memory_candidate(record)
    store.close()

    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    stored = reopened.repo_memory_candidates_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=event.event_key,
        root_input_id=event.event_key,
    )
    assert len(stored) == 1
    assert stored[0].candidate_id == candidate_id
    # A mismatched identity is refused outright.
    with pytest.raises(ValueError, match="does not match"):
        reopened.save_repo_memory_candidate(
            RepoMemoryCandidateRecord(**{**record.__dict__, "candidate_id": "forged"})
        )
    reopened.close()


def test_different_lifecycles_keep_independent_proposals(tmp_path):
    base = {
        "repo_id": REPO_ID,
        "thread_id": THREAD_ID,
        "fact": "Run tests with make test.",
        "evidence_path": "Makefile",
        "evidence_start_line": 1,
        "evidence_end_line": 2,
    }
    first = repo_memory_candidate_id_for(**base, cycle_id=1, root_input_id="deferred-a")
    second = repo_memory_candidate_id_for(
        **base, cycle_id=2, root_input_id="deferred-b"
    )
    assert first != second


def test_execution_agent_still_cannot_write_repository_memory(tmp_path, monkeypatch):
    """The /memories/** and /skills/** write denies survive the new tools."""
    from sweforge.agent import run_task
    from sweforge.context import RepoAgentContext

    captured: dict[str, object] = {}

    def factory(**kwargs):
        captured.update(kwargs)

        class Agent:
            def get_state(self, config):
                return type("Snapshot", (), {"tasks": ()})()

            def invoke(self, state, **kwargs):
                return {"messages": [type("Message", (), {"content": "ok"})()]}

        return Agent()

    monkeypatch.setattr("sweforge.agent.create_deep_agent", factory)
    run_task(
        model="provider:model",
        worktree=str(tmp_path),
        task="t",
        thread_id="thread-1",
        checkpointer=object(),
        memory_store=InMemoryStore(),
        repo_context=RepoAgentContext(
            repo_id=REPO_ID, repo_full_name="example/repo", thread_id="thread-1"
        ),
        unsafe_local_shell=True,
        repo_memory_proposal_sink=lambda **kwargs: "recorded",
    )
    denied = {
        path
        for rule in captured["permissions"]
        for path in rule.paths
        if rule.mode == "deny" and "write" in rule.operations
    }
    assert denied == {"/memories/**", "/skills/**"}
    tool_names = {getattr(item, "name", "") for item in captured["tools"]}
    # The proposal tool is additive; it is not a memory write path.
    assert "propose_repo_memory" in tool_names
    assert not any(name.startswith("write") for name in tool_names)


def test_proposal_survives_publication_and_reaches_agents_md(tmp_path):
    """End to end: a nominated unchanged file becomes durable memory."""
    harness = Harness(tmp_path)
    (harness.source / "Makefile").write_text(
        "test:\n\tuv run pytest -q --strict-markers\n"
    )
    subprocess.run(["git", "add", "Makefile"], cwd=harness.source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.com",
            "commit",
            "-qm",
            "makefile",
        ],
        cwd=harness.source,
        check=True,
    )
    origin = source_event(
        harness.repo, "1", "@agent fix the thing", "2026-01-01T00:00:00Z"
    )
    harness.record(origin)
    harness.plan_and_approve(
        event_key=origin.event_key,
        root_input_id=None,
        approval_event=approval(harness.repo, "2", "2026-01-01T01:00:00Z"),
    )
    harness.run_to_publication("touched.txt")
    assert harness.publisher.publish_one().status == "COMPLETED"

    # The agent nominated a fact from a file this task never modified.
    timestamp = "2026-01-02T00:00:00Z"
    harness.store.save_repo_memory_candidate(
        RepoMemoryCandidateRecord(
            candidate_id=repo_memory_candidate_id_for(
                repo_id=harness.repo.repo_id,
                thread_id=THREAD_ID,
                cycle_id=1,
                root_input_id=origin.event_key,
                fact="Run tests with `uv run pytest -q --strict-markers`.",
                evidence_path="Makefile",
                evidence_start_line=1,
                evidence_end_line=2,
            ),
            repo_id=harness.repo.repo_id,
            thread_id=THREAD_ID,
            cycle_id=1,
            root_input_id=origin.event_key,
            source_event_key=origin.event_key,
            category="TOOLING",
            fact="Run tests with `uv run pytest -q --strict-markers`.",
            durability_reason="The Makefile defines the canonical test target.",
            evidence_path="Makefile",
            evidence_start_line=1,
            evidence_end_line=2,
            status=RepoMemoryCandidateStatus.PROPOSED.value,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    harness.engine.memory_learner = None
    memory = InMemoryStore()
    # No curator model: the proposal alone must still close the coverage gap.
    result = harness.engine.advance(
        thread_id=THREAD_ID,
        model="planning-sonnet",
        review_model="review-sonnet",
        memory_model=None,
        repo_paths={harness.repo.full_name: harness.source},
        workspace_root=harness.tmp_path / "workspaces",
        memory_store=memory,
        execute_kwargs=harness.execute_kwargs("touched.txt"),
    )
    assert result.phase is WorkflowPhase.IDLE
    written = (
        read_repo_memory(memory, repo_memory_namespace(harness.repo.repo_id)) or ""
    )
    assert "uv run pytest -q --strict-markers" in written
    learning = harness.store.memory_learning_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert learning.status == "UPDATED"
    stored = harness.store.repo_memory_candidates_for_cycle(
        thread_id=THREAD_ID,
        cycle_id=1,
        root_event_key=origin.event_key,
        root_input_id=None,
    )
    assert stored[0].status == RepoMemoryCandidateStatus.ACCEPTED.value
    harness.store.close()


def test_validator_still_rejects_stale_proposal_evidence(tmp_path):
    """Evidence is re-verified at curation time against the live worktree."""
    root = knowledge_repo(tmp_path)
    candidate = candidate_from_proposal(
        repo_id=REPO_ID,
        worktree=root,
        category="TOOLING",
        fact="Run tests with `uv run pytest -q --strict-markers`.",
        durability_reason="Makefile target.",
        path="Makefile",
        start_line=1,
        end_line=2,
    )
    (root / "Makefile").write_text("test:\n\tsomething-else\n")
    memory = InMemoryStore()
    result = apply_memory_candidates(
        memory,
        repo_id=REPO_ID,
        worktree=root,
        candidates=[candidate],
        lock_root=tmp_path / "locks",
    )
    assert result.status.value == "NO_UPDATE"
    assert result.rejected_candidates == 1


def test_subagents_do_not_receive_the_proposal_tool(tmp_path, monkeypatch):
    """Read-only investigation delegates; nominating durable knowledge does not."""
    from sweforge.agent import run_task
    from sweforge.context import RepoAgentContext

    captured: dict[str, object] = {}

    def factory(**kwargs):
        captured.update(kwargs)

        class Agent:
            def get_state(self, config):
                return type("Snapshot", (), {"tasks": ()})()

            def invoke(self, state, **kwargs):
                return {"messages": [type("Message", (), {"content": "ok"})()]}

        return Agent()

    monkeypatch.setattr("sweforge.agent.create_deep_agent", factory)
    run_task(
        model="provider:model",
        worktree=str(tmp_path),
        task="t",
        thread_id="thread-1",
        checkpointer=object(),
        repo_context=RepoAgentContext(
            repo_id=REPO_ID, repo_full_name="example/repo", thread_id="thread-1"
        ),
        unsafe_local_shell=True,
        repo_memory_proposal_sink=lambda **kwargs: "recorded",
        issue_memory_search=lambda query, limit: "none",
        interrupt_result_sink=lambda payload: None,
    )
    main_tools = {getattr(item, "name", "") for item in captured["tools"]}
    assert {"propose_repo_memory", "search_issue_memory", "request_clarification"} <= (
        main_tools
    )
    subagents = captured["subagents"]
    assert [item["name"] for item in subagents] == ["general-purpose"]
    sub_tools = {getattr(item, "name", "") for item in subagents[0]["tools"]}
    # Read-only historical research is delegated; mutation-adjacent and
    # human-interrupting tools stay with the lifecycle owner.
    assert sub_tools == {"search_issue_memory"}
    assert "propose_repo_memory" not in sub_tools
    assert "request_clarification" not in sub_tools
