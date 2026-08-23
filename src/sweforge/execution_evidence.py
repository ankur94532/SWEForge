"""Durable, provider-neutral observations of completed sandbox commands."""

import hashlib
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
    _apply_grep_max_count,
    _method_accepts_max_count,
    execute_accepts_timeout,
)

MAX_COMMAND_CHARS = 4_000
MAX_OUTPUT_CHARS = 12_000
MAX_ATTEMPT_OUTPUT_CHARS = 96_000

_SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization|private[_-]?key)"
    r"\s*([=:])\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)


def _secret_values() -> tuple[str, ...]:
    names = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "SWEFORGE_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    )
    return tuple(value for name in names if (value := os.environ.get(name)))


def sanitize(value: str) -> str:
    result = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _SECRET_NAME.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", result
    )
    for secret in _secret_values():
        result = result.replace(secret, "[REDACTED_SECRET]")
    return result


def bounded_excerpt(value: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n...[output bounded]...\n"
    if limit <= len(marker):
        return value[:limit], True
    available = max(0, limit - len(marker))
    head = available // 2
    tail = available - head
    return value[:head] + marker + value[-tail:], True


class RecordingSandboxBackend:
    """Delegate all sandbox operations while recording completed execute calls."""

    def __init__(self, backend: SandboxBackendProtocol, sink: Callable[..., Any]):
        self._backend = backend
        self._sink = sink
        self._attempt_output_chars = 0

    @property
    def id(self):
        return self._backend.id

    def __getattr__(self, name: str):
        return getattr(self._backend, name)

    def ls(self, path: str) -> LsResult:
        return self._backend.ls(path)

    async def als(self, path: str) -> LsResult:
        return await self._backend.als(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend.read(file_path, offset=offset, limit=limit)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        return await self._backend.aread(file_path, offset=offset, limit=limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        if _method_accepts_max_count(type(self._backend), "grep"):
            result = self._backend.grep(
                pattern, path=path, glob=glob, max_count=max_count
            )
        else:
            result = self._backend.grep(pattern, path=path, glob=glob)
        return _apply_grep_max_count(result, max_count)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        if _method_accepts_max_count(type(self._backend), "agrep"):
            result = await self._backend.agrep(
                pattern, path=path, glob=glob, max_count=max_count
            )
        else:
            result = await self._backend.agrep(pattern, path=path, glob=glob)
        return _apply_grep_max_count(result, max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._backend.glob(pattern, path=path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await self._backend.aglob(pattern, path=path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._backend.write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await self._backend.awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self._backend.edit(file_path, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return await self._backend.aedit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return self._backend.delete(file_path)

    async def adelete(self, file_path: str) -> DeleteResult:
        return await self._backend.adelete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._backend.upload_files(files)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return await self._backend.aupload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend.download_files(paths)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._backend.adownload_files(paths)

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        return self._backend.execute_with_offload(
            command,
            capture_path,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
            timeout=timeout,
        )

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        return await self._backend.aexecute_with_offload(
            command,
            capture_path,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
            timeout=timeout,
        )

    def _record(self, command: str, response: ExecuteResponse) -> None:
        safe_command, command_truncated = bounded_excerpt(
            sanitize(command), MAX_COMMAND_CHARS
        )
        sanitized_output = sanitize(response.output)
        remaining = max(0, MAX_ATTEMPT_OUTPUT_CHARS - self._attempt_output_chars)
        safe_output, output_truncated = bounded_excerpt(
            sanitized_output, min(MAX_OUTPUT_CHARS, remaining)
        )
        self._attempt_output_chars += len(safe_output)
        self._sink(
            command=safe_command,
            exit_code=response.exit_code,
            output=safe_output,
            output_hash=hashlib.sha256(sanitized_output.encode()).hexdigest(),
            truncated=bool(response.truncated or command_truncated or output_truncated),
            recorded_at=datetime.now(UTC).isoformat(),
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if timeout is not None and execute_accepts_timeout(type(self._backend)):
            response = self._backend.execute(command, timeout=timeout)
        else:
            response = self._backend.execute(command)
        self._record(command, response)
        return response

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        if timeout is not None and execute_accepts_timeout(type(self._backend)):
            response = await self._backend.aexecute(command, timeout=timeout)
        else:
            response = await self._backend.aexecute(command)
        self._record(command, response)
        return response
