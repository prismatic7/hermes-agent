"""Reasoning promoted to the answer must be screened for leaked tool-call markup.

Regression for the 2026-09-27 `custodian:cron-health` delivery (job 55a020e5fe8c, 08:01): the model
emitted an invalid `tool_call` twice, then stopped cleanly with the whole answer in the reasoning
channel. The promotion path returned that chain-of-thought verbatim and Chris's channel received a
raw tool-call blob.

The fixture is the delivered text byte-for-byte, read from
`~/.hermes/cron/output/55a020e5fe8c/2026-09-27_08-01-11.md` line 498 onward.

Both reasoning-delivery paths are covered because they are separate: the promotion in
``turn_final_response`` (which fires when reasoning is promoted to the visible answer) and
``turn_empty_response._terminal_empty`` (which surfaces reasoning ONLY on the exhausted-retries
path). Covering one does not cover the other.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Collection-time import: ``agent.conversation_loop`` reaches ``hermes_bootstrap`` ->
# ``pm.environments.payload_venv``, which stats ``<repo>/../manifest.json``. Where the repo is
# checked out inside a Hermes home (here), that stat is I/O against the real home and trips the
# ``_forbid_real_hermes_home_io`` guard — but only for imports that happen DURING a test, since the
# guard is installed per-test. Importing it here (module scope, before the guard exists) lets the
# lazy imports inside ``finish_text_response`` resolve from ``sys.modules``.
import agent.conversation_loop  # noqa: F401  (see comment above)
from agent import turn_empty_response as ter
from agent.codex_responses_adapter import (
    _leaked_tool_call_text, reasoning_carries_leaked_call,
)
from agent.turn_final_response import finish_text_response

# The exact delivered blob (byte-exact; the trailing newline is part of the file).
BLOB = (
    "We need to follow the instructions carefully for the custodian-health-checks skill. The user "
    "asks to run cron health check: call the `custodian_cron_health` tool with dry_run=false. It's "
    "an agent tool, not a terminal command.\n"
    "\n"
    "We attempted to call it but got a JSON error due to invalid calls syntax. We need to format "
    "the tool_call correctly.\n"
    "\n"
    "The tool_call expects a \"calls\" array with each entry having \"name\" and \"arguments\". The "
    "example we wrote used a list with a dict; but perhaps the syntax is wrong. The spec says:\n"
    "\n"
    "```\n"
    "<function=tool_call>\n"
    "<parameter=calls>\n"
    "[{\"name\": \"custodian_cron_health\", \"arguments\": {\"dry_run\": false}}\n"
    "</parameter>\n"
)

# Inside ``_terminal_empty``'s 500-char reasoning preview (which is what ``site_copy`` echoes), and
# distinctive from any operator-facing copy.
BLOB_PREVIEW_MARK = "The tool_call expects a \"calls\" array"


def test_fixture_is_the_real_delivered_blob():
    """Guard the fixture itself: if it drifts, the regression stops proving anything."""
    assert len(BLOB) == 649
    assert BLOB.startswith("We need to follow the instructions carefully")
    assert BLOB.endswith("<parameter=calls>\n[{\"name\": \"custodian_cron_health\""
                         ", \"arguments\": {\"dry_run\": false}}\n</parameter>\n")
    assert "JSON error" in BLOB
    assert BLOB.index(BLOB_PREVIEW_MARK) < 500  # the preview marker really is inside the preview


# ---- the screen ------------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", [
    "<function=tool_call>\n<parameter=calls>\n[{\"name\": \"x\"}]"
    , "  <function=terminal>",
    "\t<parameter=command>",
    "Calling tool now to=functions.terminal {\"command\": \"ls\"}",
])
def test_screen_flags_unambiguous_call_markup(dialect):
    assert reasoning_carries_leaked_call(dialect) is True


@pytest.mark.parametrize("prose", [
    "We need to check the disk, then report.",
    # Mid-sentence mentions stay legitimate: only line-start markup is a call.
    "The spec means the model wrote <function=tool_call> inline.",
    "See the docs for <parameter=calls> handling.",
    # Context-sensitive Codex-CLI shape needs a lead-in; reasoning discussing a command is not a call.
    "Running it now.\n{\"cmd\": \"df -h\"}",
    "",
])
def test_screen_leaves_prose_alone(prose):
    assert reasoning_carries_leaked_call(prose) is False


def test_screen_flags_the_real_blob():
    assert reasoning_carries_leaked_call(BLOB) is True


def test_codex_cli_shape_still_detected_no_regression():
    """The pre-existing classifier must keep working: it is what the Codex responses path calls."""
    cases = [
        "Calling tool now to=functions.terminal {\"command\": \"ls\"}",
        "Sure, let me run the tests.\n{\"cmd\": \"pytest -q\", \"workdir\": \"/repo\"}",
    ]
    for text in cases:
        assert _leaked_tool_call_text(text) is True, text
    # And the native dialect is deliberately NOT added to that classifier: the markup is also the
    # prompt's own format spec, so a model quoting its instructions must stay a legitimate answer.
    assert _leaked_tool_call_text(BLOB) is False


# ---- delivery path 1: the promotion in turn_final_response ------------------------------------

def _promotion_agent(reasoning: str):
    agent = MagicMock()
    agent.model = "nemotron-3-nano:30b-cloud"
    agent.provider = "custom"
    agent._extract_reasoning.return_value = reasoning
    # Real semantics: True when the text carries visible content. A plain ``False`` here would send
    # EVERY response down the empty-response ladder and make the promotion assertions vacuous.
    agent._has_content_after_think_block.side_effect = lambda t: bool((t or "").strip())
    agent._strip_think_blocks.side_effect = lambda t: t or ""
    return agent


def _run_promotion(agent):
    """Drive finish_text_response far enough to observe what it returns for delivery.

    ``turn_final_response`` imports ``recover_empty_response`` by name, so the ladder must be
    patched where it was bound, not on the importing module.
    """
    assistant_message = SimpleNamespace(content="", tool_calls=[], reasoning=BLOB, reasoning_content=None)
    sentinel = ter.EmptyResponseVerdict(
        action="break", result=None, final_response="(recovery ladder ran)",
        turn_exit_reason="x", active_system_prompt="sys", preflight_compression_blocked=False,
        api_call_count=3,
    )
    with patch("agent.turn_final_response.recover_empty_response", return_value=sentinel) as ladder:
        verdict = finish_text_response(
            agent, assistant_message=assistant_message, response=None, finish_reason="stop",
            messages=[], api_messages=[], conversation_history=[], api_call_count=3,
            user_message="run the cron health check", active_system_prompt="sys",
            final_response="", _turn_exit_reason=None, _preflight_compression_blocked=False,
            codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None, _pending_verification_response_previewed=False,
            effective_task_id="t",
        )
    return verdict, ladder


def test_promotion_withholds_reasoning_that_is_a_leaked_tool_call():
    agent = _promotion_agent(BLOB)
    verdict, ladder = _run_promotion(agent)
    # The blob must not be what gets delivered.
    assert verdict.final_response != BLOB
    assert BLOB not in (verdict.final_response or "")
    assert "<function=tool_call>" not in (verdict.final_response or "")
    # Withholding means the empty-response ladder runs instead, which re-elicits a real call.
    ladder.assert_called_once()


def test_promotion_still_promotes_ordinary_reasoning():
    """The screen must not become a blanket ban on reasoning promotion."""
    agent = _promotion_agent("The disk is fine; nothing to report.")
    verdict, ladder = _run_promotion(agent)
    assert verdict.final_response == "The disk is fine; nothing to report."
    ladder.assert_not_called()


def test_promotion_is_what_leaked_without_the_screen():
    """Falsification companion: with the screen neutralised the blob IS delivered.

    Proves the assertion above is load-bearing rather than vacuous — the promotion is exactly the
    path that put the blob in the channel.
    """
    agent = _promotion_agent(BLOB)
    with patch("agent.turn_final_response.reasoning_carries_leaked_call", return_value=False):
        verdict, ladder = _run_promotion(agent)
    # ``finish_text_response`` strips the answer before delivery; compare on that basis.
    assert verdict.final_response == BLOB.strip()
    assert "<function=tool_call>" in verdict.final_response
    ladder.assert_not_called()


# ---- delivery path 2: turn_empty_response._terminal_empty -------------------------------------

def _empty_agent(reasoning: str):
    agent = MagicMock()
    agent.model = "nemotron-3-nano:30b-cloud"
    agent.provider = "custom"
    agent._empty_content_retries = 3
    agent._extract_reasoning.return_value = reasoning
    agent._build_assistant_message.return_value = {"role": "assistant"}
    # getattr() on a MagicMock auto-creates a Mock, which then fails `cost > 0`; the guard reads
    # this attr for the streak-cost status line.
    agent._empty_streak_cost_usd = None
    return agent


def test_terminal_empty_withholds_reasoning_that_is_a_leaked_tool_call():
    agent = _empty_agent(BLOB)
    messages = []
    final = ter._terminal_empty(agent, SimpleNamespace(), "stop", messages)
    assert BLOB not in final
    assert BLOB_PREVIEW_MARK not in final
    assert "<function=tool_call>" not in final
    # Still says what happened and what to do — withholding must not swallow the diagnosis.
    assert "nemotron-3-nano:30b-cloud" in final
    assert "/retry" in final
    # The persisted row keeps the sentinel; the withheld text is only kept out of the DELIVERY text.
    assert messages[-1]["_empty_terminal_sentinel"] is True


def test_terminal_empty_still_surfaces_ordinary_reasoning():
    agent = _empty_agent("The disk is fine; nothing to report.")
    final = ter._terminal_empty(agent, SimpleNamespace(), "stop", [])
    assert "The disk is fine; nothing to report." in final


def test_terminal_empty_is_what_leaked_without_the_screen():
    """Falsification companion for the second path: without the screen the preview is delivered."""
    agent = _empty_agent(BLOB)
    with patch("agent.turn_empty_response.reasoning_carries_leaked_call", return_value=False):
        final = ter._terminal_empty(agent, SimpleNamespace(), "stop", [])
    assert BLOB_PREVIEW_MARK in final
