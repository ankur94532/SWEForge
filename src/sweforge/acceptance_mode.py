"""Single authority for the acceptance-mode safety gate.

Acceptance mode exists so test-only machinery — fault injection above all —
cannot be activated by accident. It is deliberately one explicit environment
variable with one exact value, checked in one place, so there is no second
path that could drift.
"""

import os

ACCEPTANCE_MODE_ENV = "SWEFORGE_ACCEPTANCE_MODE"
_ENABLED_VALUE = "1"


class AcceptanceModeRequired(RuntimeError):
    """A gated capability was requested without explicit acceptance mode."""


def acceptance_enabled() -> bool:
    """True only for the exact opt-in value; anything else is off."""
    return os.environ.get(ACCEPTANCE_MODE_ENV) == _ENABLED_VALUE


def require_acceptance(feature: str) -> None:
    """Fail closed when a gated capability is requested outside acceptance mode."""
    if not acceptance_enabled():
        raise AcceptanceModeRequired(
            f"{feature} requires {ACCEPTANCE_MODE_ENV}={_ENABLED_VALUE}; "
            "it is test-only machinery and must never be enabled in production"
        )
