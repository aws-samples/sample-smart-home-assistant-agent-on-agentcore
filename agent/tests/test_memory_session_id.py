"""The Memory session id is per USER, not per login — and three writers must agree.

AgentCore Memory's short-term memory is scoped to `(memoryId, actorId, sessionId)`.
The runtime session id is `user-session-{sub}-{epoch_ms}`, minted fresh on every
login because the Sessions tab and the dashboard's per-runtime attribution are
built on "one login, one session". Keying memory on that meant every login began
with an empty transcript: the user refreshed the page and the agent had forgotten
the last ten minutes, while the Memories page still showed the events under an old
session nobody would ever read again.

So the two ids are split. What this file protects is the part that fails silently:
**three different code paths write conversational events**, and if they compute the
session id differently each half of the conversation lands in a different session.
Nothing errors. The transcript just has holes, in the exact places where the user
switched modality — which is the failure `persist_voice_transcript`'s own docstring
was written to prevent.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

from memory.session import memory_session_id  # noqa: E402


def test_it_is_derived_from_the_actor_not_the_login():
    """Two logins by the same user must land in the same memory session."""
    assert memory_session_id("alice@example.com") == memory_session_id("alice@example.com")


def test_two_users_never_share_a_session():
    assert memory_session_id("alice@example.com") != memory_session_id("bob@example.com")


def test_it_is_not_a_runtime_session_id():
    """A runtime session id carries an epoch suffix; this must not look like one,
    or a reader cannot tell which of the two they are holding."""
    sid = memory_session_id("alice@example.com")
    assert not sid.startswith("user-session-")
    assert not re.search(r"-\d{13}$", sid), sid


def test_it_reuses_the_actor_sanitisation():
    """`@` and `.` are not accepted in an actor id, and the session id embeds it.
    Sanitising differently here would produce a session id that does not match the
    actor's own namespace, which reads as a user with no history."""
    sid = memory_session_id("alice.smith@example.com")
    assert "@" not in sid and "." not in sid
    assert "alice_smith_example_com" in sid


def test_the_session_manager_ignores_the_runtime_session_it_is_given():
    """`get_memory_session_manager` still takes the runtime session id, because
    every caller has one to hand — but it must not use it for the memory scope."""
    import inspect

    from memory import session as mod

    src = inspect.getsource(mod.get_memory_session_manager)
    assert "session_id = memory_session_id(actor_id)" in src, (
        "the session manager must override the passed runtime session id; "
        "keying memory on it empties short-term memory on every login")


# --- the three writers have to agree -------------------------------------

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_every_conversational_event_writer_uses_the_derived_id():
    """A grep-level check, deliberately.

    The three writers are in three modules and two of them call `create_event`
    directly, so there is no single function to unit-test. What matters is that
    none of them passes a runtime session id into a memory write — and that is a
    property of the call sites, not of any one function's behaviour.
    """
    agent_py = _read(os.path.join(AGENT_DIR, "agent.py"))
    voice_py = _read(os.path.join(AGENT_DIR, "voice_session.py"))

    # The vision bypass writes events directly.
    assert "session_id=memory_session_id(actor_id)" in agent_py, (
        "agent.py:_persist_vision_turn must write under the derived memory "
        "session; using the runtime session splits vision turns out of the "
        "transcript")

    # Voice writes finalized Nova Sonic turns directly.
    assert "sessionId=_memory_session_id(actor_id)" in voice_py, (
        "voice_session.py:persist_voice_transcript must write under the derived "
        "memory session, or 'the follow-up text chat sees the user's voice "
        "history' only holds within a single login")

    # And neither may still be passing a runtime session into a memory write.
    for name, src in (("agent.py", agent_py), ("voice_session.py", voice_py)):
        for bad in ("session_id=session_id,\n            messages=",
                    "sessionId=session_id,"):
            assert bad not in src, f"{name} still writes memory under the runtime session"
