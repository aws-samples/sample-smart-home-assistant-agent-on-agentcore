"""Turning Memory events into a transcript — the filter is the whole test.

`role` looks like it answers "who spoke" and does not. The Bedrock Converse format
requires a `toolResult` to travel inside a **USER-role** message and a `toolUse`
inside an **ASSISTANT-role** one, so filtering on role alone renders tool output as
the user talking. Measured on one real 62-event session:

    ASSISTANT  text          11
    ASSISTANT  toolUse       23
    USER       text           6
    USER       toolResult    13
    None       (blob)        23

53 of those pass a role filter and only 17 are conversation. The fixtures below are
that exact shape.
"""
import json
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

import chat_history  # noqa: E402


def _env(role, blocks):
    """A conversational payload whose text is a JSON Strands envelope."""
    return {"conversational": {
        "role": role,
        "content": {"text": json.dumps({"message": {"role": role.lower(),
                                                    "content": blocks}})},
    }}


def _event(ts, *payloads):
    return {"eventTimestamp": ts, "payload": list(payloads)}


# --- the role trap ---------------------------------------------------------

def test_a_tool_result_is_not_rendered_as_the_user_speaking():
    """The whole reason role alone is insufficient."""
    events = [_event("2026-01-01T00:00:01Z",
                     _env("USER", [{"toolResult": {"toolUseId": "t1",
                                                   "status": "success",
                                                   "content": [{"text": "{...}"}]}}]))]
    assert chat_history.messages_from_events(events) == []


def test_a_tool_use_is_not_rendered_as_an_assistant_reply():
    events = [_event("2026-01-01T00:00:01Z",
                     _env("ASSISTANT", [{"toolUse": {"toolUseId": "t1",
                                                     "name": "discover_devices",
                                                     "input": {}}}]))]
    assert chat_history.messages_from_events(events) == []


def test_a_blob_payload_is_never_conversation():
    """Strands writes agent state (the injected skills XML) as a blob payload.
    23 of the 62 events in the measured session were these."""
    events = [_event("2026-01-01T00:00:01Z",
                     {"blob": '{"agent_id": "default", "state": {...}}'})]
    assert chat_history.messages_from_events(events) == []


def test_real_text_survives_the_filter():
    events = [
        _event("2026-01-01T00:00:01Z", _env("USER", [{"text": "把灯打开"}])),
        _event("2026-01-01T00:00:02Z", _env("ASSISTANT", [{"text": "已打开"}])),
    ]
    got = chat_history.messages_from_events(events)
    assert [(m["role"], m["text"]) for m in got] == [
        ("user", "把灯打开"), ("assistant", "已打开")]


def test_a_mixed_session_keeps_only_the_conversation():
    """The measured ratio: 17 conversational out of a 62-event session."""
    events = [
        _event("t1", _env("USER", [{"text": "audit my home"}])),
        _event("t2", {"blob": "state"}),
        _event("t3", _env("ASSISTANT", [{"toolUse": {"toolUseId": "a", "name": "x"}}])),
        _event("t4", _env("USER", [{"toolResult": {"toolUseId": "a"}}])),
        _event("t5", _env("ASSISTANT", [{"text": "here is the audit"}])),
    ]
    got = chat_history.messages_from_events(events)
    assert [m["role"] for m in got] == ["user", "assistant"]
    assert got[1]["text"] == "here is the audit"


def test_roles_outside_user_and_assistant_are_dropped():
    """`TOOL` and `OTHER` are in the API enum. Strands never writes them, so one
    appearing is unexpected — and an unattributed bubble is worse than omission."""
    for role in ("TOOL", "OTHER"):
        events = [_event("t1", _env(role, [{"text": "something"}]))]
        assert chat_history.messages_from_events(events) == [], role


# --- payload shapes --------------------------------------------------------

def test_a_bare_string_is_accepted_because_voice_writes_one():
    """`voice_session.persist_voice_transcript` writes plain text, not an envelope.
    Requiring the envelope would silently drop every voice turn."""
    events = [_event("t1", {"conversational": {
        "role": "USER", "content": {"text": "开一下客厅的灯"}}})]
    got = chat_history.messages_from_events(events)
    assert [(m["role"], m["text"]) for m in got] == [("user", "开一下客厅的灯")]


def test_empty_and_missing_text_is_dropped_not_rendered_blank():
    for content in ({"text": ""}, {"text": "   "}, {}):
        events = [_event("t1", {"conversational": {"role": "USER",
                                                   "content": content}})]
        assert chat_history.messages_from_events(events) == [], content


def test_multiple_text_blocks_in_one_message_are_joined():
    events = [_event("t1", _env("ASSISTANT", [{"text": "line one"},
                                              {"text": "line two"}]))]
    assert chat_history.messages_from_events(events)[0]["text"] == "line one\nline two"


def test_a_message_with_text_and_a_tool_use_keeps_only_the_text():
    events = [_event("t1", _env("ASSISTANT", [
        {"text": "checking your devices"},
        {"toolUse": {"toolUseId": "a", "name": "discover_devices"}}]))]
    got = chat_history.messages_from_events(events)
    assert got[0]["text"] == "checking your devices"


# --- ordering and turn collapsing -----------------------------------------

def test_output_is_oldest_first_even_though_the_api_returns_newest_first():
    events = [
        _event("2026-01-01T00:00:09Z", _env("ASSISTANT", [{"text": "second"}])),
        _event("2026-01-01T00:00:01Z", _env("USER", [{"text": "first"}])),
    ]
    got = chat_history.messages_from_events(events)
    assert [m["text"] for m in got] == ["first", "second"]


def test_consecutive_assistant_messages_collapse_into_one_turn():
    """A streamed reply persisted in chunks must not render as five bubbles."""
    msgs = [
        {"role": "user", "text": "hi", "timestamp": "t1"},
        {"role": "assistant", "text": "part one", "timestamp": "t2"},
        {"role": "assistant", "text": "part two", "timestamp": "t3"},
    ]
    turns = chat_history.turns_from_messages(msgs)
    assert len(turns) == 2
    assert turns[1]["text"] == "part one\npart two"


def test_consecutive_user_messages_are_kept_separate():
    """Nothing splits a user prompt across events, so a run of them means the user
    really did send several before getting a reply. Joining them would rewrite what
    happened into one message they never sent."""
    msgs = [
        {"role": "user", "text": "把灯打开", "timestamp": "t1"},
        {"role": "user", "text": "在吗?", "timestamp": "t2"},
    ]
    turns = chat_history.turns_from_messages(msgs)
    assert [t["text"] for t in turns] == ["把灯打开", "在吗?"]


# --- walking sessions -----------------------------------------------------

def _client(events_by_session, sessions=None):
    c = MagicMock()
    c.list_events.side_effect = lambda **kw: {
        "events": events_by_session.get(kw["sessionId"], [])}
    if sessions is not None:
        c.list_sessions.return_value = {"sessionSummaries": sessions}
    return c


def test_it_walks_back_through_sessions_until_the_limit_is_met():
    """One login session held ~6 real turns in the measured data, so asking for 20
    has to span sessions — which also backfills history written before the memory
    session id became stable."""
    by_session = {
        "s-new": [_event("t9", _env("USER", [{"text": "newest"}]))],
        "s-old": [_event("t1", _env("USER", [{"text": "older"}])),
                  _event("t2", _env("ASSISTANT", [{"text": "reply"}]))],
    }
    turns, read = chat_history.recent_turns(
        _client(by_session), "mem", "actor", 10, ["s-new", "s-old"])
    # Oldest first across sessions.
    assert [t["text"] for t in turns] == ["older", "reply", "newest"]
    assert read == ["s-new", "s-old"]


def test_it_stops_reading_once_the_limit_is_reached():
    """A user with 56 historical sessions must not cost 56 ListEvents calls."""
    by_session = {f"s{i}": [_event(f"t{i}", _env("USER", [{"text": f"m{i}"}]))]
                  for i in range(10)}
    client = _client(by_session)
    turns, read = chat_history.recent_turns(
        client, "mem", "actor", 2, [f"s{i}" for i in range(10)])
    assert len(turns) == 2
    assert client.list_events.call_count <= 3, client.list_events.call_count


def test_an_unreadable_session_does_not_empty_the_transcript():
    """The newest session is the one most likely to be mid-write."""
    client = MagicMock()

    def events(**kw):
        if kw["sessionId"] == "s-bad":
            raise RuntimeError("throttled")
        return {"events": [_event("t1", _env("USER", [{"text": "kept"}]))]}

    client.list_events.side_effect = events
    turns, read = chat_history.recent_turns(
        client, "mem", "actor", 10, ["s-bad", "s-good"])
    assert [t["text"] for t in turns] == ["kept"]


def test_sessions_are_sorted_explicitly_because_the_api_does_not():
    """Measured: `list_sessions` does not return them in order. Trusting the API's
    order shows a three-week-old conversation as "recent"."""
    client = _client({}, sessions=[
        {"sessionId": "middle", "createdAt": "2026-08-11T00:00:00Z"},
        {"sessionId": "newest", "createdAt": "2026-08-13T00:00:00Z"},
        {"sessionId": "oldest", "createdAt": "2026-08-01T00:00:00Z"},
    ])
    assert chat_history.sessions_newest_first(client, "mem", "actor") == [
        "newest", "middle", "oldest"]


def test_the_limit_returns_the_most_recent_turns_not_the_first():
    roles = ["USER", "ASSISTANT"]
    by_session = {"s1": [
        _event(f"t{i}", _env(roles[i % 2], [{"text": f"m{i}"}])) for i in range(1, 6)]}
    turns, _ = chat_history.recent_turns(
        _client(by_session), "mem", "actor", 2, ["s1"])
    assert [t["text"] for t in turns] == ["m4", "m5"]
