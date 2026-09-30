"""The gateway announces a pm plugin eviction once, at boot.

Eviction fires during a restart with no operator present, and a warning-free sync
overwrites latest.json within minutes, so the boot path is the reliable moment to
surface it. A crash-looping gateway must not re-announce the same receipt.
"""
import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.run_notifications import GatewayNotificationsMixin


class _Transport:
    async def send(self, *args, **kwargs):
        return SimpleNamespace(success=True)


class _Runner(GatewayNotificationsMixin):
    """Just enough surface for the notice method."""

    def __init__(self):
        self.sent = []

    def _home_channel_transports(self):
        home = SimpleNamespace(chat_id="C1", thread_id=None)
        cfg = SimpleNamespace(gateway_restart_notification=True)
        yield SimpleNamespace(value="telegram"), cfg, home, _Transport()

    async def _send_home_channel_message(self, platform, home, transport, message, failure_fmt):
        self.sent.append(message)
        return True


def _rows(*messages):
    return [{"message": m, "at": f"T{index}", "receipt": f"pm_{index}.json", "kind": "sync"}
            for index, m in enumerate(messages)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the method's marker at tmp_path without importing gateway.run."""
    stub = types.ModuleType("gateway.run")
    stub._hermes_home = tmp_path
    monkeypatch.setitem(sys.modules, "gateway.run", stub)
    return tmp_path


@pytest.fixture
def warnings(monkeypatch):
    """Return a setter for pm.receipt.recent_warnings."""
    from pm import receipt as pm_receipt

    def set_rows(rows):
        monkeypatch.setattr(pm_receipt, "recent_warnings", lambda limit=5: rows[:limit])

    return set_rows


def test_a_first_boot_announces_the_newest_warning_only(home, warnings):
    """First run on a host: the window may hold several; announce the newest."""
    warnings(_rows("newest eviction", "older eviction"))
    runner = _Runner()

    asyncio.run(runner._send_pm_eviction_notice())

    assert len(runner.sent) == 1
    assert "newest eviction" in runner.sent[0]
    assert "older eviction" not in runner.sent[0]


def test_the_same_receipt_is_never_announced_twice(home, warnings):
    """A crash-looping gateway must not re-announce one eviction every boot."""
    warnings(_rows("eviction one"))
    runner = _Runner()

    asyncio.run(runner._send_pm_eviction_notice())
    assert len(runner.sent) == 1

    asyncio.run(runner._send_pm_eviction_notice())
    assert len(runner.sent) == 1, "second boot re-announced the same receipt"
    assert json.loads((home / ".pm_eviction_notice.json").read_text())["last_receipt"] == "pm_0.json"


def test_a_new_receipt_is_announced_after_the_marker(home, warnings):
    """A later eviction must still get through."""
    warnings(_rows("first"))
    runner = _Runner()
    asyncio.run(runner._send_pm_eviction_notice())

    warnings([{"message": "second", "at": "T9", "receipt": "pm_9.json", "kind": "sync"},
              {"message": "first", "at": "T0", "receipt": "pm_0.json", "kind": "sync"}])
    asyncio.run(runner._send_pm_eviction_notice())

    assert any("second" in message for message in runner.sent), runner.sent
    assert len(runner.sent) == 2


def test_no_warnings_sends_nothing(home, warnings):
    warnings([])
    runner = _Runner()

    asyncio.run(runner._send_pm_eviction_notice())

    assert runner.sent == []
    assert not (home / ".pm_eviction_notice.json").exists()


def test_a_broken_receipt_never_blocks_startup(home, warnings, monkeypatch):
    """Best-effort: a failure here must not propagate into the boot path."""
    from pm import receipt as pm_receipt

    def boom(limit=5):
        raise RuntimeError("receipt store exploded")

    monkeypatch.setattr(pm_receipt, "recent_warnings", boom)
    runner = _Runner()

    asyncio.run(runner._send_pm_eviction_notice())  # must not raise

    assert runner.sent == []
