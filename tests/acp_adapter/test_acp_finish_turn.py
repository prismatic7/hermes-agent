"""Tests for ACP turn finalisation — transformed-after-streaming responses.

A plugin hook (``transform_llm_output``) can rewrite the final response after
the original text was already streamed chunk-by-chunk. The adapter must not
re-send the whole transformed response (that duplicates it in the client);
it should send only the delta, mirroring ``cli._post_stream_transform_output``.
"""

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self._supports_active_turn_redirect = True
        self.steers = []
        self.redirects = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        return None


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


def _text_updates(conn):
    """Return the text payloads of agent_message_chunk updates, in order."""
    texts = []
    for _session_id, update in conn.updates:
        if getattr(update, "session_update", None) == "agent_message_chunk":
            content = getattr(update, "content", None)
            if content is not None and getattr(content, "type", None) == "text":
                texts.append(getattr(content, "text", ""))
    return texts


@pytest.mark.asyncio
async def test_finish_turn_transformed_after_stream_sends_only_delta():
    """When the response was streamed and then transformed, only the delta is sent."""
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True

    result = {
        "final_response": "original answer\n\n[plugin appended this]",
        "messages": [{"role": "assistant", "content": "original answer\n\n[plugin appended this]"}],
        "response_transformed": True,
        "pre_transform_response": "original answer",
    }
    await acp_agent._finish_turn(state, state.session_id, conn, result, None, streamed_message=True)

    texts = _text_updates(conn)
    assert texts == ["\n\n[plugin appended this]"]


@pytest.mark.asyncio
async def test_finish_turn_transformed_replacement_after_stream_marks_it():
    """A full replacement (no shared prefix) is sent with a marker, not silently dropped."""
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True

    result = {
        "final_response": "XYZ",
        "messages": [{"role": "assistant", "content": "XYZ"}],
        "response_transformed": True,
        "pre_transform_response": "abc",
    }
    await acp_agent._finish_turn(state, state.session_id, conn, result, None, streamed_message=True)

    texts = _text_updates(conn)
    assert texts == ["\n[Response transformed after streaming]\nXYZ"]


@pytest.mark.asyncio
async def test_finish_turn_not_streamed_sends_full_response():
    """Without streaming, the full final response is sent once."""
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True

    result = {
        "final_response": "full answer",
        "messages": [{"role": "assistant", "content": "full answer"}],
    }
    await acp_agent._finish_turn(state, state.session_id, conn, result, None, streamed_message=False)

    texts = _text_updates(conn)
    assert texts == ["full answer"]


@pytest.mark.asyncio
async def test_finish_turn_streamed_untransformed_sends_nothing():
    """A streamed, untransformed response must not be re-sent."""
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True

    result = {
        "final_response": "streamed answer",
        "messages": [{"role": "assistant", "content": "streamed answer"}],
    }
    await acp_agent._finish_turn(state, state.session_id, conn, result, None, streamed_message=True)

    texts = _text_updates(conn)
    assert texts == []
