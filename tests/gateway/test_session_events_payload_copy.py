"""A session-event payload that cannot be deepcopied must still reach subscribers.

``SessionEvents.publish`` deepcopies each frame to isolate subscribers from a publisher that
mutates its payload afterwards, and terminal publication is attempted once. When the copy
protocol could not rebuild a payload (a ``str`` subclass whose ``__new__`` takes two arguments),
the raise happened before the frame went out and the viewer never saw ``message.complete``: the
turn settled server-side while the UI kept rendering its last tool call as running.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gateway.session_events import _copy_payload


def test_copyable_payload_is_isolated():
    payload = {"text": ["a"]}

    copied = _copy_payload(payload, "sess", "message.complete")

    assert copied == payload
    assert copied is not payload


def test_uncopyable_payload_publishes_the_original(caplog):
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise TypeError("cannot rebuild this")

    payload = Uncopyable()

    assert _copy_payload(payload, "sess", "message.complete") is payload
    assert "was not copyable" in caplog.text
