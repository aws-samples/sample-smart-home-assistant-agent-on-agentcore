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


def get_memory_session_manager(
    session_id: str, actor_id: str
) -> Optional[AgentCoreMemorySessionManager]:
    """Create AgentCore Memory session manager for conversation persistence."""
    if not MEMORY_ID:
        return None
    try:
        retrieval_config = {
            f"/facts/{actor_id}/": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/summaries/{actor_id}/{session_id}/": RetrievalConfig(top_k=3, relevance_score=0.5),
            f"/preferences/{actor_id}/": RetrievalConfig(top_k=3, relevance_score=0.5),
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
