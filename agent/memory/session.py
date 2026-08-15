import os
import logging
from typing import Optional

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

logger = logging.getLogger(__name__)

# agentcore CLI sets MEMORY_<NAME>_ID automatically on deploy
MEMORY_ID = os.getenv("MEMORY_SMARTHOMEMEMORY_ID", "")
REGION = os.getenv("AWS_REGION", "us-west-2")
# The EPISODIC strategy's id, needed because episodes are stored under
# `/strategy/{memoryStrategyId}/...` rather than a path we can build from the
# actor alone. Minted when the strategy is created, so it is patched in
# post-deploy by scripts/setup-agentcore.py like MEMORY_ID itself.
EPISODIC_STRATEGY_ID = os.getenv("MEMORY_STRATEGY_EPISODIC_ID", "")


# The actor id rule lives in shared/memory_actor.py because the A2A sub-agents
# read the same Memory namespaces and have to compute the identical id — see that
# module for why a divergence is silent rather than loud. scripts/01-install-deps.sh
# copies it to agent/memory_actor.py for this container (gitignored build output).
#
# The fallback is a real one, not defensive noise: this module is imported by unit
# tests that run from the repo without the build step having happened.
try:
    from memory_actor import memory_session_id as _memory_session_id  # noqa: F401
    from memory_actor import sanitize_actor_id as _sanitize_actor_id  # noqa: F401
except ImportError:  # pragma: no cover - pre-build / test path
    import sys
    from pathlib import Path

    _shared = Path(__file__).resolve().parent.parent.parent / "shared"
    if _shared.is_dir() and str(_shared) not in sys.path:
        sys.path.insert(0, str(_shared))
    from memory_actor import memory_session_id as _memory_session_id  # noqa: F401
    from memory_actor import sanitize_actor_id as _sanitize_actor_id  # noqa: F401


def memory_session_id(actor_id: str) -> str:
    """The Memory session id for one user's conversation — STABLE across logins.

    Deliberately NOT the runtime session id. That one is
    `user-session-{sub}-{epoch_ms}`, minted fresh on every login because the
    Sessions tab and the dashboard's per-runtime attribution are built on "one
    login, one session". AgentCore Memory's SHORT-TERM memory, though, is scoped
    to `(memoryId, actorId, sessionId)` — so keying it on the per-login id means
    every login starts with an empty transcript, and the model cannot see what
    the user said ten minutes ago before they refreshed the page.

    Splitting the two ids is what lets both hold: per-login tracking stays
    per-login, and the conversation is one continuous thread per user.

    Derived from the actor rather than passed in, for two reasons. It cannot be
    forged into another user's thread by a client sending someone else's session
    id. And it is computed identically by the three places that write events —
    the Strands session manager (text turns), `agent.py:_persist_vision_turn`
    (the vision bypass writes events directly) and
    `voice_session.py:persist_voice_transcript`. Those three splitting apart is
    silent: each half of the conversation lands in a different session and the
    transcript simply has holes, which is the failure the voice writer's own
    docstring exists to prevent ("so the follow-up text chat sees the user's
    voice history as prior turns").

    One consequence worth stating: the SUMMARIZATION namespace is
    `/summaries/{actor}/{session_id}`, so it becomes one running summary per user
    instead of one per login. That is the intended trade — a per-login summary of
    a conversation that spans logins was summarising an arbitrary slice.

    The rule itself moved to shared/memory_actor.py, next to the actor rule, once a
    FOURTH reader appeared: the A2A sub-agents retrieve that summary namespace, so
    they need this exact string and are packaged from a different directory. This
    function stays as the orchestrator's entry point — three writers call it — and
    delegates.
    """
    return _memory_session_id(actor_id)


def get_memory_session_manager(
    session_id: str, actor_id: str
) -> Optional[AgentCoreMemorySessionManager]:
    """Create AgentCore Memory session manager for conversation persistence.

    `session_id` is accepted and IGNORED for the memory scope: the memory session
    is derived from the actor (see `memory_session_id`) so short-term memory
    survives a re-login. The parameter is kept so callers can keep passing the
    runtime session id, which is still what identifies the request everywhere
    else, and so a caller reading this line is told the two are different rather
    than assuming they are the same.
    """
    if not MEMORY_ID:
        return None
    try:
        session_id = memory_session_id(actor_id)
        actor_id = _sanitize_actor_id(actor_id)
        # One entry per built-in Memory strategy. All four are enabled on the memory
        # resource (SEMANTIC / SUMMARIZATION / USER_PREFERENCE / EPISODIC), and each
        # needs a line here: a strategy whose namespace nothing retrieves from still
        # extracts and stores records, costs money, and contributes nothing to an
        # answer. Nothing errors — the namespace is simply never queried.
        # `agent/tests/test_memory_namespaces.py` pins these against what the setup
        # script provisions.
        #
        # EPISODIC is the ordered account ("turned the fan on, then said it got
        # cold"), which SUMMARIZATION's prose and SEMANTIC's standalone facts both
        # drop. Two things about it are unlike the others and both are documented
        # AWS behaviour, not local quirks:
        #
        #   - Its namespace is STRATEGY-scoped, not user-scoped. Per the docs,
        #     episodes live under `/strategy/{memoryStrategyId}/...`; the actor-level
        #     form is used here so one user's episodes stay separate. A
        #     `/users/{actor}/episodes` namespace is *accepted* by the API and then
        #     never populated, which is the silent version of this mistake.
        #   - Records appear only once AgentCore judges an episode COMPLETE — "if an
        #     episode is not complete, it will take longer to generate because the
        #     system waits to see if the conversation is continued." So unlike
        #     semantic/preference (which landed ~50s after the same events in a
        #     measured probe), an empty episodic namespace mid-conversation is
        #     expected and is not evidence of a misconfiguration.
        #
        # MEMORY_STRATEGY_EPISODIC_ID is patched in per deployment because the id is
        # minted with the strategy. Without it the entry is omitted rather than
        # guessed: a wrong namespace retrieves nothing forever and looks identical
        # to a strategy that has not produced records yet.
        retrieval_config = {
            f"/users/{actor_id}/facts": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/summaries/{actor_id}/{session_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/users/{actor_id}/preferences": RetrievalConfig(top_k=3, relevance_score=0.5),
        }
        if EPISODIC_STRATEGY_ID:
            retrieval_config[
                f"/strategy/{EPISODIC_STRATEGY_ID}/actor/{actor_id}/"
            ] = RetrievalConfig(top_k=3, relevance_score=0.5)
        else:
            logger.info(
                "MEMORY_STRATEGY_EPISODIC_ID unset; episodic memory will not be "
                "retrieved. Run scripts/setup-agentcore.py to patch it in."
            )
        return AgentCoreMemorySessionManager(
            AgentCoreMemoryConfig(
                memory_id=MEMORY_ID,
                session_id=session_id,
                actor_id=actor_id,
                retrieval_config=retrieval_config,
            ),
            REGION,
        )
    except Exception as e:
        logger.warning(f"Failed to create memory session manager: {e}")
        return None
