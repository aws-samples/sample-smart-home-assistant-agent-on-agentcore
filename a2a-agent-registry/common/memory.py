"""Where a sub-agent's Memory identity comes from.

Thin on purpose. The actor-id RULE lives in `shared/memory_actor.py`, which the
orchestrator uses too; this module only knows how to find that module from inside
a rendered sub-agent container and how to read a CallerIdentity.

`deploy.py` copies the repo's `shared/` next to the agent code, so the import
works at runtime; the sys.path insert covers running the tests straight out of the
repo. Both paths reach the same file, which is the point — a second copy of the
sanitizing regex would be a second chance for the sub-agent's namespace to drift
from the orchestrator's, and that drift is silent.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _memory_actor_module():
    try:
        import memory_actor  # type: ignore

        return memory_actor
    except ImportError:
        pass
    for candidate in (
        # Rendered container: shared/ sits next to common/.
        Path(__file__).resolve().parent.parent / "shared",
        # Repo checkout: a2a-agent-registry/common/ -> <root>/shared.
        Path(__file__).resolve().parent.parent.parent / "shared",
    ):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    import memory_actor  # type: ignore

    return memory_actor


def memory_actor_for(caller) -> str:
    """The Memory actor id for `caller`, or "" when one cannot be formed.

    Never raises. A sub-agent with no memory still answers the request — memory is
    an enhancement here, and failing the whole delegation because a namespace could
    not be computed would trade a better answer for no answer.
    """
    try:
        return _memory_actor_module().memory_actor_id(
            email=getattr(caller, "email", ""), sub=getattr(caller, "sub", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not derive the memory actor id: %s", exc)
        return ""
