"""The Cognito group naming that authorizes A2A sub-agent access.

COPY of shared/a2a_groups.py. Each sub-agent is packaged from
`a2a-agent-registry/` alone and cannot import `shared/`, the same reason
`REGISTRY_CLIENT` is duplicated in agent/tools/a2a.py.
shared/tests/test_a2a_groups_parity.py holds the copies byte-identical
below this header.

A grant is a Cognito group. `cognito:groups` is a signed, string-array claim on the
end user's token, and each sub-agent Runtime's `customJWTAuthorizer.customClaims`
matches it with `CONTAINS_ANY` over that agent's group names. So authorization is
checked by AgentCore before any of our code runs, on a claim the client cannot
forge or widen.

That is the whole reason this convention has to be exact and shared. Four separate
deployment units derive these strings independently:

  - `a2a-agent-registry/deploy.py`      writes the authorizer's match list
  - `a2a-agent-registry/common/server.py`  derives the caller's skill set
  - `cdk/lambda/admin-api`             materialises DDB intent into memberships
  - `agent/`                           decides which `a2a_*` tools to register

A disagreement between any two of them does not raise. It either silently denies a
granted user (group written one way, matched another) or silently offers the model a
tool the platform will refuse. Hence one definition here and a parity test over the
copies, the same arrangement as `agent_registry.REGISTRY_CLIENT`.

Group names encode `(agent, skill)` rather than just the agent, so the same claim
answers both questions the two enforcement points ask: the Runtime authorizer asks
"any grant on me at all" (CONTAINS_ANY over all of this agent's skill groups) and
the server asks "which skills" (the subset present in the claim).
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


def parse_group(name: str) -> tuple[str, str] | None:
    """`(agent, skill)` for one of our groups, or None for anything else.

    None for `admin`, for a group with our prefix but no separator, and for an
    empty half — a malformed group must not decode to a grant on `""`, which
    would match nothing and read as a real entry in the UI.
    """
    if not name or not name.startswith(GROUP_PREFIX):
        return None
    body = name[len(GROUP_PREFIX):]
    agent, sep, skill = body.partition(SEPARATOR)
    if not sep or not agent or not skill:
        return None
    return agent, skill


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


def all_groups_for_agent(agent_name: str, skill_ids) -> list[str]:
    """Every group name for one agent, for the authorizer's CONTAINS_ANY list.

    `CONTAINS_ANY` takes an exact list with no wildcard, so the authorizer has to
    enumerate the agent's skills. That is why adding a skill is an
    `UpdateAgentRuntime` and not just a group write.
    """
    return [group_name(agent_name, s) for s in sorted(set(skill_ids))]
