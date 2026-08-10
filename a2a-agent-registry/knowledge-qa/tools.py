"""Gateway tools for the knowledge-QA agent, built per request.

One tool, but with a wrinkle that matters: the knowledge base scopes retrieval by
the user's EMAIL, not by their Cognito `sub`. Device state partitions on `sub`;
the KB's metadata filter matches `scope` against `__shared__` plus the caller's
email. Passing the sub here is not a permissions hole — the filter simply matches
nothing private, so the user silently loses their own documents and sees only the
shared ones. That is the kind of failure nobody reports as a bug.

`common.server` verifies both values off the same idToken, so `caller.email` is as
trustworthy as `caller.sub`; it just answers a different question.
"""

from __future__ import annotations

import logging

from common.gateway_tools import QUERY_KB, GatewaySession

logger = logging.getLogger(__name__)

WANTED = (QUERY_KB,)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`."""
    try:
        session = GatewaySession(caller, WANTED)
    except RuntimeError as exc:
        logger.error("could not open a gateway session: %s", exc)
        return []

    if not session.has(QUERY_KB):
        logger.warning(
            "sub=%s... is not permitted query_knowledge_base — Cedar policy or "
            "the user's tool permissions", caller.sub[:8])
        return []

    if not caller.email:
        # Worth logging loudly: retrieval still works, but only over shared docs,
        # and the user's own uploads become invisible with no error anywhere.
        logger.warning(
            "no email on the verified token for sub=%s... — knowledge base "
            "retrieval will cover shared documents only", caller.sub[:8])

    from strands import tool as strands_tool

    @strands_tool
    def query_knowledge_base(query: str) -> str:
        """Search the enterprise knowledge base and return the matching passages
        with their source documents.

        Pass the user's question largely as they asked it, plus the obvious
        keywords — this is a vector search, so a phrase that reads like the target
        document retrieves better than a single word. Search again with different
        wording before concluding something is absent."""
        # `user_id` here is the EMAIL, not the sub — see the module docstring.
        return session.call(QUERY_KB, {"query": query},
                            user_id=caller.email or caller.sub)

    return [query_knowledge_base]
