"""Read an admin-governed system prompt override for this sub-agent.

Until now a sub-agent's prompt was whatever `system_prompt.md` said in the image
it was built from, so changing one meant a redeploy. The orchestrator's prompt has
been editable from the Admin Console since §8.10; the specialists were the part of
the fleet that governance did not reach, which is backwards — a specialist is
exactly what an operator wants to retune without shipping a container.

Resolution mirrors the orchestrator's `load_system_prompt`, with one deliberate
difference:

    global override        -> replaces the shipped prompt entirely
    per-user override      -> appended to whatever the global step produced
    neither                -> the shipped `system_prompt.md`

The orchestrator concatenates its global override onto nothing (its built-in
constant is used only when both scopes are empty), and this matches that: an admin
who writes a global prompt for a specialist has replaced it, not annotated it.

Failure is soft, in one direction only. If DynamoDB is unreachable the shipped
prompt is used and the request proceeds, because the alternative — refusing to
answer because a governance table was throttled — degrades an agent that has a
perfectly good prompt compiled into it. The inverse (silently ignoring an override
that IS readable) would be a governance hole, so a read that succeeds is always
honoured.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Set on the runtime by deploy.py; absent means prompt governance is not wired up
# for this agent and the shipped prompt is authoritative.
TABLE_ENV = "SKILLS_TABLE_NAME"

# The sort key the Admin Console writes. Keyed by AgentCard name — the one
# identifier the console, the Registry record and this process each derive
# independently. `agent/agent.py` uses the same `__prompt_<type>__` shape with
# "text"/"voice" in the slot.
GLOBAL_SCOPE = "__global__"

_table: Any = None


def _get_table():
    """The skills table, created once per container."""
    global _table
    if _table is None:
        import boto3

        name = os.environ.get(TABLE_ENV, "")
        if not name:
            return None
        _table = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"),
        ).Table(name)
    return _table


def prompt_sort_key(agent_name: str) -> str:
    return f"__prompt_{agent_name}__"


def _read_body(table, user_id: str, sk: str) -> str:
    resp = table.get_item(Key={"userId": user_id, "skillName": sk})
    item = resp.get("Item") or {}
    body = item.get("promptBody")
    return body.strip() if isinstance(body, str) else ""


def resolve_system_prompt(agent_name: str, shipped: str,
                          user_id: str | None = None) -> str:
    """The prompt this request should run with.

    `agent_name` is the AgentCard name. `shipped` is the contents of
    `system_prompt.md`. `user_id` is the verified caller's Cognito sub, or None
    for an unauthenticated (prompt-only) agent, in which case only the global
    override applies.

    Never raises and never returns empty: the caller always gets a usable prompt.
    """
    table = _get_table()
    if table is None:
        return shipped

    sk = prompt_sort_key(agent_name)
    try:
        global_body = _read_body(table, GLOBAL_SCOPE, sk)
        user_body = _read_body(table, user_id, sk) if user_id else ""
    except Exception as exc:  # noqa: BLE001
        # Logged at warning, not error: the request is about to succeed with the
        # shipped prompt, and paging on this would be noise. It IS logged, because
        # "my override isn't taking effect" is otherwise unanswerable.
        logger.warning("prompt override unreadable for %s: %s", agent_name, exc)
        return shipped

    base = global_body or shipped
    if user_body:
        return f"{base}\n\n{user_body}"
    return base
