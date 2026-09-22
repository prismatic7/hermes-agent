"""Regression: the FOREIGN-holder guard must fire on macOS, not just Linux.

`hermes_cli.backup._foreign_db_holder_pids` used to return ``None`` on every
non-Linux platform. Its callers read that as "no holders" (``if holders:``),
so on macOS the unlink+move restore path had NO holder protection at all: it
would unlink a live database and its ``-wal``/``-shm`` sidecars while another
process still had them open. That is precisely the #90950 WAL split-brain
generator behind the 2026-09-19/20/21 lcm.db corruptions.

These tests hold a database open from a REAL second process (not merely an
in-process connection — the existing own-holder test covers that case) and
require the guard to refuse.

Requires ``lsof``, which ships with macOS and every mainstream Linux.
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import backup as backup_mod


def _make_db(path, marker):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def _has_lsof():
    try:
        subprocess.run(["lsof", "-v"], capture_output=True)
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _has_lsof(), reason="lsof not installed")


class _ForeignHolder:
    """A second process holding *db_path* read-write for longer than the scan takes."""

    def __init__(self, db_path, hold_seconds=30):
        self.db_path = str(db_path)
        self.hold_seconds = hold_seconds
        self.proc = None

    def __enter__(self):
        code = (
            "import sqlite3, sys, time\n"
            "c = sqlite3.connect(sys.argv[1])\n"
            "c.execute('PRAGMA journal_mode=WAL')\n"
            "c.execute('CREATE TABLE IF NOT EXISTS t (v TEXT)')\n"
            "c.execute(\"INSERT INTO t VALUES ('foreign-write')\")\n"
            "c.commit()\n"
            "sys.stdout.write('ready'); sys.stdout.flush()\n"
            "time.sleep(float(sys.argv[2]))\n"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, self.db_path, str(self.hold_seconds)],
            stdout=subprocess.PIPE,
            text=True,
        )
        # Block until the child itself confirms it holds the file open.
        assert self.proc.stdout.read(5) == "ready"
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        return False


def test_foreign_holder_scan_finds_second_process(tmp_path):
    """The scan must see a holder in ANOTHER process on this platform."""
    dst = tmp_path / "state.db"
    _make_db(dst, "live-old")
    with _ForeignHolder(dst) as holder:
        pids = backup_mod._foreign_db_holder_pids(dst)
        assert pids is not None, "holder scan unavailable — guard would fail OPEN"
        assert holder.proc.pid in pids


def test_foreign_holder_scan_covers_sidecars(tmp_path):
    """A process holding only the WAL is still a holder (#90950 fingerprint)."""
    dst = tmp_path / "state.db"
    _make_db(dst, "live-old")
    with _ForeignHolder(dst):
        # The DB itself is held, but the watched set must cover both sidecars.
        watched = backup_mod._watched_db_paths(dst)
        assert watched == {
            backup_mod._canonical_watched_path(str(dst)),
            backup_mod._canonical_watched_path(str(dst)) + "-wal",
            backup_mod._canonical_watched_path(str(dst)) + "-shm",
        }


def test_foreign_holder_scan_ignores_own_pid(tmp_path):
    """This process's own handle is not a FOREIGN holder (caller handles self)."""
    dst = tmp_path / "state.db"
    _make_db(dst, "live-old")
    held = sqlite3.connect(str(dst))
    try:
        held.execute("PRAGMA journal_mode=WAL")
        held.execute("INSERT INTO t VALUES ('self')")
        held.commit()
        pids = backup_mod._foreign_db_holder_pids(dst)
        assert pids is not None
        assert os.getpid() not in pids
    finally:
        held.close()


def test_restore_refuses_to_unlink_sidecar_held_by_live_process(tmp_path):
    """The acceptance test: restore must fail closed and leave the WAL untouched.

    The inode-preserving ``sqlite3.backup()`` primary path is safe under a live
    holder, so to reach the ``_unlink_move_restore_db`` fallback — the path that
    actually unlinks sidecars — the destination header must be unreadable.
    """
    dst = tmp_path / "state.db"
    src = tmp_path / "snap.db"
    _make_db(src, "snapshot-good")
    _make_db(dst, "live-old")
    with _ForeignHolder(dst) as holder:
        assert holder.proc is not None and holder.proc.pid
        # Offline-fixture mutation: doom the destination header so the backup()
        # API fails and the restore routes into the unlink+move fallback.
        with open(dst, "r+b") as fh:
            fh.write(b"\x00" * 100)

        wal = dst.with_name(dst.name + "-wal")
        assert wal.exists()
        wal_ino = wal.stat().st_ino

        assert backup_mod._safe_restore_db(src, dst) is False

        # The live generation must survive: same inode, sidecar still present.
        assert wal.exists() and wal.stat().st_ino == wal_ino

    # Sanity: with the holder gone, the same call succeeds.
    deadline = time.time() + 10
    while time.time() < deadline and backup_mod._foreign_db_holder_pids(dst):
        time.sleep(0.2)
    assert backup_mod._safe_restore_db(src, dst) is True
    conn = sqlite3.connect(str(dst))
    try:
        assert conn.execute("SELECT v FROM t LIMIT 1").fetchone()[0] == "snapshot-good"
    finally:
        conn.close()


def test_unlink_move_restore_refuses_when_scan_unavailable(tmp_path, monkeypatch):
    """A scan that cannot RUN must refuse, not assume "nobody holds it".

    ``None`` from ``_foreign_db_holder_pids`` means the scan was UNAVAILABLE (no
    ``lsof``, ``lsof`` failed, ``/proc`` unreadable). On macOS ``lsof`` also exits 0
    while warning that it "can't stat()" a stale network mount, so a partial scan can
    omit the real holder. Reading ``None`` as "no holders" is the macOS fail-open that
    produced the split-brain; the destructive unlink+move path must fail closed.
    """
    dst = tmp_path / "state.db"
    src = tmp_path / "snap.db"
    _make_db(src, "snapshot-good")
    _make_db(dst, "live-old")
    # Keep a connection OPEN: the -wal sidecar only exists while a connection holds it
    # (a clean close checkpoints and unlinks it).
    holder = sqlite3.connect(str(dst))
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("INSERT INTO t VALUES ('x')")
        wal = dst.with_name(dst.name + "-wal")
        assert wal.exists()
        wal_ino = wal.stat().st_ino

        monkeypatch.setattr(backup_mod, "_foreign_db_holder_pids", lambda _p: None)

        assert backup_mod._unlink_move_restore_db(src, dst) is False
        # Nothing was unlinked: the live generation is intact.
        assert wal.exists() and wal.stat().st_ino == wal_ino
    finally:
        holder.close()
