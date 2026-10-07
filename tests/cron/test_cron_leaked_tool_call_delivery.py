"""A cron response that IS a serialized tool call must never be delivered as the answer.

Measured 2026-10-07 (custodian:escalation-runner, job 9d261ec25dda): the queue was EMPTY, so
the job's contract said the entire response should be ``[SILENT]``. Instead the model emitted
its intended tool call as plain text::

    {"name": "terminal", "arguments": {"command": "FILE=..."}}

430 chars of it were delivered to Chris's Discord channel as the "answer".

Why nothing caught it before: the existing leaked-call screen
(``codex_responses_adapter._leaked_tool_call_text``) was wired only to the Codex Responses
adapter and the reasoning-promotion path. This job runs ``nemotron-3-super:cloud`` — a
different transport entirely — and the response was neither reasoning nor a Codex output.
So the guard never ran.
"""
from __future__ import annotations

import json

import pytest

from agent.codex_responses_adapter import _leaked_tool_call_text
from cron.scheduler import _final_response_from_result


class _FakeAgent:
    """The delivery seam only passes this through; it is never dereferenced on these paths."""


def _deliver(text: str) -> str:
    return _final_response_from_result(
        {"final_response": text, "completed": True, "failed": False},
        "9d261ec25dda", "custodian:escalation-runner", _FakeAgent,
    )


# ── the third dialect: the whole response is a Chat-Completions-style call object ──
REAL_DELIVERED_BLOB = (
    '{\n  "name": "terminal",\n  "arguments": {\n    "command": "FILE=\"$HOME/.hermes/'
    'commons/data/ocas-custodian/issues.jsonl\"; if [ -s \"$FILE\" ]; then echo \"[SILENT]\"; '
    'fi"\n  }\n}'
)


@pytest.mark.parametrize("text", [
    REAL_DELIVERED_BLOB,
    json.dumps({"name": "terminal", "arguments": {"command": "ls"}}),
    json.dumps({"name": "search_files", "arguments": {"pattern": "x"}}),
    # arguments as an escaped JSON *string* rather than an object — the other half of the shape
    json.dumps({"name": "terminal", "arguments": json.dumps({"command": "ls"})}),
    "  " + json.dumps({"name": "terminal", "arguments": {"command": "ls"}}) + "  \n",
])
def test_serialized_call_object_is_detected(text):
    assert _leaked_tool_call_text(text) is True


@pytest.mark.parametrize("text", [
    "[SILENT]",
    "Cron health: 45/47 ok, 0 error, 0 paused (+1 parked intentionally)",
    "- **oc_disk_full** — tier 3, /Volumes/Backup at 94%. Act: prune snapshots.",
    # A legitimate answer may QUOTE the schema. These classifiers are deliberately separate
    # from the context-scored shell heuristic precisely so a quoted example survives.
    'A leaked call looks like {"name": "terminal", "arguments": {...}} in the body.',
    'The schema is:\n```json\n{"name":"terminal","arguments":{"command":"ls"}}\n```',
    # A real JSON answer that is not a call: no name+arguments pair.
    '{"query": "noema federation wedge", "limit": 5}',
    # Prose wrapping such an object, and partial shapes.
    'Result: {"name":"widget","arguments":{"count":3}} saved.',
    '{"name": "terminal"}',
    '{"arguments": {"command": "ls"}}',
    '',
])
def test_legitimate_answers_survive(text):
    assert _leaked_tool_call_text(text) is False


def test_delivery_seam_suppresses_the_leaked_call():
    """The suppressed response must come back EMPTY, which routes cron to the [SILENT] path."""
    assert _deliver(REAL_DELIVERED_BLOB).strip() == ""


@pytest.mark.parametrize("text", [
    "[SILENT]",
    "Cron health: 45/47 ok, 0 error, 0 paused (+1 parked intentionally)",
    "- **oc_disk_full** — tier 3, /Volumes/Backup at 94%. Act: prune snapshots.",
    'A leaked call looks like {"name": "terminal", "arguments": {...}} in the body.',
])
def test_delivery_seam_preserves_real_reports(text):
    assert _deliver(text) == text
