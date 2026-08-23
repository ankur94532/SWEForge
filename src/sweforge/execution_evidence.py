"""Durable, provider-neutral observations of completed sandbox commands."""

import hashlib
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

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
        response = self._backend.execute(command, timeout=timeout)
        self._record(command, response)
        return response

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        response = await self._backend.aexecute(command, timeout=timeout)
        self._record(command, response)
        return response
