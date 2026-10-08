"""Regression test for native --windowed executables on Windows."""

from __future__ import annotations

import sys

from worktwin.__main__ import _configure_frozen_stdio


def test_frozen_windowed_process_creates_stderr_and_stdout(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv("WORKTWIN_DATA_DIR", str(tmp_path))
        patch.setattr(sys, "frozen", True, raising=False)
        patch.setattr(sys, "stdout", None)
        patch.setattr(sys, "stderr", None)

        _configure_frozen_stdio()
        assert sys.stdout is not None
        assert sys.stderr is sys.stdout
        sys.stdout.write("desktop startup works\n")
        sys.stdout.flush()
        logfile = sys.stdout

    logfile.close()
    assert "desktop startup works" in (tmp_path / "worktwin-launch.log").read_text(encoding="utf-8")


def test_regular_python_stdout_is_not_modified(monkeypatch):
    current = sys.stdout
    with monkeypatch.context() as patch:
        patch.setattr(sys, "frozen", False, raising=False)
        _configure_frozen_stdio()
        assert sys.stdout is current
