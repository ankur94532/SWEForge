import asyncio

import pytest
from deepagents.backends.protocol import ExecuteResponse

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
