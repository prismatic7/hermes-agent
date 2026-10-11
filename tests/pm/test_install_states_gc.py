"""Dependency state of a deleted checkout is reclaimed; state anything live could still read, or
whose checkout this process merely cannot see, is kept (``pm/install_states.py``)."""

from __future__ import annotations

import os
import subprocess

import pm.environments
import pytest
from hermes_cli.worktree_ops import _prune_stale_worktrees
from pm.filesystem import lock_fd
from pm.install_states import collect_orphan_install_states, orphan_install_states, unclaimed_install_states


def _state(installs, key, project_root):
    state = installs / key
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(project_root), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    return state


def test_startup_prune_reclaims_only_provably_deleted_checkouts(tmp_path, monkeypatch):
    installs = tmp_path / "home" / "installs"
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    live_root = tmp_path / "home" / "hermes-agent"
    live_root.mkdir(parents=True)
    live = _state(installs, "aaaa", live_root)
    gone_scratch = _state(installs, "bbbb", tmp_path / "home" / "cache" / "scratch" / "clone")
    gone_worktree = _state(installs, "cccc", repo / ".worktrees" / "removed-by-hand")
    # Pre-record state (no .project-root) has no evidence either way: never touched.
    unknown = installs / "dddd"
    (unknown / "environments").mkdir(parents=True)
    # A data root shared across a mount boundary records checkouts this process cannot see:
    # a host install from inside a container, a container install from the host.
    host_install = _state(installs, "eeee", tmp_path / "other-host-home" / ".hermes" / "hermes-agent")
    container_install = _state(installs, "ffff", tmp_path / "opt-hermes")

    assert orphan_install_states(installs) == [gone_scratch, gone_worktree]
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    # No tree under .worktrees/ is old enough to prune: the reclaim must still run.
    _prune_stale_worktrees(str(repo))
    assert not gone_scratch.exists() and not gone_worktree.exists()
    assert all(p.is_dir() for p in (live, unknown, host_install, container_install))


def test_held_orphan_is_kept(tmp_path):
    installs = tmp_path / "installs"
    locked = _state(installs, "dddd", tmp_path / "gone-a")
    leased = _state(installs, "eeee", tmp_path / "gone-b")
    runtime = _state(installs, "ffff", tmp_path / "gone-c")

    fds = [os.open(locked / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)]
    for lease_dir in (leased / "environments" / "gen" / ".leases",
                      runtime / "pm-runtime" / "generations" / "gen" / ".leases"):
        lease_dir.mkdir(parents=True)
        fds.append(os.open(lease_dir / "reader", os.O_CREAT | os.O_RDWR, 0o600))
    assert all(lock_fd(fd, wait=False) for fd in fds)
    try:
        assert collect_orphan_install_states(installs) == []
        assert locked.is_dir() and leased.is_dir() and runtime.is_dir()
    finally:
        for fd in fds:
            os.close(fd)
    # Locks released (owner exited): all are reclaimed on the next pass.
    assert sorted(p.name for p in collect_orphan_install_states(installs)) == ["dddd", "eeee", "ffff"]


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory modes")
def test_partly_removed_orphan_keeps_its_record_until_retried(tmp_path):
    installs = tmp_path / "installs"
    state = _state(installs, "aaaa", tmp_path / "gone")
    stuck = state / "environments" / "gen" / "venv"
    (stuck / "pyvenv.cfg").write_text("", encoding="utf-8")
    stuck.chmod(0o500)  # its entries cannot be unlinked: the removal stops part-way
    try:
        assert collect_orphan_install_states(installs) == []
        assert orphan_install_states(installs) == [state]
    finally:
        stuck.chmod(0o700)
    assert collect_orphan_install_states(installs) == [state] and not state.exists()


def test_unclaimed_states_swept_only_on_request(tmp_path):
    """A state dir written before ``inputs/.project-root`` existed can never name its checkout, so
    the record-ful path skips it for ever (observed: five dead dirs, 1.8 GB). The sweep is
    explicit, and it still keeps a dir whose checkout this data root can see."""
    installs = tmp_path / "home" / "installs"
    live = tmp_path / "home" / "scratch" / "clone"
    live.mkdir(parents=True)
    (live / "pyproject.toml").write_text("", encoding="utf-8")
    live_state = installs / pm.environments.install_key(live)
    (live_state / "environments").mkdir(parents=True)
    dead_state = installs / ("0" * 16)
    (dead_state / "environments").mkdir(parents=True)

    assert orphan_install_states(installs) == []
    assert unclaimed_install_states(installs) == [dead_state]
    assert collect_orphan_install_states(installs) == []  # never swept by default
    assert dead_state.is_dir()
    assert collect_orphan_install_states(installs, sweep_unknown=True) == [dead_state]
    assert not dead_state.exists() and live_state.is_dir()


def test_sweep_unknown_keeps_a_held_state(tmp_path):
    installs = tmp_path / "installs"
    state = installs / ("1" * 16)
    (state / "environments").mkdir(parents=True)
    fd = os.open(state / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=False)
    try:
        assert collect_orphan_install_states(installs, sweep_unknown=True) == []
        assert state.is_dir()
    finally:
        os.close(fd)
    assert collect_orphan_install_states(installs, sweep_unknown=True) == [state]
