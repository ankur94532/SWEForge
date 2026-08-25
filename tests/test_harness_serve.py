"""Readiness must be a real signal, and a kill must be a real kill."""

import subprocess
import sys

import pytest
from harness.serve import ServeProcess, ServerNotReady

from sweforge.server import ServerConfig, SWEForgeServer


def test_ready_file_is_written_only_after_the_lock_and_first_poll(tmp_path):
    """Signalling earlier would let a harness race the singleton lock."""
    import inspect

    source = inspect.getsource(SWEForgeServer.run)
    assert source.index("instance_lock.acquire()") < source.index("_signal_ready()")
    assert source.index("self._poll(") < source.index("_signal_ready()")


def _config(tmp_path, **kw):
    return ServerConfig(
        repositories=("example/repo",),
        repo_paths={"example/repo": tmp_path},
        db=tmp_path / "s.db",
        **kw,
    )


def test_signal_ready_is_inert_without_a_configured_path(tmp_path):
    SWEForgeServer(_config(tmp_path))._signal_ready()  # must not raise


def test_signal_ready_records_the_pid(tmp_path):
    target = tmp_path / "nested" / "ready"
    SWEForgeServer(_config(tmp_path, ready_file=target))._signal_ready()
    assert target.read_text().strip() == str(__import__("os").getpid())


def test_start_raises_with_the_log_when_the_process_exits_early(tmp_path):
    """A dead process must fail loudly, not time out silently."""
    serve = ServeProcess(root=tmp_path / "srv", args=["--repo-path", "bogus-no-equals"])
    with pytest.raises(ServerNotReady, match="exited with"):
        serve.start(timeout=20.0)


def test_await_ready_times_out_with_the_log_rather_than_hanging(tmp_path, monkeypatch):
    serve = ServeProcess(root=tmp_path / "srv")

    class NeverExits:
        returncode = None

        def poll(self):
            return None

    serve.process = NeverExits()
    with pytest.raises(ServerNotReady, match="did not signal readiness within"):
        serve._await_ready(0.2)


def test_kill_is_a_real_sigkill(tmp_path):
    """S16 needs a crash with no chance to clean up."""
    serve = ServeProcess(root=tmp_path / "srv")
    serve.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    code = serve.kill()
    assert not serve.is_running()
    # negative return code means terminated by signal; -9 is SIGKILL
    assert code == -9


def test_stop_escalates_to_kill_when_a_process_ignores_interrupt(tmp_path):
    import time

    serve = ServeProcess(root=tmp_path / "srv")
    armed = tmp_path / "armed"
    serve.process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            # Announce only after the handler is installed: signalling earlier
            # races the interpreter and the child dies of SIGINT (-2).
            "open(sys.argv[1], 'w').write('armed')\n"
            "time.sleep(60)",
            str(armed),
        ]
    )
    deadline = time.monotonic() + 10
    while not armed.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert armed.exists(), "child never installed its SIGINT handler"
    assert serve.stop(timeout=1.0) == -9
    assert not serve.is_running()


def test_tail_reports_when_there_is_no_output_yet(tmp_path):
    assert ServeProcess(root=tmp_path / "srv").tail() == "(no output)"


def test_context_manager_stops_the_process(tmp_path):
    serve = ServeProcess(root=tmp_path / "srv")
    serve.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    with serve:
        assert serve.is_running()
    assert not serve.is_running()
