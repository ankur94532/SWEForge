"""The acceptance sandbox provider must stay installed and version-controlled.

Strict GitHub execution fails closed without a registered
`sweforge.sandbox_backends` entry point, so a missing provider silently blocks
every live acceptance scenario. This provider previously lived only under
`~/.sweforge/`, was patched in place, and lost that patch — breaking a live S1
run. These tests fail loudly instead.
"""

from importlib.metadata import entry_points

from sweforge.execution_security import resolve_sandbox_provider

PROVIDER = "acceptance-seatbelt"


def test_acceptance_sandbox_entry_point_is_registered():
    names = {item.name for item in entry_points(group="sweforge.sandbox_backends")}
    assert PROVIDER in names, (
        f"{PROVIDER} is not installed; live acceptance scenarios cannot run. "
        "Run `uv sync` — it is a dev dependency at acceptance/sandbox."
    )


def test_acceptance_sandbox_provider_resolves_and_is_callable():
    provider = resolve_sandbox_provider(PROVIDER)
    assert provider is not None
    assert callable(provider)


def test_upload_files_keeps_the_deep_agents_tuple_contract():
    """The fix that was lost once already: list[tuple[str, bytes]], not a dict."""
    import inspect

    from sweforge_acceptance_sandbox import seatbelt_backend

    backend_cls = seatbelt_backend.__annotations__.get("return")
    del backend_cls  # only the source contract matters here
    source = inspect.getsource(inspect.getmodule(seatbelt_backend))
    assert "def upload_files(" in source
    signature = source.split("def upload_files(", 1)[1].split(")", 1)[0]
    assert "list[tuple[str, bytes]]" in signature, (
        "upload_files must accept Deep Agents' list[tuple[path, bytes]] contract"
    )
