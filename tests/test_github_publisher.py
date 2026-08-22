import subprocess

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_publisher import GitHubPublisher, expected_github_remote
from sweforge.github_store import (
    ExecutionPermit,
    PermitSource,
    PublicationStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
)


class TokenProvider:
    def token_for(self, repository, profile):
        return "installation-token"


class Client:
    def __init__(self):
        self.comments_created = []
        self.pull_requests_created = []

    def repository(self, full_name):
        return RepositoryRef(1, full_name, "main")

    def pull_requests(self, repo, *, head, base):
        return list(self.pull_requests_created)

    def create_pull_request(self, repo, *, head, base, title, body):
        item = {"number": 41, "html_url": "https://github.com/example/repo/pull/41"}
        self.pull_requests_created.append(item)
        return item

    def comments(self, repo, number):
        return list(self.comments_created)

    def create_comment(self, repo, number, body):
        item = {"id": 99, "body": body}
        self.comments_created.append(item)
        return item


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def setup_publication(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("base\n")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "base",
    )
    base = git(source, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    git(source, "worktree", "add", "-q", "-b", "sweforge/issue-7", str(workspace), base)
    (workspace / "README.md").write_text("published\n")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/repo")
    store.upsert_repository(1, repo.full_name, "now")
    event = SourceEvent(
        1,
        repo.full_name,
        SourceKind.ISSUE,
        "1",
        "2026-01-01T00:00:00Z",
        SubjectKind.ISSUE,
        7,
        "user",
        "@agent fix",
        None,
    )
    store.record_batch(1, "issues", [event], since="now", etag=None, polled_at="now")
    claim = store.claim_next_event(now="now")
    assert claim
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            claim.thread_id,
            1,
            repo.full_name,
            7,
            str(source),
            str(workspace),
            "sweforge/issue-7",
            base,
            "now",
            "now",
        )
    )
    store.mark_execution_succeeded(
        claim.event_key,
        completed_at="now",
        response_text="done",
        workspace_path=str(workspace),
        start_head_sha=base,
        end_head_sha=base,
        end_dirty=True,
    )
    plan_id = "plan-reviewed"
    store.connection.execute(
        """INSERT INTO issue_plans(
           plan_id,thread_id,repo_id,repo_full_name,issue_number,cycle_id,version,
           root_event_key,plan_text,status,created_at,posted_at,approved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan_id,
            claim.thread_id,
            1,
            repo.full_name,
            7,
            1,
            1,
            claim.event_key,
            "reviewed",
            "APPROVED",
            "now",
            "now",
            "now",
        ),
    )
    store.connection.execute(
        """INSERT INTO issue_workflow_state(
           thread_id,repo_id,repo_full_name,issue_number,phase,cycle_id,root_event_key,
           current_plan_id,mode,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            claim.thread_id,
            1,
            repo.full_name,
            7,
            "AWAITING_PUBLICATION",
            1,
            claim.event_key,
            plan_id,
            "INTERACTIVE",
            "now",
            "now",
        ),
    )
    store.connection.execute(
        """INSERT INTO execution_attempts(
           attempt_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,
           attempt_number,kind,authorization_id,status,created_at,completed_at,
           start_head_sha,end_head_sha,end_dirty)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "attempt-reviewed",
            claim.thread_id,
            1,
            plan_id,
            1,
            claim.event_key,
            1,
            "INITIAL",
            "permit-reviewed",
            "SUCCEEDED",
            "now",
            "now",
            base,
            base,
            1,
        ),
    )
    store.connection.execute(
        """INSERT INTO execution_reviews(
           review_id,thread_id,cycle_id,plan_id,plan_version,root_event_key,attempt_id,
           review_iteration,verdict,summary,findings_json,repair_instructions_json,
           created_at,completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "review-reviewed",
            claim.thread_id,
            1,
            plan_id,
            1,
            claim.event_key,
            "attempt-reviewed",
            1,
            "ACCEPT",
            "good",
            "[]",
            "[]",
            "now",
            "now",
        ),
    )
    store.connection.commit()
    store.insert_permit(
        ExecutionPermit(
            permit_id="permit-reviewed",
            thread_id=claim.thread_id,
            cycle_id=1,
            plan_id=plan_id,
            plan_version=1,
            root_event_key=claim.event_key,
            source=PermitSource.USER,
            source_event_key=None,
            created_at="now",
            consumed_at="now",
            invalidated_at=None,
        )
    )
    store.connection.commit()
    return store, event.event_key, remote


def test_publish_commits_pushes_and_reconciles_comment(tmp_path):
    store, event_key, remote = setup_publication(tmp_path)
    client = Client()
    publisher = GitHubPublisher(
        store=store,
        client=client,
        token_provider=TokenProvider(),
        lock_root=tmp_path / "locks",
        remote_url_factory=lambda _: f"file://{remote}",
    )

    result = publisher.publish_one()
    assert result.status == "COMPLETED", result.error
    publication = store.publication_for_event(event_key)
    assert publication and publication.local_commit_sha and publication.pr_number == 41
    assert git(remote, "show-ref", "refs/heads/sweforge/issue-7")
    assert publisher.publish_one().status == "NO_WORK"
    store.close()


def test_publication_state_survives_reopen(tmp_path):
    store, event_key, _ = setup_publication(tmp_path)
    store.close()
    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    publication = reopened.next_publication()
    assert publication and publication.event_key == event_key
    reopened.close()


def test_failed_publication_requires_explicit_retry(tmp_path):
    store, event_key, _ = setup_publication(tmp_path)
    publication = store.next_publication()
    assert publication
    store.update_publication(
        event_key,
        status=PublicationStatus.FAILED,
        now="failed",
        error_message="ambiguous remote state",
    )
    assert store.next_publication() is None
    assert (
        store.retry_publication(event_key, now="retry").status
        == PublicationStatus.PENDING
    )
    assert store.next_publication().event_key == event_key
    store.close()


def test_follow_up_without_changes_does_not_republish_old_commit(tmp_path):
    store, first_key, remote = setup_publication(tmp_path)
    client = Client()
    publisher = GitHubPublisher(
        store=store,
        client=client,
        token_provider=TokenProvider(),
        lock_root=tmp_path / "locks",
        remote_url_factory=lambda _: f"file://{remote}",
    )
    assert publisher.publish_one().status == "COMPLETED"
    workspace = tmp_path / "workspace"
    published_sha = git(workspace, "rev-parse", "HEAD")
    second = SourceEvent(
        1,
        "example/repo",
        SourceKind.ISSUE,
        "2",
        "2026-01-01T00:01:00Z",
        SubjectKind.ISSUE,
        7,
        "user",
        "@agent follow up",
        None,
    )
    store.record_batch(1, "issues", [second], since="now", etag=None, polled_at="later")
    claim = store.claim_next_event(now="later")
    assert claim and claim.event_key != first_key
    store.mark_execution_succeeded(
        claim.event_key,
        completed_at="later",
        response_text="no changes",
        workspace_path=str(workspace),
        start_head_sha=published_sha,
        end_head_sha=published_sha,
        end_dirty=False,
    )
    assert publisher.publish_one().status == "NO_WORK"
    assert git(workspace, "rev-parse", "HEAD") == published_sha
    assert len(client.pull_requests_created) == 1
    store.close()


def test_expected_github_remote_uses_git_host_for_public_api():
    assert (
        expected_github_remote("https://api.github.com", "example/repo")
        == "https://github.com/example/repo.git"
    )
    assert (
        expected_github_remote("https://github.example/api/v3", "example/repo")
        == "https://github.example/example/repo.git"
    )
