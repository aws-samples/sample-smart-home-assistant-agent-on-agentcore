"""Per-user skill policy: the one subtraction in an otherwise additive model.

`load_skills_from_dynamodb` builds a user's skill set as global UNION per-user
(same name replaces). That model had no way to take a GLOBAL skill away from one
user, and two global skills are not just prompt text: `browser-use` registers
`browse_web` and `code-interpreter` registers `execute_python`, so every user held
a live browser and a Python sandbox with no admin lever at all.

Two pieces, both pure enough to test without strands:

  - `read_disabled` / `apply_disabled`: the per-user `__skill_policy__` row names
    skills this user does NOT get, applied AFTER the merge. Only global skills are
    worth listing (a per-user skill can simply be deleted), but any name is honoured.
  - `builtin_wanted`: `http_request` and `file_write` used to be registered for
    everyone unconditionally. They are registered only when an effective skill
    DECLARES them in `allowedTools` — which the shipped skills already do
    (`weather-lookup` → http_request, `user-feedback` → file_write), so nothing a
    user has today disappears, but disabling the skill now removes the tool too.

Kept out of `agent.py` for the same reason `a2a_grants.py` is: a unit test of a set
difference should not import playwright.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SKILL_POLICY_SK = "__skill_policy__"

# Actors that are not a user. `default` is the AgentCore warm-up caller; the other
# two are storage scopes, not people.
_NOT_A_USER = ("", "default", "__global__")


def read_disabled(table, actor_id: str) -> set[str]:
    """Skill names this user is denied. Empty for no row or no real user."""
    if not actor_id or actor_id in _NOT_A_USER:
        return set()
    item = table.get_item(
        Key={"userId": actor_id, "skillName": SKILL_POLICY_SK}).get("Item") or {}
    raw = item.get("disabledSkills") or []
    if isinstance(raw, (set, list, tuple)):
        return {str(s) for s in raw if str(s).strip()}
    return set()


def apply_disabled(skills: list, disabled: set[str]) -> list:
    """`skills` minus the disabled names, order preserved."""
    if not disabled:
        return skills
    kept = [s for s in skills if getattr(s, "name", "") not in disabled]
    dropped = len(skills) - len(kept)
    if dropped:
        logger.info("skill policy removed %d skill(s): %s", dropped,
                    sorted(disabled & {getattr(s, "name", "") for s in skills}))
    return kept


def builtin_wanted(skills: list | None, tool_name: str) -> bool:
    """Whether a built-in tool should be registered for this skill set.

    `None` means the skill set is UNKNOWN (DynamoDB failed and the filesystem
    fallback is in use); that path keeps today's behaviour rather than turning an
    outage into a second, quieter outage.
    """
    if skills is None:
        return True
    for skill in skills:
        declared = getattr(skill, "allowed_tools", None) or []
        if tool_name in declared:
            return True
    return False
