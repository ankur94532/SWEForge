"""Crash-safe publication of successful IssueThread executions to GitHub."""

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .execution import ThreadLockUnavailable, thread_lock
from .github_auth import REPO_WRITE, GitHubTokenProvider
from .github_client import GitHubClient
from .github_store import (
    PublicationRecord,
    PublicationStatus,
    SQLiteGitHubStore,
)
from .workspace import WorkspaceError

DEFAULT_GIT_NAME = "SWEForge"
DEFAULT_GIT_EMAIL = "sweforge@users.noreply.github.com"


def publication_comment_marker(publication_id: str) -> str:
    """Deterministic marker scoping a publication comment to one lifecycle."""
    return f"<!-- sweforge:publication:{publication_id} -->"


def publication_comment_markers(publication: PublicationRecord) -> tuple[str, ...]:
    """Markers a lifecycle may own, including its pre-migration event marker."""
    markers = [publication_comment_marker(publication.publication_id)]
    if publication.root_input_id == publication.source_event_key:
        markers.append(f"<!-- sweforge:publication:{publication.source_event_key} -->")
    return tuple(markers)


@dataclass(frozen=True)
class PublicationResult:
    status: str
    publication_id: str | None = None
    error: str | None = None
    source_event_key: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class GitHubPublisher:
    def __init__(
        self,
        *,
        store: SQLiteGitHubStore,
        client: GitHubClient,
        token_provider: GitHubTokenProvider,
        lock_root: str | Path,
        git_name: str = DEFAULT_GIT_NAME,
        git_email: str = DEFAULT_GIT_EMAIL,
        remote_url_factory: Callable[[str], str] | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.store = store
        self.client = client
        self.token_provider = token_provider
        self.lock_root = Path(lock_root).expanduser()
        self.git_name = git_name
        self.git_email = git_email
        self.remote_url_factory = remote_url_factory or (
            lambda full_name: expected_github_remote(api_url, full_name)
        )

    def publish_one(self, publication_id: str | None = None) -> PublicationResult:
        publication = self.store.next_publication(publication_id)
        if publication is None:
            return PublicationResult("NO_WORK", publication_id)
        try:
            with thread_lock(self.lock_root, publication.thread_id):
                return self._publish(publication)
        except ThreadLockUnavailable:
            return self._result("BUSY", publication)
        except Exception as exc:
            error = self._safe_error(exc)
            self.store.update_publication(
                publication.publication_id,
                status=PublicationStatus.FAILED,
                now=_now(),
                error_message=error,
            )
            return self._result("FAILED", publication, error)

    @staticmethod
    def _result(
        status: str, publication: PublicationRecord, error: str | None = None
    ) -> PublicationResult:
        return PublicationResult(
            status,
            publication.publication_id,
            error,
            publication.source_event_key,
        )

    def _publish(self, publication: PublicationRecord) -> PublicationResult:
        if not self.store.publication_is_eligible(publication.publication_id):
            raise WorkspaceError(
                "publication is not authorized by an ACCEPT execution review"
            )
        workspace = self.store.thread_workspace(publication.thread_id)
        execution = self.store.execution_for_cycle(
            thread_id=publication.thread_id,
            cycle_id=publication.cycle_id,
            root_event_key=publication.source_event_key,
            root_input_id=publication.root_input_id,
        )
        if workspace is None or execution is None or execution["status"] != "SUCCEEDED":
            raise WorkspaceError("successful execution workspace is unavailable")
        path = Path(workspace.workspace_path).expanduser().resolve()
        if not path.is_dir():
            raise WorkspaceError("persisted workspace directory is missing")
        self._verify_workspace(path, workspace.branch_name, workspace.base_commit)

        start_head_sha = execution["start_head_sha"]
        end_head_sha = execution["end_head_sha"]
        if not start_head_sha or not end_head_sha:
            raise WorkspaceError("execution is missing its Git baseline")
        current_head_sha = self._git(path, "rev-parse", "HEAD")
        current_dirty = bool(self._git(path, "status", "--porcelain"))
        if (
            end_head_sha == start_head_sha
            and not execution["end_dirty"]
            and current_head_sha == start_head_sha
        ):
            if current_dirty:
                raise WorkspaceError("workspace became dirty after execution")
            self.store.update_publication(
                publication.publication_id,
                status=PublicationStatus.NO_CHANGES,
                now=_now(),
            )
            return self._result("NO_CHANGES", publication)

        changed = self._changed_files(path, start_head_sha)
        if not changed:
            self.store.update_publication(
                publication.publication_id,
                status=PublicationStatus.NO_CHANGES,
                now=_now(),
            )
            return self._result("NO_CHANGES", publication)
        self._validate_paths(changed)

        commit_sha = publication.local_commit_sha
        if commit_sha is None:
            if self._git(path, "status", "--porcelain"):
                self._git(path, "add", "-A", "--", ".")
                self._git(
                    path,
                    "-c",
                    f"user.name={self.git_name}",
                    "-c",
                    f"user.email={self.git_email}",
                    "commit",
                    "-m",
                    f"sweforge: address issue #{publication.issue_number}",
                )
            commit_sha = self._git(path, "rev-parse", "HEAD")
            if commit_sha == workspace.base_commit:
                raise WorkspaceError("changed files did not produce a commit")
            publication = self.store.update_publication(
                publication.publication_id,
                status=PublicationStatus.COMMITTED,
                now=_now(),
                local_commit_sha=commit_sha,
            )
        else:
            self._git(path, "merge-base", "--is-ancestor", commit_sha, "HEAD")

        remote = self.remote_url_factory(publication.repo_full_name)
        token = self.token_provider.token_for(publication.repo_full_name, REPO_WRITE)
        remote_sha = self._push(
            path, remote, publication.branch_name, commit_sha, token
        )
        if self._git(path, "status", "--porcelain"):
            raise WorkspaceError("workspace is dirty after publication commit")
        publication = self.store.update_publication(
            publication.publication_id,
            status=PublicationStatus.PUSHED,
            now=_now(),
            remote_commit_sha=remote_sha,
        )

        repo = self.client.repository(publication.repo_full_name)
        pr_number, pr_url = publication.pr_number, publication.pr_url
        if pr_number is None:
            prs = self.client.pull_requests(
                repo, head=publication.branch_name, base=repo.default_branch
            )
            if len(prs) > 1:
                raise WorkspaceError(
                    "multiple matching GitHub pull requests are ambiguous"
                )
            if prs:
                pr_number, pr_url = int(prs[0]["number"]), prs[0].get("html_url")
            else:
                body = (execution["response_text"] or "SWEForge execution completed.")[
                    :12000
                ]
                created = self.client.create_pull_request(
                    repo,
                    head=publication.branch_name,
                    base=repo.default_branch,
                    title=f"SWEForge: address issue #{publication.issue_number}",
                    body=body,
                )
                pr_number, pr_url = int(created["number"]), created.get("html_url")
            publication = self.store.update_publication(
                publication.publication_id,
                status=PublicationStatus.PR_CREATED,
                now=_now(),
                pr_number=pr_number,
                pr_url=pr_url,
            )
        self.store.register_pr_mapping(
            publication.repo_id, pr_number, publication.thread_id
        )

        marker = publication_comment_marker(publication.publication_id)
        if publication.comment_id is not None:
            comment_id = publication.comment_id
        else:
            comments = self.client.comments(repo, publication.issue_number)
            matching = [
                item
                for item in comments
                if any(
                    token in (item.get("body") or "")
                    for token in publication_comment_markers(publication)
                )
            ]
            if len(matching) > 1:
                raise WorkspaceError("multiple publication comments are ambiguous")
            if matching:
                comment_id = int(matching[0]["id"])
            else:
                comment = self.client.create_comment(
                    repo,
                    publication.issue_number,
                    f"{marker}\nSWEForge published PR #{pr_number}: "
                    f"{pr_url or '(URL unavailable)'}",
                )
                comment_id = int(comment["id"])
        self.store.update_publication(
            publication.publication_id,
            status=PublicationStatus.COMMENTED,
            now=_now(),
            comment_id=comment_id,
        )
        self.store.update_publication(
            publication.publication_id,
            status=PublicationStatus.COMPLETED,
            now=_now(),
        )
        return self._result("COMPLETED", publication)

    def _push(
        self, path: Path, remote: str, branch: str, local_sha: str, token: str
    ) -> str:
        remote_result = self._git_with_auth(
            path, token, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
        )
        remote_sha = remote_result.split()[0] if remote_result.strip() else ""
        if remote_sha and remote_sha != local_sha:
            check = subprocess.run(
                ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
                cwd=path,
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                raise WorkspaceError(
                    "remote IssueThread branch has diverged; refusing to force-push"
                )
        if remote_sha != local_sha:
            self._git_with_auth(
                path, token, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}"
            )
            remote_sha = self._git_with_auth(
                path, token, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
            ).split()[0]
        return remote_sha

    @staticmethod
    def _verify_workspace(path: Path, branch: str, base: str) -> None:
        if GitHubPublisher._git(path, "branch", "--show-current") != branch:
            raise WorkspaceError("workspace branch does not match persisted metadata")
        GitHubPublisher._git(path, "merge-base", "--is-ancestor", base, "HEAD")

    @staticmethod
    def _changed_files(path: Path, base: str) -> list[str]:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--find-renames", base, "--"],
            cwd=path,
            capture_output=True,
            check=True,
        )
        names = [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=path,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        for item in status[:-1]:
            if len(item) >= 3 and item[:2] == b"??":
                names.append(os.fsdecode(item[3:]))
        return sorted(set(names))

    @staticmethod
    def _validate_paths(paths: list[str]) -> None:
        forbidden = (".env", ".pem", ".key", ".sqlite", ".db")
        for value in paths:
            path = Path(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or any(part == ".sweforge" for part in path.parts)
            ):
                raise WorkspaceError(f"refusing unsafe publication path: {value}")
            if path.name in forbidden or path.suffix in forbidden:
                raise WorkspaceError(
                    f"refusing credential or runtime artifact: {value}"
                )

    @staticmethod
    def _git(path: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise WorkspaceError(
                f"git operation failed: {result.stderr.strip() or 'unknown error'}"
            )
        return result.stdout.strip()

    @staticmethod
    def _git_with_auth(path: Path, token: str, *args: str) -> str:
        script = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="sweforge-askpass-", mode="w", delete=False
            ) as handle:
                script = Path(handle.name)
                handle.write(
                    "#!/bin/sh\n"
                    "case \"$1\" in *Username*) printf '%s\\n' x-access-token;; "
                    "*) printf '%s\\n' \"$GIT_HTTP_TOKEN\";; esac\n"
                )
            script.chmod(0o700)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "GIT_ASKPASS": str(script),
                "GIT_HTTP_TOKEN": token,
                "GIT_TERMINAL_PROMPT": "0",
            }
            result = subprocess.run(
                ["git", *args],
                cwd=path,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if result.returncode:
                error = result.stderr.replace(token, "[REDACTED]")
                raise WorkspaceError(
                    f"git remote operation failed: {error.strip() or 'unknown error'}"
                )
            return result.stdout.strip()
        finally:
            if script is not None:
                script.unlink(missing_ok=True)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"


def expected_github_remote(api_url: str, full_name: str) -> str:
    """Build the credential-free HTTPS remote from the configured API host."""
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("GitHub API URL must be an HTTP(S) URL with a host")
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v3"):
        path = path[:-7]
    host = "github.com" if parsed.netloc == "api.github.com" else parsed.netloc
    return f"https://{host}{path}/{full_name}.git"
