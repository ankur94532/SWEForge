import subprocess

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_publisher import GitHubPublisher
from sweforge.github_store import SQLiteGitHubStore, ThreadWorkspaceRecord


class TokenProvider:
    def token_for(self, repository, profile):
        return "installation-token"


class Client:
    def __init__(self):
        self.comments_created = []

    def repository(self, full_name):
        return RepositoryRef(1, full_name, "main")

    def pull_requests(self, repo, *, head, base):
        return []

    def create_pull_request(self, repo, *, head, base, title, body):
        return {"number": 41, "html_url": "https://github.com/example/repo/pull/41"}

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
    )
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
