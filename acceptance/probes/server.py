"""Acceptance probe MCP server.

Provides the six deterministic probes S4-S9 assert against. Adopted from the
operator server that previously lived only under ~/.sweforge/, with its JSON
state file replaced by a SQLite ledger so call counts survive the process
kills S15-S17 perform.

Registered as MCP server id `acceptance`, so tools surface to the executor as
`acceptance_retryable_probe` and siblings.
"""

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from acceptance.probes.ledger import LEDGER_ENV, connect, record

mcp = FastMCP("sweforge-acceptance", log_level="ERROR")
_connection = None


def _ledger():
    global _connection
    if _connection is None:
        _connection = connect(os.environ[LEDGER_ENV])
    return _connection


def _record(
    tool_name: str,
    operation_id: str,
    repo_id: int,
    repo_full_name: str,
    result_class: str,
    args_received: dict | None = None,
) -> int:
    return record(
        _ledger(),
        tool_name=tool_name,
        operation_id=operation_id,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
        result_class=result_class,
        args_received=args_received,
    )


@mcp.tool()
def retryable_probe(
    operation_id: str, repo_id: int, repo_full_name: str
) -> dict[str, Any]:
    """Fail retryably on call 1 for an operation_id, then succeed."""
    call_number = _record(
        "retryable_probe", operation_id, repo_id, repo_full_name, "structured_result"
    )
    if call_number == 1:
        return {
            "ok": False,
            "retryable": True,
            "error_code": "TEMPORARY_ACCEPTANCE_FAILURE",
            "call_number": call_number,
        }
    return {
        "ok": True,
        "retryable": False,
        "value": "acceptance retry succeeded",
        "call_number": call_number,
    }


@mcp.tool()
def nonretryable_probe(
    operation_id: str, repo_id: int, repo_full_name: str
) -> dict[str, Any]:
    """Return a permanent failure without raising; must be attempted once."""
    call_number = _record(
        "nonretryable_probe", operation_id, repo_id, repo_full_name, "structured_result"
    )
    return {
        "ok": False,
        "retryable": False,
        "error_code": "PERMANENT_ACCEPTANCE_FAILURE",
        "fallback_allowed": True,
        "call_number": call_number,
    }


@mcp.tool()
def warning_probe(
    operation_id: str, repo_id: int, repo_full_name: str
) -> dict[str, Any]:
    """Return an advisory warning: not success, not failure, not fatal."""
    call_number = _record(
        "warning_probe", operation_id, repo_id, repo_full_name, "warning"
    )
    return {
        "ok": False,
        "severity": "warning",
        "continue_allowed": True,
        "message": "acceptance advisory unavailable",
        "call_number": call_number,
    }


@mcp.tool()
def fatal_probe(operation_id: str, repo_id: int, repo_full_name: str) -> None:
    """Raise the deterministic hard tool failure."""
    _record("fatal_probe", operation_id, repo_id, repo_full_name, "hard_exception")
    raise RuntimeError("ACCEPTANCE_FATAL_TOOL_FAILURE")


@mcp.tool()
def timeout_probe(operation_id: str, repo_id: int, repo_full_name: str) -> None:
    """Raise a deterministic timeout-class failure."""
    _record("timeout_probe", operation_id, repo_id, repo_full_name, "timeout_exception")
    raise TimeoutError("ACCEPTANCE_TOOL_TIMEOUT")


@mcp.tool()
def identity_echo(
    operation_id: str,
    repo_id: int,
    repo_full_name: str,
    workspace_root: str = "",
    repo_path: str = "",
    tenant: str = "",
) -> dict[str, Any]:
    """Echo only the authoritative identity the interceptor injected.

    The spoofable fields are accepted and discarded deliberately: a model can
    attempt to supply them, and recording what arrived is what proves
    repo_scope_interceptor stripped them rather than passing them through.
    """
    _record(
        "identity_echo",
        operation_id,
        repo_id,
        repo_full_name,
        "identity",
        args_received={
            "workspace_root": workspace_root,
            "repo_path": repo_path,
            "tenant": tenant,
        },
    )
    return {"repo_id": repo_id, "repo_full_name": repo_full_name}


def main() -> None:
    parser = argparse.ArgumentParser(description="SWEForge acceptance probe server")
    parser.add_argument("--ledger", default=os.environ.get(LEDGER_ENV))
    args = parser.parse_args()
    if not args.ledger:
        parser.error(f"--ledger or {LEDGER_ENV} is required")
    os.environ[LEDGER_ENV] = str(args.ledger)
    _ledger()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
