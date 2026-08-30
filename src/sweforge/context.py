"""Authoritative, non-secret runtime authority for one repository invocation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RepoAgentContext:
    """Application-derived authority; models never choose these values."""

    repo_id: int
    repo_full_name: str
    thread_id: str
    config_generation_id: str | None = None

    def __post_init__(self) -> None:
        if self.repo_id <= 0:
            raise ValueError("repo_id must be positive")
        if not self.repo_full_name or "/" not in self.repo_full_name:
            raise ValueError("repo_full_name must be OWNER/REPOSITORY")
        if not self.thread_id:
            raise ValueError("thread_id must not be empty")
        if self.config_generation_id is not None and not self.config_generation_id:
            raise ValueError("config_generation_id must be non-empty when supplied")
