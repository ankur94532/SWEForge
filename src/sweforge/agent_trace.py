"""Optional bounded, redacted live tracing for durable agent workflows."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Protocol, TextIO
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage

MODEL_TEXT_LIMIT = 4_000
TOOL_ARGUMENT_LIMIT = 2_000
TOOL_RESULT_LIMIT = 4_000
TRACE_DETAIL_LIMIT = 2_000
TRACE_ERROR_LIMIT = 2_000

_SECRET_ENV_NAMES = (
    "SWEFORGE_GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|private[_-]?key|password|secret)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;}]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_AUTHENTICATED_URL = re.compile(r"(https?://)([^/\s:@]+)(?::[^@\s/]*)?@")
_OBVIOUS_TOKEN = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{8,})\b"
)
_LIFECYCLE_TOOLS = {
    "submit_plan",
    "finish_execution",
    "finish_validation",
    "request_clarification",
}


def redact_text(value: object) -> str:
    """Redact known credentials and common token-bearing text forms."""
    text = str(value)
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _AUTHENTICATED_URL.sub(r"\1[REDACTED]@", text)
    return _OBVIOUS_TOKEN.sub("[REDACTED]", text)


def bounded_text(value: object, limit: int) -> str:
    """Return visibly bounded text without silently dropping content."""
    text = redact_text(value)
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    marker = f" ... [truncated {omitted} chars]"
    kept = max(0, limit - len(marker))
    omitted = len(text) - kept
    marker = f" ... [truncated {omitted} chars]"
    kept = max(0, limit - len(marker))
    return f"{text[:kept]}{marker}"[:limit]


def _safe_payload(value: Any, *, depth: int = 0) -> Any:
    """Convert callback payloads without invoking arbitrary object reprs."""
    if depth >= 4:
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:25]:
            name = str(key)
            rendered[name] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(name)
                else _safe_payload(item, depth=depth + 1)
            )
        if len(items) > 25:
            rendered["..."] = f"[{len(items) - 25} entries omitted]"
        return rendered
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
        rendered = [_safe_payload(item, depth=depth + 1) for item in items[:25]]
        if len(items) > 25:
            rendered.append(f"[{len(items) - 25} entries omitted]")
        return rendered
    return f"<{type(value).__name__}>"


def render_payload(value: Any, *, limit: int) -> str:
    """Render a callback payload through the common redaction/bounding path."""
    safe = _safe_payload(value)
    if isinstance(safe, str):
        return bounded_text(safe, limit)
    return bounded_text(json.dumps(safe, sort_keys=True), limit)


def observable_message_text(message: Any) -> str:
    """Extract only provider-visible assistant text, never reasoning blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    blocks = getattr(message, "content_blocks", content)
    if not isinstance(blocks, (list, tuple)):
        return ""
    visible: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping):
            if block.get("type") in {"text", "plain_text"} and isinstance(
                block.get("text"), str
            ):
                visible.append(block["text"])
        elif getattr(block, "type", None) in {"text", "plain_text"}:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                visible.append(text)
    return "\n".join(visible)


@dataclass(frozen=True, slots=True)
class TraceContext:
    thread_id: str = ""
    repo: str = ""
    issue_number: int | None = None
    origin_surface: str = ""
    subject_number: int | None = None
    workflow_cycle_id: str = ""
    cycle_id: int | None = None
    task_id: str = ""
    task_run_id: str = ""
    phase: str = ""
    model_role: str = ""
    model: str = ""

    def with_values(self, **values: Any) -> TraceContext:
        return replace(self, **values)


@dataclass(frozen=True, slots=True)
class AgentTraceEvent:
    category: str
    message: str
    context: TraceContext


class AgentTraceSink(Protocol):
    def emit(self, event: AgentTraceEvent) -> None: ...


class TerminalTraceSink:
    """Render complete prefixed events atomically to stderr by default."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self._write_lock = RLock()

    def emit(self, event: AgentTraceEvent) -> None:
        prefix = self._prefix(event.context)
        lines = event.message.splitlines() or [""]
        rendered = "\n".join(
            f"{prefix} {event.category}: {line}".rstrip() for line in lines
        )
        with self._write_lock:
            self.stream.write(rendered + "\n")
            self.stream.flush()

    @staticmethod
    def _prefix(context: TraceContext) -> str:
        fields: list[tuple[str, object]] = [
            ("thread", context.thread_id),
            ("repo", context.repo),
            ("issue", context.issue_number),
            ("surface", context.origin_surface),
            ("subject", context.subject_number),
            ("cycle", context.cycle_id),
            ("workflow_cycle", context.workflow_cycle_id),
            ("task", context.task_id),
            ("task_run", context.task_run_id),
            ("phase", context.phase),
            ("model_role", context.model_role),
            ("model", context.model),
        ]
        parts = [
            f"[{name}={bounded_text(value, 180)}]"
            for name, value in fields
            if value not in (None, "")
        ]
        return " ".join(parts) or "[sweforge]"


class AgentTracer:
    """Best-effort observer; sink failures never affect workflow behavior."""

    def __init__(
        self, sink: AgentTraceSink, *, include_tool_payloads: bool = False
    ) -> None:
        self.sink = sink
        self.include_tool_payloads = include_tool_payloads

    def emit(
        self,
        category: str,
        message: object,
        context: TraceContext,
        *,
        limit: int = TRACE_DETAIL_LIMIT,
    ) -> None:
        event = AgentTraceEvent(
            category=category,
            message=bounded_text(message, limit),
            context=context,
        )
        try:
            self.sink.emit(event)
        except Exception:
            # Debugging must never become lifecycle authority or a failure mode.
            return

    def transition(self, context: TraceContext, before: object, after: object) -> None:
        before_value = getattr(before, "value", before)
        after_value = getattr(after, "value", after)
        if before_value != after_value:
            self.emit(
                "WORKFLOW",
                f"{context.task_id or 'task'} {before_value} -> {after_value}",
                context,
            )

    def interrupt(self, context: TraceContext, kind: str, occurrence: str) -> None:
        self.emit("INTERRUPT", f"{kind} {occurrence}", context)

    def resume(self, context: TraceContext, kind: str, occurrence: str) -> None:
        self.emit("RESUME", f"{kind} {occurrence}", context)

    def authorization(
        self, context: TraceContext, mode: str, target: str, actor: str = ""
    ) -> None:
        suffix = f" by {actor}" if actor else ""
        self.emit("AUTHORIZATION", f"{mode} {target}{suffix}", context)


class AgentTraceCallbackHandler(BaseCallbackHandler):
    """Observe completed model calls and tool activity without changing invoke()."""

    def __init__(
        self,
        tracer: AgentTracer,
        *,
        context_provider: Callable[[], TraceContext],
        model_names: Mapping[str, str] | None = None,
    ) -> None:
        self.tracer = tracer
        self.context_provider = context_provider
        self.model_names = dict(model_names or {})
        self._lock = RLock()
        self._models: dict[UUID, TraceContext] = {}
        self._tools: dict[UUID, tuple[TraceContext, str]] = {}

    def _context(self) -> TraceContext:
        try:
            context = self.context_provider()
            return context if isinstance(context, TraceContext) else TraceContext()
        except Exception:
            return TraceContext()

    def _model_context(self) -> TraceContext:
        context = self._context()
        role = {
            "PLANNING": "planning",
            "EXECUTING": "execution",
            "VALIDATING": "validation",
        }.get(context.phase, "workflow")
        return context.with_values(
            model_role=role,
            model=self.model_names.get(role, self.model_names.get("workflow", "")),
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        context = self._model_context()
        with self._lock:
            self._models[run_id] = context
        label = (
            f"{context.model_role}_model={context.model}"
            if context.model
            else f"role={context.model_role}"
        )
        self.tracer.emit("MODEL START", label, context)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        with self._lock:
            context = self._models.pop(run_id, self._model_context())
        seen: set[str] = set()
        for generation_group in getattr(response, "generations", ()) or ():
            for generation in generation_group or ():
                message = getattr(generation, "message", None)
                text = observable_message_text(message) if message is not None else ""
                if text and text not in seen:
                    seen.add(text)
                    self.tracer.emit("MODEL", text, context, limit=MODEL_TEXT_LIMIT)
        self.tracer.emit("MODEL END", "completed", context)

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        del kwargs
        with self._lock:
            context = self._models.pop(run_id, self._model_context())
        self.tracer.emit(
            "MODEL ERROR",
            f"{type(error).__name__}: {error}",
            context,
            limit=TRACE_ERROR_LIMIT,
        )
        self.tracer.emit("MODEL END", "failed", context)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        name = str(serialized.get("name") or "tool")
        context = self._context()
        with self._lock:
            self._tools[run_id] = (context, name)
        category = (
            "SUBAGENT START"
            if name == "task"
            else "LIFECYCLE START"
            if name in _LIFECYCLE_TOOLS
            else "TOOL START"
        )
        self.tracer.emit(category, name, context)
        if self.tracer.include_tool_payloads:
            payload: Any = inputs if inputs is not None else input_str
            self.tracer.emit(
                "TOOL ARGS",
                render_payload(payload, limit=TOOL_ARGUMENT_LIMIT),
                context,
                limit=TOOL_ARGUMENT_LIMIT,
            )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        with self._lock:
            context, name = self._tools.pop(run_id, (self._context(), "tool"))
        if self.tracer.include_tool_payloads:
            value = output.content if isinstance(output, ToolMessage) else output
            self.tracer.emit(
                "TOOL RESULT",
                render_payload(value, limit=TOOL_RESULT_LIMIT),
                context,
                limit=TOOL_RESULT_LIMIT,
            )
        category = (
            "SUBAGENT END"
            if name == "task"
            else "LIFECYCLE END"
            if name in _LIFECYCLE_TOOLS
            else "TOOL END"
        )
        self.tracer.emit(category, name, context)

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        del kwargs
        with self._lock:
            context, name = self._tools.pop(run_id, (self._context(), "tool"))
        self.tracer.emit(
            "TOOL ERROR",
            f"{name}: {type(error).__name__}: {error}",
            context,
            limit=TRACE_ERROR_LIMIT,
        )
