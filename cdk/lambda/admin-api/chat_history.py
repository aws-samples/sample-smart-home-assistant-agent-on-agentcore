"""Turning AgentCore Memory events back into a readable transcript.

The chatbot shows a user their recent conversation on login. Short-term memory
already holds it, but not in a shape you can render — and the gap between the two
is where every mistake here lives.

**`role` does not mean "who spoke".** The API's enum is
`ASSISTANT | USER | TOOL | OTHER`, which invites filtering on it. That does not
work, because the Bedrock Converse format requires a `toolResult` to travel inside
a **USER-role** message and a `toolUse` inside an **ASSISTANT-role** one. Measured
on one real 62-event session:

    ASSISTANT  text          11     <- real assistant replies
    ASSISTANT  toolUse       23     <- tool calls
    USER       text           6     <- real user prompts
    USER       toolResult    13     <- tool output, recorded as the USER speaking
    None       (blob)        23     <- Strands agent state, not conversation

Filtering `role in (USER, ASSISTANT)` yields 53 items of which 36 are plumbing,
and the 13 toolResults render as the user pasting walls of JSON. `TOOL` was never
used at all, so it cannot be relied on to exclude anything. The usable filter is
role **plus** content-block type.

**The payload is doubly wrapped.** An event's payload is either
`{"conversational": {...}}` or `{"blob": ...}`, and for conversational events the
`content.text` is itself a JSON-encoded Strands envelope:

    {"message": {"role": "user", "content": [{"text": "..."}]}}

so the readable text is two `json.loads` deep. Voice turns are the exception —
`voice_session.persist_voice_transcript` writes a bare string — so both shapes have
to be accepted or every voice turn vanishes from the transcript.

**One session is not enough.** That 62-event session held ~6 real turns. Asking for
20 means walking sessions newest-first until the count is met, which also backfills
the history that predates the stable-memory-session change (it lives under old
per-login session ids).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Content blocks that are conversation. Everything else on a conversational
# payload is tool plumbing and must not reach the user's screen.
_TEXT_BLOCK = "text"
# Roles worth rendering. `TOOL` and `OTHER` are in the API enum but Strands never
# writes them; they are excluded rather than mapped, so a future event carrying one
# is dropped loudly-in-logs rather than rendered as an unattributed bubble.
_RENDERABLE_ROLES = {"USER", "ASSISTANT"}


def _envelope_text(raw: str) -> str | None:
    """The human-readable text of one conversational payload, or None.

    Returns None for anything that is not a plain text message — tool calls, tool
    results, empty content. None means "not part of the transcript", which is
    different from an empty string and is why this does not just return "".
    """
    if not raw or not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        # A bare string: how voice turns are written. Not an error.
        return stripped
    try:
        outer = json.loads(stripped)
    except (ValueError, TypeError):
        # Text that merely begins with a brace. Render it rather than dropping a
        # real message on a parse technicality.
        return stripped
    message = outer.get("message") if isinstance(outer, dict) else None
    if not isinstance(message, dict):
        return stripped if not isinstance(outer, dict) else None
    parts = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        # A block with a `toolUse` or `toolResult` key is plumbing. Explicitly
        # checking for `text` rather than excluding known plumbing keys means a new
        # block type the SDK adds is dropped, not leaked.
        if _TEXT_BLOCK in block and isinstance(block[_TEXT_BLOCK], str):
            text = block[_TEXT_BLOCK].strip()
            if text:
                parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


def messages_from_events(events: list[dict]) -> list[dict]:
    """Renderable messages from one session's events, OLDEST first.

    `list_events` returns newest-first; the transcript reads oldest-first, so the
    caller gets it in display order rather than having to remember to reverse it.
    """
    out = []
    for event in events or []:
        ts = event.get("eventTimestamp")
        for payload in event.get("payload") or []:
            if not isinstance(payload, dict):
                continue
            conv = payload.get("conversational")
            if not isinstance(conv, dict):
                # `blob` payloads carry Strands agent state (the injected skills
                # XML, for one). Never conversation.
                continue
            role = (conv.get("role") or "").upper()
            if role not in _RENDERABLE_ROLES:
                continue
            text = _envelope_text((conv.get("content") or {}).get("text"))
            if text is None:
                continue
            out.append({
                "role": "assistant" if role == "ASSISTANT" else "user",
                "text": text,
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
            })
    # Oldest first. Sorting on the timestamp rather than reversing the list,
    # because a session's events can interleave when several are written in the
    # same second and "reverse the API order" then shuffles a turn pair.
    out.sort(key=lambda m: m["timestamp"])
    return out


def turns_from_messages(messages: list[dict]) -> list[dict]:
    """Collapse consecutive ASSISTANT messages so one reply is one bubble.

    A single assistant reply can be split across events — one per streamed chunk
    that got persisted separately — and rendering those as separate bubbles makes
    one answer look like five.

    Deliberately NOT applied to user messages. There is no mechanism that splits a
    user prompt across events, so a run of them means the user really did send
    several before getting a reply (asked, waited, asked again). Joining those would
    rewrite what happened into a single message they never sent.
    """
    turns: list[dict] = []
    for msg in messages:
        if turns and msg["role"] == "assistant" and turns[-1]["role"] == "assistant":
            turns[-1]["text"] = f"{turns[-1]['text']}\n{msg['text']}"
            turns[-1]["timestamp"] = msg["timestamp"]
            continue
        turns.append(dict(msg))
    return turns


def recent_turns(client, memory_id: str, actor_id: str, limit: int,
                 session_ids: list[str]) -> tuple[list[dict], list[str]]:
    """The last `limit` renderable turns across `session_ids`, oldest first.

    `session_ids` must be newest-session-first. Walks them in order, accumulating
    until `limit` turns are held, then stops — so a user with 56 historical
    sessions costs two or three ListEvents calls rather than 56.

    Returns (turns, sessions_read) so the caller can report how far back it had to
    reach. A transcript assembled from four logins is a different thing from one
    continuous conversation, and the UI says which.
    """
    collected: list[dict] = []
    read: list[str] = []
    for session_id in session_ids:
        if len(collected) >= limit:
            break
        try:
            events = _all_events(client, memory_id, actor_id, session_id)
        except Exception as exc:  # noqa: BLE001
            # One unreadable session must not empty the transcript: the newest
            # session is the one most likely to be mid-write.
            logger.warning("could not read events for session %s: %s",
                           session_id, exc)
            continue
        turns = turns_from_messages(messages_from_events(events))
        if not turns:
            continue
        read.append(session_id)
        # Prepend: we are walking backwards through time, and the result is
        # oldest-first.
        collected = turns + collected
    return collected[-limit:], read


def _all_events(client, memory_id: str, actor_id: str, session_id: str) -> list[dict]:
    """Every event in one session, paginated.

    Bounded at 10 pages. A session that has grown past that is one where the tail
    is what matters anyway, and an unbounded loop here would be a Lambda timeout
    rather than a slow response.
    """
    events: list[dict] = []
    token = None
    for _ in range(10):
        kwargs = {"memoryId": memory_id, "actorId": actor_id,
                  "sessionId": session_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_events(**kwargs)
        events.extend(resp.get("events") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return events


def sessions_newest_first(client, memory_id: str, actor_id: str,
                          max_sessions: int = 200) -> list[str]:
    """This actor's session ids, newest first.

    `list_sessions` does not promise an order — measured, it does not return them
    sorted — so this sorts explicitly. Taking the API's order on trust would show
    a user a transcript from three weeks ago as their "recent" conversation.
    """
    summaries: list[dict] = []
    token = None
    while len(summaries) < max_sessions:
        kwargs = {"memoryId": memory_id, "actorId": actor_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_sessions(**kwargs)
        summaries.extend(resp.get("sessionSummaries") or [])
        token = resp.get("nextToken")
        if not token:
            break
    summaries.sort(key=lambda s: str(s.get("createdAt") or ""), reverse=True)
    return [s["sessionId"] for s in summaries if s.get("sessionId")]
