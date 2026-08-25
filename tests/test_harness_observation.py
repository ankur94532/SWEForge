"""One predicate must serve both layers, so both fact sources share an interface."""

import subprocess
from pathlib import Path

import pytest
from harness.github_fake import FakeGitHub
from harness.observation import (
    GitFacts,
    GitHubFacts,
    LedgerGitHubFacts,
    Observation,
    RestGitHubFacts,
)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    (path / "README.md").write_text("initial\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-m", "initial", cwd=path)
    return path


def test_both_fact_sources_satisfy_the_same_interface():
    assert isinstance(LedgerGitHubFacts(FakeGitHub()), GitHubFacts)
    assert isinstance(RestGitHubFacts(None, None, 1, "main"), GitHubFacts)


def test_ledger_facts_distinguish_attempts_from_final_state():
    """Reusing a PR and creating a second both leave one PR; attempts differ."""
    fake = FakeGitHub()
    r = fake.repository("example/repo")
    fake.create_pull_request(
        r, head="sweforge/issue-7", base="main", title="t", body="b"
    )
    facts = LedgerGitHubFacts(fake)
    assert len(facts.pull_requests()) == 1
    assert facts.create_pull_request_calls() == 1
    fake.pull_requests(r, head="sweforge/issue-7", base="main")  # reconcile, not create
    assert len(facts.pull_requests()) == 1
    assert facts.create_pull_request_calls() == 1


def test_ledger_facts_count_inline_replies_as_comment_creations():
    fake = FakeGitHub()
    r = fake.repository("example/repo")
    fake.create_comment(r, 7, "one")
    fake.create_review_comment_reply(r, 7, 3, "two")
    assert LedgerGitHubFacts(fake).create_comment_calls() == 2


def test_marker_matching_finds_publication_comments():
    fake = FakeGitHub()
    r = fake.repository("example/repo")
    fake.create_comment(r, 7, "<!-- sweforge:publication:p1 --> done")
    fake.create_comment(r, 7, "unrelated")
    facts = LedgerGitHubFacts(fake)
    assert len(facts.comments_matching("sweforge:publication:p1")) == 1


def test_rest_facts_refuse_to_invent_attempt_counts():
    """REST reports what exists, not how many times creation was tried."""
    facts = RestGitHubFacts(None, None, 1, "main")
    with pytest.raises(NotImplementedError, match="event log"):
        facts.create_pull_request_calls()
    with pytest.raises(NotImplementedError, match="event log"):
        facts.create_comment_calls()


def test_git_facts_read_a_real_repository(repo):
    facts = GitFacts(repo=repo)
    assert facts.branches() == ["main"]
    assert facts.commit_messages() == ["initial"]
    assert facts.is_clean()
    (repo / "new.txt").write_text("x\n")
    assert not facts.is_clean()


def test_git_facts_refuse_to_report_force_when_it_cannot_observe_it(repo):
    """An empty list would read as 'no force-push happened'."""
    facts = GitFacts(repo=repo)
    assert not facts.forced_updates_available()
    with pytest.raises(RuntimeError, match="BRANCH_PUSHED.forced"):
        facts.forced_updates("refs/heads/main")


def test_git_facts_detect_a_forced_update_on_the_origin(repo, tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    # Bare repositories disable reflog by default; force is unobservable without it.
    _git("config", "core.logAllRefUpdates", "true", cwd=origin)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    facts = GitFacts(repo=repo, origin=origin)
    assert facts.forced_updates_available()
    assert facts.forced_updates("refs/heads/main") == []
    # Rewrite history and force-push: the origin ref moves non-fast-forward.
    (repo / "README.md").write_text("rewritten\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "--amend", "-m", "rewritten", cwd=repo)
    _git("push", "--force", "origin", "main", cwd=repo)
    assert facts.forced_updates("refs/heads/main")


def test_observation_filters_events_by_kind_and_thread():
    obs = Observation(
        events=[
            {"kind": "PLAN_CREATED", "thread_id": "t1"},
            {"kind": "PERMIT_CREATED", "thread_id": "t1"},
            {"kind": "PLAN_CREATED", "thread_id": "t2"},
        ]
    )
    assert len(obs.events_of("PLAN_CREATED")) == 2
    assert len(obs.events_for_thread("t1")) == 2
