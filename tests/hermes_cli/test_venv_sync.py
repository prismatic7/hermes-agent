"""venv_sync must work on trees where the venv does not exist yet.

It is the stdlib-only-at-import pre-venv entry point: the installers call
it on a fresh clone before any dependency is importable, and post_update
calls it after a tree swap when the venv is not trustworthy. Its
behaviour is driven through PM's public client; package resolution and
publication belong to PM, not this CLI entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import venv_sync

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_bare(snippet: str) -> subprocess.CompletedProcess:
    program = f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n" + textwrap.dedent(snippet)
    return subprocess.run([sys.executable, '-I', '-S', '-c', program],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)


def test_bare_import_and_passive_paths(tmp_path):
    result = _run_bare(f"""
        from pathlib import Path
        from hermes_cli import venv_sync
        assert 'pm' not in sys.modules
        root = Path({str(tmp_path)!r})
        assert venv_sync.sync(root, check=True)['state'] == 'failed'
        (root / 'install-stamp.json').write_text('{{"updateMechanism":"external"}}')
        assert venv_sync.sync(root) == {{'state': 'sealed', 'ok': True}}
        assert venv_sync.prepare_launch(root, []) is None
        assert 'pm.install' not in sys.modules
    """)
    assert result.returncode == 0, result.stderr


def test_bare_harness_rejects_third_party_import():
    result = _run_bare('import requests')
    assert result.returncode != 0 and 'ModuleNotFoundError' in result.stderr


def _checkout(tmp_path: Path, name: str = "co") -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "uv.lock").write_text("lock-v1\n")
    return root


def _wire_pm(monkeypatch, *, current=False, error=None):
    import pm

    calls = []
    monkeypatch.setattr(pm, "venv_is_current", lambda *, project_root: current)

    def sync(*, explicit, project_root, evict_incompatible_plugins):
        assert evict_incompatible_plugins, "an update sync must disable misfit plugins, not fail"
        calls.append((project_root, explicit))
        if error:
            raise pm.InstallError("venv", error)

    monkeypatch.setattr(pm, "sync_venv", sync)
    return calls


class TestCheckoutSync:
    @pytest.mark.parametrize("foreign", [False, True])
    def test_each_root_uses_the_public_pm_transaction(self, tmp_path, monkeypatch, foreign):
        root = _checkout(tmp_path)
        monkeypatch.setattr(venv_sync, "_project_root", lambda: root)
        calls = _wire_pm(monkeypatch)
        assert venv_sync.sync(root if foreign else None) == {"state": "synced", "ok": True}
        assert calls == [(root, True)]


    def test_check_is_passive(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path)
        calls = _wire_pm(monkeypatch)
        before = set(tmp_path.rglob("*"))
        assert venv_sync.sync(root, check=True) == {"state": "would-sync", "ok": True}
        assert calls == []
        assert set(tmp_path.rglob("*")) == before

    def test_a_failed_sync_is_reported_and_retried(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path)
        calls = _wire_pm(monkeypatch, error="resolution failed")
        for _ in range(2):
            out = venv_sync.sync(root)
            assert out["state"] == "failed" and not out["ok"]
            assert "resolution failed" in out["detail"]
        assert calls == [(root, True), (root, True)]


class TestSealedTrees:
    def test_a_sealed_tree_is_a_clean_noop(self, tmp_path, monkeypatch):
        """The desktop payload and nix bundle must not fail, must not sync."""
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "abc123", "payload": "full", "updateMechanism": "electron-updater"})
        )
        calls = _wire_pm(monkeypatch)

        out = venv_sync.sync(root)

        assert out == {"state": "sealed", "ok": True}
        assert calls == []

    def test_a_dev_tree_with_both_stamp_and_git_is_a_checkout(
        self, tmp_path, monkeypatch
    ):
        root = _checkout(tmp_path)
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "abc", "updateMechanism": "electron-updater"})
        )
        _wire_pm(monkeypatch)

        assert venv_sync.sync(root)["state"] == "synced"

    def test_a_stamp_without_update_mechanism_is_a_build_lane_bug(self, tmp_path):
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(json.dumps({"commit": "abc"}))
        with pytest.raises(RuntimeError, match="updateMechanism"):
            venv_sync.sync(root)


class TestCliContract:
    def test_json_output_and_exit_codes(self, tmp_path):
        """post_update and the installers read exactly this."""
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "x", "updateMechanism": "electron-updater"})
        )

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.venv_sync",
                "--project-root",
                str(root),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {"state": "sealed", "ok": True}

    def test_failure_exits_nonzero(self, tmp_path):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.venv_sync",
                "--project-root",
                str(tmp_path),  # empty dir: no pyproject, no stamp
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        assert proc.returncode == 1
        assert json.loads(proc.stdout)["state"] == "failed"

def test_every_failed_tail_path_counts_an_attempt(tmp_path, monkeypatch):
    """The pending marker is armed BEFORE the tail runs, so the marker alone cannot
    tell "never tried" from "tried and failed N times" -- the counter beside it is the
    only record that can. Only a nonzero child exit used to write one, so a dependency
    sync that RAISED left the marker with no counter; completion_retry_state then read
    attempts=0 ("never tried") and the cap and its backoff never engaged, re-running the
    whole tail on every launch. Measured shape on sma: a pending marker with no
    source-completion-attempts file beside it.

    Patched at the boundary so this proves the recording, not a real dependency build.
    """
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(venv_sync, "completion_pending_path",
                        lambda project: tmp_path / "source-completion-pending")

    def raising(*_args, **_kwargs):
        raise OSError("the dependency sync could not reach the index")

    monkeypatch.setattr(venv_sync, "_finish_source_update_attempted", raising)

    record = tmp_path / "source-completion-attempts"
    for expected in ("1", "2", "3"):
        with pytest.raises(OSError):
            venv_sync._finish_source_update(root, current=True, pending=tmp_path / "pending")
        assert record.read_text(encoding="utf-8").strip() == expected

    # Three consecutive failures: two past the first, so the backoff window is in force.
    may_retry, attempts, backoff = venv_sync.completion_retry_state(root)
    assert (may_retry, attempts) == (False, 3)
    assert backoff > 0

    # A success clears the record, so a later failure starts counting again. Production
    # does that with clear_completion() inside the tail, which is what the stub mirrors.
    monkeypatch.setattr(venv_sync, "_finish_source_update_attempted",
                        lambda root_, **_k: venv_sync.clear_completion(root_))
    venv_sync._finish_source_update(root, current=True, pending=tmp_path / "pending")
    assert not record.exists(), "a success must clear the attempt record"
    assert venv_sync.completion_retry_state(root) == (True, 0, 0)

    # And the record comes back on the next failure: the count restarts, never sticks.
    monkeypatch.setattr(venv_sync, "_finish_source_update_attempted", raising)
    with pytest.raises(OSError):
        venv_sync._finish_source_update(root, current=True, pending=tmp_path / "pending")
    assert record.read_text(encoding="utf-8").strip() == "1"


def test_the_cap_stops_self_starting_once_attempts_are_recorded(tmp_path, monkeypatch):
    """With the counter engaging, the existing cap finally bites: past
    COMPLETION_RETRY_MAX_ATTEMPTS no launch may re-run the tail on its own."""
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(venv_sync, "completion_pending_path",
                        lambda project: tmp_path / "source-completion-pending")
    (tmp_path / "source-completion-attempts").write_text(
        f"{venv_sync.COMPLETION_RETRY_MAX_ATTEMPTS}\n", encoding="utf-8")

    may_retry, attempts, _backoff = venv_sync.completion_retry_state(root)

    assert (may_retry, attempts) == (False, venv_sync.COMPLETION_RETRY_MAX_ATTEMPTS)
