"""The A2A agent roster — single source of truth.

These three tables used to be copy-pasted into deploy.py, teardown.py,
demo_reset.py and smoke_test.py. Four copies were survivable at three agents;
the spec adds five more, and a roster edit that misses one copy fails at a
different stage each time (deploy renders it, teardown leaves it running,
demo_reset can't find it, smoke_test skips it silently).

Adding an agent means adding one entry to AGENTS below. The derived maps and the
name/slug constraints follow from it.

Slug constraints, measured rather than assumed:
  - `agentcore create --name` accepts `[A-Za-z][A-Za-z0-9]{0,22}` — starts with a
    letter, alphanumeric only, at most 23 characters. Hence no hyphens.
  - The CLI names the runtime `{project}_{runtime}-{10 random}` and the service
    caps `agentRuntimeName` at 48, so `len(slug) * 2 + 11 <= 48`, i.e.
    `len(slug) <= 18`. `sha2amaintenance` (16) is the longest in use.
`validate_roster()` enforces both and is called at import.
"""

from __future__ import annotations

# dir name -> (registry/card name, agentcore project slug)
#
# The directory name is what a2a-agent-registry/<dir>/ is called and what
# `--agent` takes on the command line. The long name is the AgentCard `name` and
# the Registry record name. The slug names the agentcore project, the CFN stack
# and the runtime.
AGENTS: dict[str, tuple[str, str]] = {
    "energy-optimization": ("energy-optimization-agent", "sha2aenergy"),
    "home-security": ("home-security-agent", "sha2asecurity"),
    "appliance-maintenance": ("appliance-maintenance-agent", "sha2amaintenance"),
    # The first agent with tools: it ships a tools.py, so deploy.py generates a
    # main.py that passes the factory and the server requires a verified user
    # identity on every request.
    "device-control": ("device-control-agent", "sha2adevice"),
}

AGENT_NAMES: tuple[str, ...] = tuple(AGENTS)
AGENT_LONG_NAMES: dict[str, str] = {k: v[0] for k, v in AGENTS.items()}
AGENT_SHORT_SLUG: dict[str, str] = {k: v[1] for k, v in AGENTS.items()}
LONG_NAME_TO_AGENT: dict[str, str] = {v: k for k, v in AGENT_LONG_NAMES.items()}

# Cognito resource server / scope identifiers for the m2m inbound auth. Also
# duplicated across the scripts before now.
RESOURCE_SERVER_ID = "a2a-server"
SCOPE_NAME = "invoke"
SCOPE_FULL = f"{RESOURCE_SERVER_ID}/{SCOPE_NAME}"
M2M_CLIENT_NAME = "smarthome-a2a-m2m"
SECRET_NAME = "smarthome/a2a/m2m-credentials"

# Header carrying the end user's idToken on an A2A hop. The `Authorization` slot
# is taken by the m2m token the Runtime's CUSTOM_JWT authorizer checks, so user
# identity needs its own header — the same split the main runtime uses with
# X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken.
USER_TOKEN_HEADER = "X-SuperApp-User-Token"
# Skills the caller says it was granted. Sent by the client and ENFORCED
# server-side; see common.server.
ALLOWED_SKILLS_HEADER = "X-A2A-Allowed-Skills"

MAX_SLUG_LEN = 18

# AWS Agent Registry GA namespace. Registry left the `bedrock-agentcore` namespace
# at GA (2026-08-06) and the old one stops serving it on 2026-09-17. Runtime,
# Gateway, Identity and workload identities did NOT move — a teardown that deletes
# a workload identity still needs the old client for that call while using this one
# for the record. Mirrors shared/agent_registry.REGISTRY_CLIENT; the deploy scripts
# run from this directory and do not import shared/.
REGISTRY_CLIENT = "agent-registry-control"


def validate_roster() -> None:
    """Fail at import if a roster entry would break at deploy time.

    Cheaper to catch here than after `agentcore create` has already run.
    """
    seen_slugs: dict[str, str] = {}
    seen_long: dict[str, str] = {}
    for agent, (long_name, slug) in AGENTS.items():
        if not slug or not slug[0].isalpha() or not slug.isalnum():
            raise ValueError(
                f"{agent}: slug {slug!r} must start with a letter and be "
                f"alphanumeric only (agentcore create rejects hyphens)"
            )
        if len(slug) > MAX_SLUG_LEN:
            raise ValueError(
                f"{agent}: slug {slug!r} is {len(slug)} chars; the runtime name "
                f"is <slug>_<slug>-<10 random> and the service caps that at 48, "
                f"so the limit is {MAX_SLUG_LEN}"
            )
        if slug in seen_slugs:
            raise ValueError(f"duplicate slug {slug!r}: {seen_slugs[slug]} and {agent}")
        if long_name in seen_long:
            raise ValueError(
                f"duplicate long name {long_name!r}: {seen_long[long_name]} and {agent}")
        seen_slugs[slug] = agent
        seen_long[long_name] = agent


validate_roster()
