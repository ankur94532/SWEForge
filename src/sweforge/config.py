"""Runtime configuration for the V0 CLI."""

import os
from dataclasses import dataclass


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
