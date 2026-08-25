"""Spawn, kill and restart a real sweforge-serve process.

Process-level scenarios (S12-S17) need a genuine dispatcher: a singleton lock
held by a second OS process, a SIGKILL that leaves an orphaned attempt, a
restart that must recover durable state. None of that is reachable in-process.
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


class ServerNotReady(RuntimeError):
    """The dispatcher did not announce readiness inside its budget."""


@dataclass
class ServeProcess:
    """One sweforge-serve process under harness control."""

    root: Path
    args: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = None
    ready_file: Path = field(init=False)
    log: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.ready_file = self.root / "ready"
        self.log = self.root / "serve.log"

    def start(
        self, *, timeout: float = 30.0, env: dict | None = None
    ) -> "ServeProcess":
        """Launch and wait for the readiness signal, never a fixed sleep."""
        self.ready_file.unlink(missing_ok=True)
        handle = self.log.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                "uv",
                "run",
                "sweforge-serve",
                "--ready-file",
                str(self.ready_file),
                *self.args,
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )
        self._await_ready(timeout)
        return self

    def _await_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready_file.exists():
                return
            if self.process is not None and self.process.poll() is not None:
                raise ServerNotReady(
                    f"sweforge-serve exited with {self.process.returncode} before "
                    f"signalling readiness; log:\n{self.tail()}"
                )
            time.sleep(0.05)
        raise ServerNotReady(
            f"sweforge-serve did not signal readiness within {timeout}s; "
            f"log:\n{self.tail()}"
        )

    @property
    def pid(self) -> int:
        if self.process is None:
            raise RuntimeError("server was never started")
        return self.process.pid

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def tail(self, lines: int = 40) -> str:
        if not self.log.exists():
            return "(no output)"
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])

    def stop(self, *, timeout: float = 10.0) -> int | None:
        """Graceful shutdown; escalates to SIGKILL rather than hanging."""
        if self.process is None or self.process.poll() is not None:
            return self.process.returncode if self.process else None
        self.process.send_signal(signal.SIGINT)
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return self.kill()

    def kill(self, *, timeout: float = 10.0) -> int | None:
        """SIGKILL: the crash S16 needs, with no chance to clean up."""
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.process.kill()
        return self.process.wait(timeout=timeout)

    def restart(
        self, *, timeout: float = 30.0, env: dict | None = None
    ) -> "ServeProcess":
        self.stop()
        return self.start(timeout=timeout, env=env)

    def __enter__(self) -> "ServeProcess":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
