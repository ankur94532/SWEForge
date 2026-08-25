"""The acceptance gate must be exact — a truthy-looking value is not opt-in."""

import pytest

from sweforge.acceptance_mode import (
    ACCEPTANCE_MODE_ENV,
    AcceptanceModeRequired,
    acceptance_enabled,
    require_acceptance,
)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ACCEPTANCE_MODE_ENV, raising=False)
    assert acceptance_enabled() is False
    with pytest.raises(AcceptanceModeRequired, match="test-only machinery"):
        require_acceptance("fault injection")


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE", "2", " 1"])
def test_only_the_exact_value_enables_it(monkeypatch, value):
    monkeypatch.setenv(ACCEPTANCE_MODE_ENV, value)
    assert acceptance_enabled() is False


def test_exact_value_enables_it(monkeypatch):
    monkeypatch.setenv(ACCEPTANCE_MODE_ENV, "1")
    assert acceptance_enabled() is True
    require_acceptance("fault injection")
