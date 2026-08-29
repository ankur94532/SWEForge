import subprocess

from sweforge.github_models import RepositoryRef, SourceEvent, SourceKind, SubjectKind
from sweforge.github_publisher import GitHubPublisher, expected_github_remote
from sweforge.github_store import (
    ExecutionPermit,
    PermitSource,
    PublicationStatus,
    RepoMemoryCandidateRecord,
    RepoMemoryCandidateStatus,
    SQLiteGitHubStore,
    ThreadWorkspaceRecord,
    repo_memory_candidate_id_for,
)
from sweforge.repo_memory import (
    SQLiteMemoryStore,
    read_repo_memory,
    repo_memory_namespace,
)
from sweforge.workflow_learning import WorkflowLearningService
from sweforge.workflow_runtime import ValidationVerdict, WorkflowRuntime
from sweforge.workflow_spec import parse_workflow_spec


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
        item = {
            "number": 41,
            "html_url": "https://github.com/example/repo/pull/41",
            "body": body,
        }
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
    assert publication.publication_id == result.publication_id
    assert publication.source_event_key == event_key
    assert publication.root_input_id == event_key
    assert git(remote, "show-ref", "refs/heads/sweforge/issue-7")
    assert publisher.publish_one().status == "NO_WORK"
    store.close()


def test_declarative_multitask_publication_is_one_cumulative_pr(tmp_path):
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
        "-qm",
        "base",
    )
    base = git(source, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    git(source, "worktree", "add", "-q", "-b", "sweforge/issue-8", workspace, base)
    (workspace / "README.md").write_text("A and B completed\n")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)

    store = SQLiteGitHubStore(tmp_path / "state.db")
    repo = RepositoryRef(1, "example/repo")
    store.upsert_repository(1, repo.full_name, "2026-01-01T00:00:00Z")
    event = SourceEvent(
        1,
        repo.full_name,
        SourceKind.ISSUE,
        "declarative",
        "2026-01-01T00:00:00Z",
        SubjectKind.ISSUE,
        8,
        "user",
        "@agent implement A and B",
        None,
    )
    store.record_batch(1, "issues", [event], since="now", etag=None, polled_at="now")
    thread_id = store.source_event(event.event_key)["thread_id"]
    store.save_thread_workspace(
        ThreadWorkspaceRecord(
            thread_id,
            1,
            repo.full_name,
            8,
            str(source),
            str(workspace),
            "sweforge/issue-8",
            base,
            "now",
            "now",
        )
    )

    def raw_task(task_id, dependencies):
        return {
            "id": task_id,
            "depends_on": dependencies,
            "planning": {"skill": "plan", "tools": ["read_file"]},
            "execution": {"skill": "execute", "tools": ["edit_file"]},
            "validation": {"skill": "validate", "tools": ["run_validation"]},
        }

    runtime = WorkflowRuntime(store, clock=lambda: "2026-01-01T00:10:00Z")
    cycle = runtime.initialize_cycle(
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=event.event_key,
        spec=parse_workflow_spec(
            {
                "version": 1,
                "workflow_id": "two-tasks",
                "tasks": [raw_task("A", []), raw_task("B", ["A"])],
            }
        ),
    )
    for index, task_id in enumerate(("A", "B"), start=1):
        task = runtime.select_active_task(cycle.workflow_cycle_id)
        assert task.task_id == task_id
        plan = runtime.submit_posted_plan(
            task_run_id=task.task_run_id,
            plan_text=f"Plan {task_id}",
            posted_comment_id=index,
            posted_at="2026-01-01T00:11:00Z",
        )
        approval_event = SourceEvent(
            1,
            repo.full_name,
            SourceKind.ISSUE_COMMENT,
            f"approval-{task_id}",
            "2026-01-01T00:12:00Z",
            SubjectKind.ISSUE,
            8,
            "maintainer",
            "@agent approve",
            None,
            source_created_at="2026-01-01T00:12:00Z",
        )
        store.record_batch(
            1,
            f"approval-{task_id}",
            [approval_event],
            since="now",
            etag=None,
            polled_at="2026-01-01T00:12:00Z",
        )
        runtime.approve_plan(
            task_run_id=task.task_run_id,
            occurrence_key=plan.approval_occurrence_key,
            approval_event_key=approval_event.event_key,
            approved_by="maintainer",
            approval_is_authorized=True,
            approval_occurred_at="2026-01-01T00:12:00Z",
        )
        runtime.finish_execution(
            task.task_run_id,
            summary=f"Executed {task_id}",
            evidence={
                "reported": {"tests": "passed"},
                "tool_observations": [
                    {"command": "tests", "exit_code": 0, "output": "passed"}
                ],
            },
        )
        runtime.finish_validation(
            task_run_id=task.task_run_id,
            verdict=ValidationVerdict.ACCEPT,
            summary=f"Validated {task_id}",
            findings=[],
            repair_instructions=[],
            evidence={
                "reported": {"tests": "passed"},
                "validation_runs": [{"diff": "", "executions": []}],
            },
        )
    assert runtime.select_active_task(cycle.workflow_cycle_id) is None

    expected_publication = store.eligible_publication_id(thread_id)
    assert expected_publication is not None
    latest_validation = store.connection.execute(
        """SELECT validation_id,evidence_json FROM workflow_task_validations_v1
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    store.connection.execute(
        """UPDATE workflow_task_validations_v1 SET evidence_json='{}'
           WHERE validation_id=?""",
        (latest_validation["validation_id"],),
    )
    store.connection.commit()
    assert store.eligible_publication_id(thread_id) is None
    store.connection.execute(
        "UPDATE workflow_task_validations_v1 SET evidence_json=? WHERE validation_id=?",
        (latest_validation["evidence_json"], latest_validation["validation_id"]),
    )
    permit = store.connection.execute(
        "SELECT permit_id FROM workflow_task_permits_v1 ORDER BY created_at LIMIT 1"
    ).fetchone()
    store.connection.execute(
        """UPDATE workflow_task_permits_v1 SET invalidated_at='tampered'
           WHERE permit_id=?""",
        (permit["permit_id"],),
    )
    store.connection.commit()
    assert store.eligible_publication_id(thread_id) is None
    store.connection.execute(
        "UPDATE workflow_task_permits_v1 SET invalidated_at=NULL WHERE permit_id=?",
        (permit["permit_id"],),
    )
    store.connection.commit()
    assert store.eligible_publication_id(thread_id) == expected_publication

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
    assert len(client.pull_requests_created) == 1
    assert "Executed A" in client.pull_requests_created[0]["body"]
    assert "Executed B" in client.pull_requests_created[0]["body"]
    publication = store.publication_for_id(result.publication_id)
    assert publication is not None
    learning = store.finalize_publication(
        publication.publication_id, now="2026-01-01T00:20:00Z"
    )
    assert learning.thread_id == thread_id
    assert runtime.cycle(cycle.workflow_cycle_id).status.value == "PUBLISHED"
    assert "Executed A" in store.declarative_publication_summary(
        publication.publication_id
    )
    assert "Executed B" in store.declarative_publication_summary(
        publication.publication_id
    )
    candidate_id = repo_memory_candidate_id_for(
        repo_id=1,
        thread_id=thread_id,
        cycle_id=1,
        root_input_id=event.event_key,
        fact="The repository baseline is documented in README.md.",
        evidence_path="README.md",
        evidence_start_line=1,
        evidence_end_line=1,
    )
    store.save_repo_memory_candidate(
        RepoMemoryCandidateRecord(
            candidate_id=candidate_id,
            repo_id=1,
            thread_id=thread_id,
            cycle_id=1,
            root_input_id=event.event_key,
            source_event_key=event.event_key,
            category="convention",
            fact="The repository baseline is documented in README.md.",
            durability_reason="Future changes should inspect the project baseline.",
            evidence_path="README.md",
            evidence_start_line=1,
            evidence_end_line=1,
            status=RepoMemoryCandidateStatus.PROPOSED.value,
            created_at="2026-01-01T00:20:00Z",
            updated_at="2026-01-01T00:20:00Z",
        )
    )
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    learning_service = WorkflowLearningService(
        store=store,
        memory_store=memory.store,
        memory_model=None,
        resolution_model=None,
        lock_root=tmp_path / "locks",
        clock=lambda: "2026-01-01T00:21:00Z",
    )
    assert learning_service.process_one(thread_id)
    learned = read_repo_memory(memory.store, repo_memory_namespace(1))
    assert learned is not None and "repository baseline" in learned
    assert store.repo_memory_candidate(candidate_id).status == "ACCEPTED"
    assert learning_service.process_one(thread_id)
    assert store.pending_memory_learning(thread_id) is None
    assert store.pending_issue_resolution(thread_id) is None
    memory.close()
    store.close()


def test_publication_state_survives_reopen(tmp_path):
    store, event_key, _ = setup_publication(tmp_path)
    store.close()
    reopened = SQLiteGitHubStore(tmp_path / "state.db")
    publication = reopened.next_publication()
    assert publication and publication.source_event_key == event_key
    reopened.close()


def test_failed_publication_requires_explicit_retry(tmp_path):
    store, event_key, _ = setup_publication(tmp_path)
    publication = store.next_publication()
    assert publication
    publication_id = publication.publication_id
    store.update_publication(
        publication_id,
        status=PublicationStatus.FAILED,
        now="failed",
        error_message="ambiguous remote state",
    )
    assert store.next_publication() is None
    assert (
        store.retry_publication(publication_id, now="retry").status
        == PublicationStatus.PENDING
    )
    assert store.next_publication().publication_id == publication_id
    assert store.resolve_publication_id(event_key) == publication_id
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
