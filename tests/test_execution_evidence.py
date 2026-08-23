import asyncio
import inspect

import pytest
from deepagents.backends.protocol import (
    BackendProtocol,
    ExecuteResponse,
    GrepResult,
    SandboxBackendProtocol,
)
from deepagents.middleware.filesystem import FilesystemMiddleware

from sweforge.agent import _build_backend
from sweforge.execution_evidence import RecordingSandboxBackend


class FakeBackend:
    id = "fake"

    def __init__(self, responses):
        self.responses = iter(responses)

    def execute(self, command, *, timeout=None):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    async def aexecute(self, command, *, timeout=None):
        return self.execute(command, timeout=timeout)


class ForwardingBackend(FakeBackend):
    id = "forwarding"

    def __getattr__(self, name):
        if name.startswith("a"):

            async def async_operation(*args, **kwargs):
                return (name, args, kwargs)

            return async_operation

        def operation(*args, **kwargs):
            return (name, args, kwargs)

        return operation

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return GrepResult(matches=[], error=None)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return self.grep(pattern, path, glob, max_count=max_count)


class LegacyGrepBackend(FakeBackend):
    def grep(self, pattern, path=None, glob=None):
        return type("Result", (), {"matches": [pattern], "error": None})()

    async def agrep(self, pattern, path=None, glob=None):
        return self.grep(pattern, path, glob)


def response(output="ok", exit_code=0, truncated=False):
    return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)


def test_records_completed_commands_in_order_and_preserves_response():
    observations = []
    backend = RecordingSandboxBackend(
        FakeBackend([response("one"), response("two", exit_code=3)]),
        lambda **item: observations.append(item),
    )

    first = backend.execute("first")
    backend.execute("second")

    assert first.output == "one"
    assert [item["command"] for item in observations] == ["first", "second"]
    assert observations[1]["exit_code"] == 3


def test_prose_without_execute_creates_no_observation():
    observations = []
    RecordingSandboxBackend(FakeBackend([]), observations.append)
    assert observations == []


def test_failed_and_crashed_commands_are_distinguishable():
    observations = []
    backend = RecordingSandboxBackend(
        FakeBackend([response("failure", exit_code=1), RuntimeError("crash")]),
        lambda **item: observations.append(item),
    )

    backend.execute("fails")
    with pytest.raises(RuntimeError):
        backend.execute("crashes")

    assert len(observations) == 1
    assert observations[0]["exit_code"] == 1


def test_output_is_bounded_and_secret_redacted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    observations = []
    backend = RecordingSandboxBackend(
        FakeBackend([response("a" * 20_000 + " super-secret-value")]),
        lambda **item: observations.append(item),
    )

    backend.execute("curl --header 'Authorization: Bearer super-secret-value'")

    item = observations[0]
    assert len(item["output"]) <= 12_000
    assert item["truncated"] is True
    assert "super-secret-value" not in item["output"]
    assert "super-secret-value" not in item["command"]


def test_async_completed_command_is_recorded():
    observations = []
    backend = RecordingSandboxBackend(
        FakeBackend([response("async")]), lambda **item: observations.append(item)
    )

    asyncio.run(backend.aexecute("async-command"))

    assert observations[0]["output"] == "async"


def test_class_level_protocol_inspection_and_grep_capability():
    assert list(inspect.signature(RecordingSandboxBackend.grep).parameters) == list(
        inspect.signature(BackendProtocol.grep).parameters
    )
    assert list(inspect.signature(RecordingSandboxBackend.agrep).parameters) == list(
        inspect.signature(BackendProtocol.agrep).parameters
    )
    from deepagents.backends.protocol import _method_accepts_max_count

    assert _method_accepts_max_count(RecordingSandboxBackend, "grep") is True


def test_wrapper_exposes_complete_installed_protocol_surface():
    expected = {
        name
        for cls in (BackendProtocol, SandboxBackendProtocol)
        for name, member in cls.__dict__.items()
        if not name.startswith("_") and callable(member)
    }
    assert expected <= set(dir(RecordingSandboxBackend))


def test_filesystem_operations_forward_without_recording_shell_evidence():
    observations = []
    backend = ForwardingBackend([])
    wrapper = RecordingSandboxBackend(backend, lambda **item: observations.append(item))

    assert wrapper.ls(".")[0] == "ls"
    assert asyncio.run(wrapper.aread("file"))[0] == "aread"
    assert wrapper.grep("needle", max_count=2).matches == []
    assert asyncio.run(wrapper.agrep("needle", max_count=2)).matches == []
    assert wrapper.glob("*.java")[0] == "glob"
    assert wrapper.write("file", "body")[0] == "write"
    assert wrapper.edit("file", "old", "new")[0] == "edit"
    assert wrapper.delete("file")[0] == "delete"
    assert wrapper.upload_files([])[0] == "upload_files"
    assert wrapper.download_files([])[0] == "download_files"
    assert observations == []


def test_legacy_grep_signature_is_supported_without_forwarding_max_count():
    wrapper = RecordingSandboxBackend(LegacyGrepBackend([]), lambda **_: None)
    result = wrapper.grep("needle", max_count=1)
    assert result.matches == ["needle"]


def test_build_backend_with_recorder_has_inspectable_filesystem_surface():
    backend = _build_backend(
        ".",
        sandbox_backend=ForwardingBackend([]),
        execution_evidence_sink=lambda **_: None,
    )
    assert hasattr(type(backend.default), "grep")
    assert inspect.signature(type(backend.default).grep).parameters["max_count"]


def test_filesystem_middleware_accepts_wrapped_backend():
    backend = RecordingSandboxBackend(ForwardingBackend([]), lambda **_: None)
    middleware = FilesystemMiddleware(backend=backend)
    assert {tool.name for tool in middleware.tools} >= {"grep", "read_file"}
