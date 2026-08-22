"""Provider-neutral execution backend boundary."""

from collections.abc import Callable
from typing import Protocol

from deepagents.backends.protocol import BackendProtocol

from .context import RepoAgentContext


class SandboxBackendProvider(Protocol):
    """Trusted operator/provider hook that returns an isolated backend."""

    def __call__(
        self, *, context: RepoAgentContext, worktree: str
    ) -> BackendProtocol: ...


SandboxBackendFactory = Callable[..., BackendProtocol]


class SecureExecutionUnavailable(RuntimeError):
    """Raised when strict GitHub execution has no configured sandbox provider."""


def require_secure_backend(
    *,
    context: RepoAgentContext,
    worktree: str,
    provider: SandboxBackendProvider | SandboxBackendFactory | None,
    unsafe_local_shell: bool,
) -> BackendProtocol | None:
    if unsafe_local_shell:
        return None
    if provider is None:
        raise SecureExecutionUnavailable(
            "strict repository execution requires a configured sandbox backend; "
            "use an explicit unsafe local-shell mode only for development"
        )
    backend = provider(context=context, worktree=worktree)
    if backend is None:
        raise SecureExecutionUnavailable("sandbox provider returned no backend")
    return backend
