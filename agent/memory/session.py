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
        retrieval_config = {
            f"/users/{actor_id}/facts": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/summaries/{actor_id}/{session_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/users/{actor_id}/preferences": RetrievalConfig(top_k=3, relevance_score=0.5),
        }
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
