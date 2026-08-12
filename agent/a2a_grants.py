"""Reading A2A grants out of the caller's token claim.

Split out of `agent.py` so it can be imported without dragging in strands,
playwright and browser-use. It is pure string handling over a JWT payload, and a
unit test of it should not cost half a gigabyte of imports.

A grant is a Cognito group named `a2a-<agent>.<skill>` (see `a2a_groups.py`). The
orchestrator reads the claim to decide which `a2a_*` tools to offer the model; each
sub-agent's Runtime authorizer independently validates the same token and matches the
same claim to decide whether to admit the call. One signed source, so what the model
is offered and what the platform permits cannot drift.
"""

from __future__ import annotations

import base64
import json
import logging

import a2a_groups

logger = logging.getLogger(__name__)

GROUPS_CLAIM = "cognito:groups"


def claims_without_verifying(token: str | None) -> dict:
    """The payload of a JWT, WITHOUT checking its signature.

    Safe only because nothing security-relevant is decided from it. These claims
    choose which tools to *offer*; the authorization decision belongs to the
    sub-agent's Runtime authorizer, which validates this same token properly
    (signature, issuer, audience) and checks the same claim. A forged claim buys a
    tool that is then refused at the door, not access.

    Verifying here too would put a JWKS fetch on the request path for a decision that
    does not need one, and the Runtime has already validated this token inbound.

    Returns `{}` for anything unparseable: a garbled header must cost the turn its
    specialists, not the turn itself.
    """
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw.split(" ", 1)[1].strip()
    parts = raw.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # base64url needs its padding back
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read claims from the user token: %s", exc)
        return {}
    return decoded if isinstance(decoded, dict) else {}


def grants_from_user_token(auth_header: str | None) -> dict[str, list[str]]:
    """`{agentCardName: [skill_id, ...]}` from the caller's `cognito:groups`.

    Keyed on the AgentCard NAME, not the Registry recordId the old grants table used:
    a grant group encodes the name, because that is what the sub-agent knows itself
    as. `build_a2a_tools` resolves names to cards.

    No token, no groups, or an unparseable token all mean NO grants. Never "all".
    """
    if not auth_header:
        return {}
    groups = claims_without_verifying(auth_header).get(GROUPS_CLAIM) or []
    if isinstance(groups, str):  # a single-group claim can arrive unwrapped
        groups = [groups]
    if not isinstance(groups, (list, tuple)):
        logger.warning("unexpected %s claim type: %s", GROUPS_CLAIM, type(groups))
        return {}
    return a2a_groups.grants_from_claim(groups)
