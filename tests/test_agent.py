from pathlib import Path

from sweforge.agent import _build_backend


def test_shell_execution_uses_worktree_as_current_directory(tmp_path: Path):
    (tmp_path / "calculator.py").write_text("print(1 + 1)\n")
    backend = _build_backend(str(tmp_path))

    pwd = backend.execute("pwd")
    file_check = backend.execute("test -f calculator.py")

    assert pwd.output.strip() == str(tmp_path)
    assert pwd.exit_code == 0
    assert file_check.exit_code == 0
