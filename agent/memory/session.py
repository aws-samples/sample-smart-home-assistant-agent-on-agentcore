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
    from memory_actor import sanitize_actor_id as _sanitize_actor_id  # noqa: F401
except ImportError:  # pragma: no cover - pre-build / test path
    import sys
    from pathlib import Path

    _shared = Path(__file__).resolve().parent.parent.parent / "shared"
    if _shared.is_dir() and str(_shared) not in sys.path:
        sys.path.insert(0, str(_shared))
    from memory_actor import sanitize_actor_id as _sanitize_actor_id  # noqa: F401


def get_memory_session_manager(
    session_id: str, actor_id: str
) -> Optional[AgentCoreMemorySessionManager]:
    """Create AgentCore Memory session manager for conversation persistence."""
    if not MEMORY_ID:
        return None
    try:
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
