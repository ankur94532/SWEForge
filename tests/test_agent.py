from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from sweforge.agent import LiveInputMiddleware, _build_backend, _normalize_response_text


def test_shell_execution_uses_worktree_as_current_directory(tmp_path: Path):
    (tmp_path / "calculator.py").write_text("print(1 + 1)\n")
    backend = _build_backend(str(tmp_path))

    pwd = backend.execute("pwd")
    file_check = backend.execute("test -f calculator.py")

    assert pwd.output.strip() == str(tmp_path)
    assert pwd.exit_code == 0
    assert file_check.exit_code == 0


def test_live_input_middleware_injects_stable_deduplicated_messages():
    def pending():
        return [("event-1", "@agent preserve compatibility")]

    middleware = LiveInputMiddleware(pending)
    first = middleware.before_model({"messages": []}, None)
    assert len(first["messages"]) == 1
    message = first["messages"][0]
    assert isinstance(message, HumanMessage)
    assert message.id.startswith("sweforge:event:")
    assert middleware.before_model({"messages": [message]}, None) is None


def test_normalize_response_text_handles_string_content():
    message = type("Message", (), {"content": "done"})()
    assert _normalize_response_text(message) == "done"


def test_normalize_response_text_handles_one_text_block():
    message = type("Message", (), {"content": [{"type": "text", "text": "done"}]})()
    assert _normalize_response_text(message) == "done"


def test_normalize_response_text_does_not_leak_structured_repr():
    message = AIMessage(content=[{"type": "text", "text": "All tests pass."}])
    result = _normalize_response_text(message)
    assert result == "All tests pass."
    assert "[" not in result
    assert "'text'" not in result


def test_normalize_response_text_joins_multiple_text_blocks():
    message = type(
        "Message",
        (),
        {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]
        },
    )()
    assert _normalize_response_text(message) == "first\nsecond"


def test_normalize_response_text_ignores_non_text_blocks():
    message = type(
        "Message",
        (),
        {
            "content": [
                {"type": "text", "text": "visible"},
                {"type": "tool_call", "name": "execute", "args": {"cmd": "pwd"}},
                {"type": "reasoning", "reasoning": "private"},
                {"type": "metadata", "value": {"secret": "private"}},
            ]
        },
    )()
    result = _normalize_response_text(message)
    assert result == "visible"
    assert "tool_call" not in result
    assert "private" not in result


def test_normalize_response_text_handles_empty_content():
    assert _normalize_response_text(type("Message", (), {"content": []})()) == ""
    assert _normalize_response_text(type("Message", (), {"content": None})()) == ""


def test_normalize_response_text_ignores_malformed_blocks_without_repr_leaks():
    message = type(
        "Message",
        (),
        {"content": [{"type": "unknown", "value": "internal"}, "not a block"]},
    )()
    result = _normalize_response_text(message)
    assert result == ""
    assert "unknown" not in result
    assert "internal" not in result
