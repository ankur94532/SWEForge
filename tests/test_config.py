import pytest

from sweforge.config import Config


def test_model_can_come_from_argument(monkeypatch):
    monkeypatch.delenv("SWEFORGE_MODEL", raising=False)
    assert Config.from_environment("openai:gpt-5").model == "openai:gpt-5"


def test_model_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("SWEFORGE_MODEL", "anthropic:claude-sonnet-4-6")
    assert Config.from_environment().model == "anthropic:claude-sonnet-4-6"


def test_model_is_required(monkeypatch):
    monkeypatch.delenv("SWEFORGE_MODEL", raising=False)
    with pytest.raises(ValueError, match="No model configured"):
        Config.from_environment()
