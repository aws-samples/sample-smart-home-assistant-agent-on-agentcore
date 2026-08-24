"""The Cognito group naming that authorizes A2A sub-agent access.

COPY of shared/a2a_groups.py, because the admin Lambda is packaged from its own directory.
shared/tests/test_a2a_groups_parity.py holds the copies byte-identical
below this header.

A grant is a Cognito group. `cognito:groups` is a signed, string-array claim on the
end user's token, and each sub-agent Runtime's `customJWTAuthorizer.customClaims`
matches it with `CONTAINS_ANY` over that agent's group names. So authorization is
checked by AgentCore before any of our code runs, on a claim the client cannot
forge or widen.

That is the whole reason this convention has to be exact and shared. Five separate
deployment units derive these strings independently:

  - `a2a-agent-registry/deploy.py`      writes the authorizer's match list
  - `a2a-agent-registry/common/server.py`  derives the caller's skill set
  - `cdk/lambda/admin-api`             materialises DDB intent into memberships
  - `cdk/lambda/pre-token`             injects GLOBAL grants into the claim itself,
                                       with no membership behind them
  - `agent/`                           decides which `a2a_*` tools to register

The trigger is worth calling out: it makes a group name that appears in the claim
and in NO membership a normal, correct state. `AdminListGroupsForUser` is therefore
no longer the whole answer to "what may this user reach" — see
`shared/subagent_policy.py`.

A disagreement between any two of them does not raise. It either silently denies a
granted user (group written one way, matched another) or silently offers the model a
tool the platform will refuse. Hence one definition here and a parity test over the
copies, the same arrangement as `agent_registry.REGISTRY_CLIENT`.

Two shapes, because the two enforcement points ask different questions
----------------------------------------------------------------------
    a2a-<agent>            "may this caller reach this agent at all"  -> the DOOR
    a2a-<agent>.<skill>    "which of its skills were granted"         -> the CONTAINER

A granted user holds BOTH: the agent group once, and one skill group per granted
skill. The two are not redundant, and which one each enforcement point reads is the
whole reason this file changed on 2026-08-15.

The Runtime authorizer used to be handed the full per-skill list
(`CONTAINS_ANY [a2a-X.s1, a2a-X.s2, ...]`). Measured against what it actually
decides: `CONTAINS_ANY` passes on ANY one of them, and the container then derives the
skill subset from the same signed claim and refuses only when that subset is empty
(`server.enforce_allowed_skills`). So the door and the container were asking the
identical question, and enumerating skills at the door bought **no** additional
authorization. What it did buy was a coupling: `CONTAINS_ANY` has no wildcard, so
adding a skill to a card meant an `UpdateAgentRuntime` before anyone granted the new
skill could get in — silently refused at the door until someone redeployed.

`authorizer_groups` therefore returns the ONE stable agent group. It is constant for
an agent's whole lifetime, so a card may grow skills with no redeploy and no
cross-team step, at identical door strength. Per-skill groups keep doing the job only
they can do: telling the container which skills a caller holds.

The migration has an order, and reversing it locks users out
-----------------------------------------------------------
The grant side must emit `a2a-<agent>` BEFORE any authorizer starts requiring it.
Flip the authorizer first and every already-granted user is refused until the next
materialisation reaches them. `check` in `shared/a2a_conformance.py` accepts a
skill-group-only authorizer as INFO rather than CLOSED for exactly this window: such
a runtime is not broken, it is merely still coupled.
"""

from __future__ import annotations

import re

# Prefix so these are recognisable next to `admin` and any future application
# group, and so a reconcile can tell which memberships it owns. A group NOT
# carrying this prefix is never touched by the materialiser.
GROUP_PREFIX = "a2a-"

# Cognito allows letters, marks, symbols, numbers and punctuation, up to 128 chars.
# `.` separates agent from skill because it is punctuation (allowed) and appears in
# neither an AgentCard name nor a skill id, so the split is unambiguous.
SEPARATOR = "."

# What we are willing to put in a group name. Deliberately narrower than what
# Cognito accepts: an AgentCard name or skill id outside this set means something
# upstream changed shape, and guessing an encoding for it would produce a group
# that one of the four callers derives differently.
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

MAX_GROUP_NAME = 128


class GroupNameError(ValueError):
    """Raised when an agent or skill cannot be encoded as a group name."""


def group_name(agent_name: str, skill_id: str) -> str:
    """The group granting `skill_id` on `agent_name`.

    >>> group_name("knowledge-qa-agent", "answer_from_docs")
    'a2a-knowledge-qa-agent.answer_from_docs'

    Raises rather than sanitising. A sanitised name is a name some other caller
    will sanitise differently, and the failure is silent in both directions.
    """
    agent, skill = (agent_name or "").strip(), (skill_id or "").strip()
    if not _SAFE.match(agent):
        raise GroupNameError(f"agent name {agent_name!r} is not group-name safe")
    if not _SAFE.match(skill):
        raise GroupNameError(f"skill id {skill_id!r} is not group-name safe")
    name = f"{GROUP_PREFIX}{agent}{SEPARATOR}{skill}"
    if len(name) > MAX_GROUP_NAME:
        raise GroupNameError(
            f"group name {name!r} exceeds Cognito's {MAX_GROUP_NAME} characters")
    return name


def agent_group_name(agent_name: str) -> str:
    """The group granting access to `agent_name` at all, with no skill component.

    >>> agent_group_name("knowledge-qa-agent")
    'a2a-knowledge-qa-agent'

    This is what a sub-agent's Runtime authorizer matches. Stable for the agent's
    whole lifetime: adding, renaming or removing a skill does not change it, which
    is what removes the "add a skill -> UpdateAgentRuntime" coupling. Renaming the
    CARD does change it, and that is correct — a renamed card is a different agent
    to every one of the five deployment units in the module docstring.
    """
    agent = (agent_name or "").strip()
    if not _SAFE.match(agent):
        raise GroupNameError(f"agent name {agent_name!r} is not group-name safe")
    name = f"{GROUP_PREFIX}{agent}"
    if len(name) > MAX_GROUP_NAME:
        raise GroupNameError(
            f"group name {name!r} exceeds Cognito's {MAX_GROUP_NAME} characters")
    return name


def parse_group(name: str) -> tuple[str, str] | None:
    """`(agent, skill)` for one of our SKILL groups, or None for anything else.

    None for `admin`, for the agent-level `a2a-<agent>` (no separator), and for an
    empty half — a malformed group must not decode to a grant on `""`, which
    would match nothing and read as a real entry in the UI.

    Deliberately still skill-only. `grants_from_claim` is built on it and answers
    "which skills", so an agent-level group must contribute no skills: counting it
    as one would invent a skill named "" and offer the orchestrator a tool for it.
    Use `agent_of_group` when the question is "which agent does this group belong
    to", which is what a revocation sweep asks.
    """
    if not name or not name.startswith(GROUP_PREFIX):
        return None
    body = name[len(GROUP_PREFIX):]
    agent, sep, skill = body.partition(SEPARATOR)
    if not sep or not agent or not skill:
        return None
    return agent, skill


def agent_of_group(name: str) -> str | None:
    """The agent a group of EITHER shape belongs to, or None if it is not ours.

    >>> agent_of_group("a2a-light-effect-agent")
    'light-effect-agent'
    >>> agent_of_group("a2a-light-effect-agent.compose_effect")
    'light-effect-agent'
    >>> agent_of_group("admin") is None
    True

    The revocation sweep scans from the group side and needs this rather than
    `parse_group`: the agent-level group is the one that actually opens the door, so
    a sweep that could not decode it would leave the door open on a deprecated
    record while dutifully removing the skill groups that gate nothing.
    """
    if not name or not name.startswith(GROUP_PREFIX):
        return None
    agent = name[len(GROUP_PREFIX):].partition(SEPARATOR)[0]
    return agent or None


def grants_from_claim(groups: list[str] | tuple[str, ...] | None) -> dict[str, list[str]]:
    """`{agent_name: [skill_id, ...]}` from a `cognito:groups` claim.

    Groups that are not ours are ignored rather than rejected: the same claim
    carries `admin`, and a pool is free to hold groups this system knows nothing
    about.
    """
    out: dict[str, set[str]] = {}
    for raw in groups or ():
        parsed = parse_group(str(raw))
        if parsed is None:
            continue
        agent, skill = parsed
        out.setdefault(agent, set()).add(skill)
    return {agent: sorted(skills) for agent, skills in sorted(out.items())}


def skills_for_agent(groups: list[str] | tuple[str, ...] | None,
                     agent_name: str) -> frozenset[str]:
    """The skills the claim grants on one agent. Empty means no access at all."""
    return frozenset(grants_from_claim(groups).get(agent_name, ()))


def skill_groups_for_agent(agent_name: str, skill_ids) -> list[str]:
    """One group per skill — what an admin hands out, and what the container reads."""
    return [group_name(agent_name, s) for s in sorted(set(skill_ids))]


def authorizer_groups(agent_name: str, skill_ids=()) -> list[str]:
    """The CONTAINS_ANY list a sub-agent's Runtime authorizer should be given.

    One entry: the agent-level group. `skill_ids` is accepted and ignored so the
    signature still reads like a question about this card, and so a caller that
    still passes them is not silently doing something different from what it looks
    like — see `agent_group_name` for why enumerating them bought nothing.

    Renamed from `all_groups_for_agent` on purpose. The old name is gone rather than
    aliased: five deployment units carry a COPY of this module, and an alias would
    let a stale copy keep emitting the per-skill list while every reader assumed the
    stable one. An `AttributeError` on the first call is the failure we want.
    """
    return [agent_group_name(agent_name)]
