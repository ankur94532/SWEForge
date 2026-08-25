"""Runtime configuration for the V0 CLI."""

import os
from dataclasses import dataclass

# Bounded retries for transport-level provider failures only.
#
# Semantic retry policy still belongs to SWEForge's dispatcher: a review that
# produced a bad artifact is re-run as a whole, deliberately. But a dropped
# TCP connection is not a semantic event, and with no retry a single blip on
# the twelfth of roughly fifteen model calls discards an entire review. One
# measured batch lost 170 complete reviews to 153 connection errors.
#
# The provider SDK retries exactly the transient classes (connection errors,
# timeouts, 408/409/429, 5xx) with exponential backoff and jitter. None of
# those are semantic outcomes, and a retried request carries identical inputs,
# so reproducibility of the review is unaffected.
MODEL_TRANSIENT_RETRIES = 3


@dataclass(frozen=True)
class Config:
    """Configuration loaded from CLI values and environment variables."""

    model: str

    @classmethod
    def from_environment(cls, model: str | None = None) -> "Config":
        selected = model or os.getenv("SWEFORGE_MODEL")
        if not selected:
            raise ValueError(
                "No model configured. Pass --model provider:model or set "
                "SWEFORGE_MODEL."
            )
        return cls(model=selected)
