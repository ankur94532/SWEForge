# Acceptance sandbox backend

Operator-owned macOS Seatbelt sandbox provider, registered through the
`sweforge.sandbox_backends` entry point as `acceptance-seatbelt`.

Strict GitHub execution fails closed when no provider is configured
(`SecureExecutionUnavailable`), so **no live acceptance scenario can run unless
this package is installed**. It is deliberately a separate distribution: it is
operator infrastructure, never part of the `sweforge` wheel.

Installed automatically by `uv sync` via the `dev` dependency group.

## History

This package previously lived only under `~/.sweforge/acceptance/.../common/sandbox`
and was patched in place. That patch — accepting Deep Agents'
`list[tuple[str, bytes]]` contract in `upload_files` — was lost to version
control and broke a live S1 run. It is preserved here; see `upload_files`.
