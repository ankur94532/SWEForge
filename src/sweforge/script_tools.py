"""Repo-generation-bound registered script tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from .agent_trace import AgentTracer, TraceContext
from .repo_config import MAX_SCRIPT_OUTPUT_CHARS, ScriptToolSpec
from .repo_secrets import MIN_SECRET_CHARS, SecretValue


def _validate_value(value: Any, schema: Mapping[str, Any], label: str) -> None:
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, False)
    if not valid:
        raise ValueError(f"script tool argument {label} must be {expected}")


def validate_script_arguments(
    args: Mapping[str, Any], schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the supported bounded JSON-schema subset deterministically."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", ()))
    missing = sorted(required - set(args))
    unknown = sorted(set(args) - set(properties))
    if missing:
        raise ValueError(f"script tool arguments are missing: {missing}")
    if unknown:
        raise ValueError(f"script tool arguments are unknown: {unknown}")
    result = dict(args)
    for name, value in result.items():
        _validate_value(value, properties[name], name)
    return result


def _bounded(value: str) -> str:
    if len(value) <= MAX_SCRIPT_OUTPUT_CHARS:
        return value
    marker = "\n...[script output bounded]...\n"
    available = MAX_SCRIPT_OUTPUT_CHARS - len(marker)
    return value[: available // 2] + marker + value[-(available - available // 2) :]


def redact_injected_secrets(value: object, secrets: list[str]) -> str:
    """Redact exact injected values before any model/log/error boundary."""
    rendered = str(value)
    for secret in secrets:
        if len(secret) >= MIN_SECRET_CHARS:
            rendered = rendered.replace(secret, "[REDACTED]")
    return rendered


class ScriptToolExecutor:
    """Execute fixed trusted scripts for one repo/config/worktree binding."""

    def __init__(
        self,
        *,
        worktree: str | Path,
        files_for: Callable[[ScriptToolSpec], Mapping[str, str]],
        secret_resolver: (
            Callable[[ScriptToolSpec], Mapping[str, SecretValue]] | None
        ) = None,
        sandbox_backend: Any = None,
        unsafe_local_shell: bool = False,
        tracer: AgentTracer | None = None,
        trace_context: TraceContext | None = None,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.files_for = files_for
        self.secret_resolver = secret_resolver
        self.sandbox_backend = sandbox_backend
        self.unsafe_local_shell = unsafe_local_shell
        self.tracer = tracer
        self.trace_context = trace_context or TraceContext()

    def invoke(self, spec: ScriptToolSpec, args: Mapping[str, Any]) -> str:
        arguments = validate_script_arguments(args, spec.args_schema)
        files = dict(self.files_for(spec))
        if spec.entrypoint not in files:
            raise PermissionError("bound script entrypoint is unavailable")
        stage = Path(tempfile.mkdtemp(prefix=".sweforge-tool-", dir=str(self.worktree)))
        started = time.monotonic()
        status = "failure"
        output_chars = 0
        secret_values: list[str] = []
        if self.tracer is not None:
            self.tracer.emit(
                "SCRIPT TOOL CALL",
                f"tool={spec.name} effect={spec.effect}",
                self.trace_context,
            )
        try:
            for relative, content in files.items():
                destination = (stage / relative).resolve()
                try:
                    destination.relative_to(stage)
                except ValueError as exc:
                    raise PermissionError(
                        "installed script path escaped staging"
                    ) from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            entrypoint = stage / spec.entrypoint
            command = (
                [sys.executable, str(entrypoint)]
                if spec.runtime == "python"
                else ["/bin/sh", str(entrypoint)]
            )
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                **dict(spec.env),
            }
            if spec.secret_env:
                if self.secret_resolver is None:
                    if self.tracer is not None:
                        self.tracer.emit(
                            "SECRET RESOLUTION",
                            (
                                f"tool={spec.name} required={len(spec.secret_env)} "
                                f"resolved=0 required_missing={len(spec.secret_env)}"
                            ),
                            self.trace_context,
                        )
                    raise PermissionError(
                        "required repository credential store is unavailable"
                    )
                try:
                    resolved = self.secret_resolver(spec)
                except PermissionError:
                    if self.tracer is not None:
                        self.tracer.emit(
                            "SECRET RESOLUTION",
                            (
                                f"tool={spec.name} required={len(spec.secret_env)} "
                                f"resolved=0 required_missing={len(spec.secret_env)}"
                            ),
                            self.trace_context,
                        )
                    raise
                secret_values = [value.reveal() for value in resolved.values()]
                env.update({name: value.reveal() for name, value in resolved.items()})
                if self.tracer is not None:
                    self.tracer.emit(
                        "SECRET RESOLUTION",
                        (
                            f"tool={spec.name} required={len(spec.secret_env)} "
                            f"resolved={len(resolved)}"
                        ),
                        self.trace_context,
                    )
            stdin = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            result = self._run(command, stdin, env, spec.timeout_seconds)
            stdout = _bounded(redact_injected_secrets(result.stdout, secret_values))
            stderr = _bounded(redact_injected_secrets(result.stderr, secret_values))
            rendered = stdout
            if stderr:
                rendered += ("\n" if rendered else "") + f"stderr:\n{stderr}"
            if result.returncode:
                rendered = (
                    f"Script tool failed with exit code {result.returncode}.\n"
                    f"{rendered}"
                )
            rendered = _bounded(rendered.rstrip())
            output_chars = len(rendered)
            status = "success" if result.returncode == 0 else "failure"
            return rendered
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"script tool exceeded {spec.timeout_seconds} second timeout"
            ) from exc
        except (PermissionError, TimeoutError):
            raise
        except Exception as exc:
            safe = redact_injected_secrets(exc, secret_values)
            raise RuntimeError(f"registered script tool failed: {safe}") from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if self.tracer is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.tracer.emit(
                    "SCRIPT TOOL RESULT",
                    (
                        f"tool={spec.name} status={status} "
                        f"duration_ms={duration_ms} output_chars={output_chars}"
                    ),
                    self.trace_context,
                )

    def _run(
        self, command: list[str], stdin: str, env: Mapping[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        trusted = getattr(self.sandbox_backend, "execute_tool", None)
        if callable(trusted):
            result = trusted(command, stdin=stdin, env=dict(env), timeout=timeout)
            if isinstance(result, subprocess.CompletedProcess):
                return result
            return subprocess.CompletedProcess(
                command,
                int(getattr(result, "exit_code", 1)),
                str(getattr(result, "stdout", getattr(result, "output", ""))),
                str(getattr(result, "stderr", "")),
            )
        if not self.unsafe_local_shell:
            raise RuntimeError(
                "sandbox provider does not support registered script tools"
            )
        return subprocess.run(
            command,
            cwd=self.worktree,
            env=dict(env),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def build_script_tools(
    specs: tuple[ScriptToolSpec, ...], executor: ScriptToolExecutor
) -> tuple[list[StructuredTool], dict[str, str]]:
    """Build normal model tool schemas without exposing runtime authority fields."""
    tools: list[StructuredTool] = []
    effects: dict[str, str] = {}
    for spec in specs:

        def invoke(_spec=spec, **kwargs):
            return executor.invoke(_spec, kwargs)

        tools.append(
            StructuredTool.from_function(
                func=invoke,
                name=spec.name,
                description=spec.description,
                args_schema=dict(spec.args_schema),
                infer_schema=False,
            )
        )
        effects[spec.name] = spec.effect
    return tools, effects
