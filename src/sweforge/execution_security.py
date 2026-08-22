"""Provider-neutral execution backend boundary."""

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Protocol

from deepagents.backends.protocol import SandboxBackendProtocol

from .context import RepoAgentContext


class SandboxBackendProvider(Protocol):
    """Trusted operator/provider hook that returns an isolated backend."""

    def __call__(
        self, *, context: RepoAgentContext, worktree: str
    ) -> SandboxBackendProtocol: ...


SandboxBackendFactory = Callable[..., SandboxBackendProtocol]
SANDBOX_ENTRY_POINT_GROUP = "sweforge.sandbox_backends"


class SecureExecutionUnavailable(RuntimeError):
    """Raised when strict GitHub execution has no configured sandbox provider."""


def resolve_sandbox_provider(name: str | None) -> SandboxBackendProvider | None:
    if not name:
        return None
    selected = next(
        (
            item
            for item in entry_points(group=SANDBOX_ENTRY_POINT_GROUP)
            if item.name == name
        ),
        None,
    )
    if selected is None:
        raise SecureExecutionUnavailable(f"unknown sandbox provider: {name}")
    provider = selected.load()
    if not callable(provider):
        raise SecureExecutionUnavailable(f"sandbox provider is not callable: {name}")
    return provider


def require_secure_backend(
    *,
    context: RepoAgentContext,
    worktree: str,
    provider: SandboxBackendProvider | SandboxBackendFactory | None,
    unsafe_local_shell: bool,
) -> SandboxBackendProtocol | None:
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
    if not callable(getattr(backend, "execute", None)):
        raise SecureExecutionUnavailable(
            "sandbox provider returned a non-shell backend"
        )
    return backend
