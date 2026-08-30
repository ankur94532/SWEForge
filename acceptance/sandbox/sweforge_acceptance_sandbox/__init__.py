"""Operator-owned macOS Seatbelt sandbox backend for SWEForge acceptance.

This package lives outside SWEForge and outside every target repository. It
exists so acceptance can exercise SWEForge's strict sandbox boundary instead of
falling back to unrestricted local shell execution.

Confinement model (kernel-enforced by /usr/bin/sandbox-exec):

  deny by default

  file CONTENTS readable only from:
      system paths (/usr /bin /sbin /System /Library /opt /private/etc ...)
      the IssueThread worktree
      the trusted checkout's .git admin directory
      the per-user temp/cache dirs and the Maven/Gradle caches

  file WRITES allowed only in:
      the IssueThread worktree
      the trusted checkout's .git admin directory
      per-user temp/cache, ~/.m2, ~/.gradle, /dev

  file METADATA (stat) is allowed host-wide. This is required: getcwd(2) and
  ordinary path resolution fail without it, and Maven's launcher walks the
  ancestor chain. Metadata exposes existence and size only -- never contents.

  network egress is DENIED unless the project needs a local build daemon
  (see `_needs_network`).

Nothing here is a keyword blacklist: denial is enforced by the kernel, and the
allow-list is derived from authoritative values (the worktree SWEForge supplies
and the git admin dir that worktree itself points at), never from model input.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
)
from deepagents.backends.sandbox import BaseSandbox

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DEFAULT_TIMEOUT = 1800
MAX_OUTPUT = 200_000
JAVA_HOME = "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home"
HOST_PATH_ROOT_NAMES = frozenset(
    {"Users", "tmp", "private", "var", "home", "opt", "System", "Volumes"}
)


def _resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _user_dir(name: str) -> str | None:
    """Resolve a DARWIN per-user directory, following /var -> /private/var."""
    try:
        value = subprocess.run(
            ["getconf", name], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return _resolved(value) if value else None


def _git_admin_dir(worktree: Path) -> str | None:
    """Derive the trusted checkout's git dir from the worktree itself.

    A linked worktree's `.git` is a file pointing at
    `<checkout>/.git/worktrees/<name>`; git cannot operate without it. Deriving
    it here keeps the allow-list authoritative rather than configured.
    """
    pointer = worktree / ".git"
    try:
        if pointer.is_dir():
            return _resolved(pointer)
        text = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    for parent in [gitdir, *gitdir.parents]:
        if parent.name == ".git":
            return _resolved(parent)
    return None


def _needs_network(worktree: Path) -> bool:
    """Gradle builds require loopback IPC to their single-use daemon.

    Seatbelt only accepts `*` or `localhost` as a network host filter, and
    `localhost` does not match the daemon's socket, so a Gradle project cannot
    be run with narrower network rules. Maven projects run with egress fully
    denied. This is decided from repository content, never from model input.
    """
    return (worktree / "gradlew").exists()


def build_profile(worktree: Path) -> str:
    """Render the Seatbelt profile for one IssueThread worktree."""
    allow_rw = [_resolved(worktree)]
    git_dir = _git_admin_dir(worktree)
    if git_dir:
        allow_rw.append(git_dir)
    for candidate in (
        _user_dir("DARWIN_USER_TEMP_DIR"),
        _user_dir("DARWIN_USER_CACHE_DIR"),
        _resolved("~/.m2"),
        _resolved("~/.gradle"),
    ):
        if candidate:
            allow_rw.append(candidate)

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow sysctl-read)",
        "(allow mach*)",
        "(allow ipc-posix-shm*)",
        "(allow signal)",
        "(allow process-info*)",
        "(allow file-map-executable)",
    ]
    # Local unix sockets only: the JVM attach protocol (used by Mockito's
    # ByteBuddy agent) connects to /tmp/.java_pid<pid>. This grants no
    # internet egress.
    lines.append("(allow network-bind (local unix-socket))")
    lines.append("(allow network-outbound (remote unix-socket))")
    if _needs_network(worktree):
        lines.append("(allow network*)")
    lines += [
        "; stat/getcwd only; contents stay allow-listed below",
        "(allow file-read-metadata)",
        "(allow file-read*",
        '  (subpath "/usr") (subpath "/bin") (subpath "/sbin")',
        '  (subpath "/System") (subpath "/Library") (subpath "/opt")',
        '  (subpath "/private/etc") (subpath "/private/var/db")',
        '  (subpath "/private/var/select") (literal "/"))',
        '(allow file-read* file-write* (subpath "/dev"))',
        "; JVM attach sockets are hardcoded to /tmp, not TMPDIR. Mockito's",
        "; ByteBuddy agent self-attaches through them. Only those exact names",
        "; are opened, not the shared temp directory.",
        "(allow file-read* file-write*",
        '  (regex #"^/private/tmp/\\.java_pid[0-9]+$")',
        '  (regex #"^/private/tmp/\\.attach_pid[0-9]+$"))',
    ]
    for path in allow_rw:
        lines.append(f'(allow file-read* file-write* (subpath "{path}"))')
    return "\n".join(lines) + "\n"


class SeatbeltSandbox(BaseSandbox):
    """Runs every command under a kernel-enforced Seatbelt profile.

    `BaseSandbox` implements the filesystem tools on top of `execute()`, so the
    model's reads, writes, greps and shell commands all cross the same
    confinement boundary.
    """

    def __init__(self, *, worktree: str | Path, repo_id: int, thread_id: str) -> None:
        self.worktree = Path(_resolved(worktree))
        self.repo_id = int(repo_id)
        self.thread_id = str(thread_id)
        if not self.worktree.is_dir():
            raise RuntimeError(f"sandbox worktree is not a directory: {self.worktree}")
        if not Path(SANDBOX_EXEC).exists():
            raise RuntimeError("sandbox-exec is unavailable on this host")
        self._profile_path = self._write_profile()

    def _write_profile(self) -> Path:
        digest = hashlib.sha256(str(self.worktree).encode()).hexdigest()[:16]
        root = Path(_resolved("~/.sweforge/acceptance-sandbox-profiles"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.repo_id}-{digest}.sb"
        path.write_text(build_profile(self.worktree), encoding="utf-8")
        return path

    def _host_path(self, path: str) -> str:
        """Translate a virtual repository path into the trusted worktree."""
        value = str(path)
        physical_root = str(self.worktree)
        if value == physical_root or value.startswith(physical_root + os.sep):
            relative = value[len(physical_root) :].lstrip(os.sep)
        elif value.startswith("/"):
            relative = value.lstrip("/")
            top_level = relative.split("/", 1)[0]
            if top_level in HOST_PATH_ROOT_NAMES or (
                top_level and not (self.worktree / top_level).exists()
            ):
                raise ValueError("path must use the virtual repository namespace")
        elif value.startswith("~") or os.path.isabs(value):
            raise ValueError("path must use the virtual repository namespace")
        else:
            relative = value
        candidate = (self.worktree / relative).resolve()
        try:
            candidate.relative_to(self.worktree)
        except ValueError as exc:
            raise ValueError("path escapes the sandbox worktree") from exc
        return str(candidate)

    def _canonical_virtual_path(self, path: str) -> str:
        return self._virtual_path(self._host_path(path))

    def _virtual_path(self, path: str) -> str:
        """Translate a provider result path back to the virtual namespace."""
        candidate = Path(path).resolve()
        try:
            relative = candidate.relative_to(self.worktree)
        except ValueError:
            return path
        return "/" + relative.as_posix()

    def _virtual_error(self, error: str | None) -> str | None:
        if error is None:
            return None
        return error.replace(str(self.worktree), "<repository>")

    def _remap_error(self, result):
        result.error = self._virtual_error(result.error)
        return result

    def ls(self, path: str) -> LsResult:
        result = super().ls(self._host_path(path))
        if result.entries:
            result.entries = [
                {**entry, "path": self._virtual_path(entry["path"])}
                for entry in result.entries
            ]
        return self._remap_error(result)

    async def als(self, path: str) -> LsResult:
        result = await super().als(self._host_path(path))
        if result.entries:
            result.entries = [
                {**entry, "path": self._virtual_path(entry["path"])}
                for entry in result.entries
            ]
        return self._remap_error(result)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._remap_error(
            super().read(self._host_path(file_path), offset, limit)
        )

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._remap_error(
            await super().aread(self._host_path(file_path), offset, limit)
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        host_path = self._host_path(path) if path is not None else str(self.worktree)
        result = super().glob(pattern, host_path)
        if result.matches:
            result.matches = [
                {**match, "path": self._virtual_path(match["path"])}
                for match in result.matches
            ]
        return self._remap_error(result)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        host_path = self._host_path(path) if path is not None else str(self.worktree)
        result = await super().aglob(pattern, host_path)
        if result.matches:
            result.matches = [
                {**match, "path": self._virtual_path(match["path"])}
                for match in result.matches
            ]
        return self._remap_error(result)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        host_path = self._host_path(path) if path is not None else None
        result = super().grep(pattern, host_path, glob, max_count=max_count)
        if result.matches:
            result.matches = [
                {**match, "path": self._virtual_path(match["path"])}
                for match in result.matches
            ]
        return self._remap_error(result)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        host_path = self._host_path(path) if path is not None else None
        result = await super().agrep(pattern, host_path, glob, max_count=max_count)
        if result.matches:
            result.matches = [
                {**match, "path": self._virtual_path(match["path"])}
                for match in result.matches
            ]
        return self._remap_error(result)

    def write(self, file_path: str, content: str):
        # BaseSandbox.write delegates to self.upload_files; keep the virtual
        # path here so the override maps it exactly once.
        result = super().write(self._host_path(file_path), content)
        if result.path:
            result.path = self._canonical_virtual_path(file_path)
        return self._remap_error(result)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        result = super().edit(
            self._host_path(file_path), old_string, new_string, replace_all
        )
        if result.path:
            result.path = self._canonical_virtual_path(file_path)
        return self._remap_error(result)

    def delete(self, file_path: str):
        result = super().delete(self._host_path(file_path))
        if result.path:
            result.path = self._canonical_virtual_path(file_path)
        return self._remap_error(result)

    @property
    def id(self) -> str:
        return f"seatbelt:{self.repo_id}:{self.thread_id}"

    @staticmethod
    def _bytebuddy_agent() -> str | None:
        """Newest cached byte-buddy agent jar, if one is present.

        macOS denies `task_for_pid` to any unprivileged sandbox profile, so a
        JVM inside the sandbox cannot self-attach an agent. Mockito's inline
        mock maker relies on exactly that. Preloading the agent with
        `-javaagent` removes the need to self-attach and keeps the inline mock
        maker's semantics unchanged -- no fixture edit, no weakened test.
        """
        root = Path(_resolved("~/.m2/repository/net/bytebuddy/byte-buddy-agent"))
        if not root.is_dir():
            return None
        jars = [
            jar
            for jar in root.glob("*/byte-buddy-agent-*.jar")
            if "sources" not in jar.name and "javadoc" not in jar.name
        ]
        if not jars:
            return None
        return str(sorted(jars)[-1])

    def _env(self) -> dict[str, str]:
        """A deliberately small environment: no credentials reach the sandbox."""
        temp = _user_dir("DARWIN_USER_TEMP_DIR") or "/tmp"
        agent = self._bytebuddy_agent()
        tool_options = f"-javaagent:{agent}" if agent else ""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": os.environ.get("HOME", ""),
            "JAVA_HOME": os.environ.get("JAVA_HOME", JAVA_HOME),
            "TMPDIR": temp,
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "dumb",
            # Inherited by Maven/Gradle forked test JVMs.
            **({"JAVA_TOOL_OPTIONS": tool_options} if tool_options else {}),
        }

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        completed = subprocess.run(
            [SANDBOX_EXEC, "-f", str(self._profile_path), "/bin/bash", "-lc", command],
            cwd=str(self.worktree),
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        truncated = len(output) > MAX_OUTPUT
        return ExecuteResponse(
            output=output[:MAX_OUTPUT],
            exit_code=completed.returncode,
            truncated=truncated,
        )

    def execute_tool(
        self,
        command: list[str],
        *,
        stdin: str,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Run one fixed registered tool without a model-controlled shell."""
        return subprocess.run(
            [SANDBOX_EXEC, "-f", str(self._profile_path), *command],
            cwd=str(self.worktree),
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write through the sandbox so uploads obey the same boundary."""
        results: list[FileUploadResponse] = []
        for virtual_path, content in files:
            import base64

            path = self._host_path(virtual_path)
            encoded = base64.b64encode(content).decode("ascii")
            command = (
                f"mkdir -p {shlex.quote(str(Path(path).parent))} && "
                f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
            )
            response = self.execute(command)
            results.append(
                FileUploadResponse(
                    path=self._canonical_virtual_path(virtual_path),
                    error=None
                    if response.exit_code == 0
                    else (response.output[:200] or "permission_denied"),
                )
            )
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read through the sandbox so downloads obey the same boundary."""
        import base64

        results: list[FileDownloadResponse] = []
        for virtual_path in paths:
            path = self._host_path(virtual_path)
            response = self.execute(f"base64 -i {shlex.quote(path)}")
            if response.exit_code != 0:
                results.append(
                    FileDownloadResponse(
                        path=self._canonical_virtual_path(virtual_path),
                        content=None,
                        error=response.output[:200] or "file_not_found",
                    )
                )
                continue
            try:
                content = base64.b64decode(response.output.strip())
            except ValueError as exc:
                results.append(
                    FileDownloadResponse(
                        path=self._canonical_virtual_path(virtual_path),
                        content=None,
                        error=str(exc)[:200],
                    )
                )
                continue
            results.append(
                FileDownloadResponse(
                    path=self._canonical_virtual_path(virtual_path),
                    content=content,
                    error=None,
                )
            )
        return results


def seatbelt_backend(*, context, worktree: str) -> SeatbeltSandbox:
    """Entry point SWEForge resolves via the `sweforge.sandbox_backends` group.

    Repository identity comes from SWEForge's authoritative `RepoAgentContext`.
    """
    return SeatbeltSandbox(
        worktree=worktree,
        repo_id=context.repo_id,
        thread_id=context.thread_id,
    )
