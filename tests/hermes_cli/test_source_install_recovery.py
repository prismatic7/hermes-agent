"""Checks for the two source-install fixes in this task.

1. ``_pid_is_running`` must report a zombie as dead: an update marker naming an
   un-reaped child otherwise reads as a live holder forever and blocks every
   dependency sync (and, with launchd KeepAlive, rebuilds an environment each boot).
   Upstream since implemented this as ``_process_state``; the probe below targets it.
2. ``pm.runtime._inputs`` must tolerate a missing ``uv.lock``: the PM runtime
   ships without the lock, and read_bytes() raised FileNotFoundError instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import _early_recovery as er


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX zombie semantics")
def test_pid_is_running_reports_a_zombie_as_dead():
    pid = os.fork()
    if pid == 0:  # child: exit immediately, leaving a zombie until reaped
        os._exit(0)
    try:
        deadline = time.time() + 5
        state = ""
        while time.time() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)
        assert state.startswith("Z"), f"child never became a zombie (stat={state!r})"
        assert (er._process_state(pid) or "").upper().startswith("Z")
        assert er._pid_is_running(pid) is False  # the regression
    finally:
        os.waitpid(pid, 0)


def test_pid_is_running_still_sees_a_live_process():
    assert er._pid_is_running(os.getpid()) is True
    assert er._pid_is_running(0) is False
    state = er._process_state(os.getpid())
    assert state is None or not state.upper().startswith("Z")


def test_pm_runtime_inputs_tolerates_a_missing_lock(tmp_path):
    from pm.runtime import _inputs

    project = tmp_path / "pm"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='pm'\n", encoding="utf-8")
    python = Path(sys.executable)

    with_lock = tmp_path / "with-lock"
    with_lock.mkdir()
    (with_lock / "pyproject.toml").write_text("[project]\nname='pm'\n", encoding="utf-8")
    (with_lock / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    # No uv.lock: digesting the absence must not raise.
    missing = _inputs(project, python)
    assert isinstance(missing, str) and len(missing) == 64

    # Presence of the lock must still change the digest (no silent collision).
    assert _inputs(with_lock, python) != missing

    # Deterministic across calls.
    assert _inputs(project, python) == missing
