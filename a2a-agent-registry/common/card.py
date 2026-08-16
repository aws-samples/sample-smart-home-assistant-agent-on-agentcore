"""AgentCard construction for A2A sample agents (a2a-sdk 0.3.x).

Reads a per-agent ``card.json`` and renders the card two audiences see: the one
published to AWS Agent Registry (``render_card_for_registry``) and the one served at
``/.well-known/agent-card.json`` (``common/server.py``, via ``card_security``).

What the card says about authentication
---------------------------------------
Both used to declare an OAuth2 ``client_credentials`` scheme pointing at Cognito's
token endpoint, filled from ``A2A_TOKEN_URL`` / ``EXPECTED_SCOPE``. That mechanism was
RETIRED: the credential is now the END USER's own id token, and authorization is the
``cognito:groups`` claim on it, checked by the Runtime authorizer. Nothing enforced the
m2m scheme any more — ``common/jwt_verify.py`` is the only code that ever checked a
scope and it is not mounted — so the declaration survived purely as a claim about
ourselves that was no longer true.

That matters because ``securitySchemes`` is a PROTOCOL field: a caller reads it to
decide what to send. Our own orchestrator never reads it (it sends the user's bearer
token unconditionally), which is exactly why the stale declaration cost nothing here
and would have cost a third party their first day.

``card_security`` is the truthful replacement and it mirrors the platform manifest's
published ``card.securitySchemes`` template, so an agent team can copy that template
and get byte-identical output. ``shared/tests/test_card_security_matches_manifest.py``
pins the two together.

Every ``common.*`` import here stays LAZY, inside the function that needs it. This
module is loaded by deploy tooling and by tests from outside the package, where
``common`` is not importable as a package.
"""

from __future__ import annotations

import json
import os
from typing import Any


A2A_PROTOCOL_VERSION = "0.3.0"

# Mirrors `shared/a2a_manifest.SECURITY_SCHEME_NAME`. Not imported from there: that
# module lives in the platform's deployment unit and never ships to a sub-agent.
SECURITY_SCHEME_NAME = "endUserIdToken"


def load_card_json(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def discovery_url_from_env() -> str:
    """This deployment's OIDC discovery URL, from the env the container already has.

    ``COGNITO_REGION`` and ``COGNITO_USER_POOL_ID`` are already required for verifying
    the forwarded user token, so declaring the issuer costs no new variable. Returns
    "" when either is absent, which a caller reports rather than papering over — a
    card that names the WRONG issuer is worse than one that names none.
    """
    region = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION", "")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not region or not pool_id:
        return ""
    return (f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
            "/.well-known/openid-configuration")


def card_security(*, discovery_url: str, door_group: str) -> tuple[dict, list]:
    """``(securitySchemes, security)`` for this deployment's credential.

    Byte-identical to the platform manifest's published ``card.securitySchemes`` /
    ``card.security`` template with ``{cardName}`` substituted — see
    ``shared/a2a_manifest.card_security``, which is the authoritative copy and carries
    the reasoning for the shape (why ``openIdConnect`` and not ``http``/``bearer``, and
    why ``security`` holds an empty scope list).

    Plain dicts, not ``a2a.types`` objects: one caller serialises this into a Registry
    descriptor and the other hands it to an ``AgentCard``, and ``a2a.types`` accepts
    the dict form. Keeping it plain is also what lets the deploy path render a card
    without importing the A2A SDK.
    """
    return (
        {SECURITY_SCHEME_NAME: {
            "type": "openIdConnect",
            "openIdConnectUrl": discovery_url,
            "description": (
                "Send the END USER's own OIDC id token as "
                "`Authorization: Bearer <token>`. There is no machine-to-machine "
                "credential and no second header. Authorization is the "
                "`cognito:groups` claim on that same token: the caller must hold "
                f"`{door_group}`, which a platform administrator grants. A token from "
                "any other issuer is refused before your container is reached."),
        }},
        [{SECURITY_SCHEME_NAME: []}],
    )


def card_security_for(card_name: str, discovery_url: str = "") -> tuple[dict, list]:
    """``card_security`` with the door group derived from the card name.

    The group is ``a2a-<cardName>``, which is what the Runtime authorizer matches, so
    it is derived from the one convention module rather than formatted here.
    """
    from common import a2a_groups

    return card_security(
        discovery_url=discovery_url or discovery_url_from_env(),
        door_group=a2a_groups.agent_group_name(card_name),
    )


def render_card_for_registry(
    card_json: dict[str, Any],
    runtime_url: str,
    discovery_url: str = "",
) -> dict[str, Any]:
    """Render the AgentCard as a plain-JSON dict for the Registry descriptor.

    ``discovery_url`` defaults to this deployment's, from env. It replaced the old
    ``token_url`` / ``scope`` pair, which described the retired m2m flow — see the
    module docstring. A caller with neither the env nor an explicit URL gets a card
    that declares the scheme with an empty issuer, which `a2a_preflight` reports; the
    alternative was silently guessing an issuer, and the wrong issuer refuses every
    caller with nothing to explain it.

    The service validates this against the full A2A schema, so this returns the WHOLE
    card. A hand-trimmed one is rejected as "does not match any supported version".
    """
    schemes, security = card_security_for(card_json["name"], discovery_url)
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": card_json["name"],
        "description": card_json.get("description", ""),
        "url": runtime_url,
        "version": card_json.get("version", "1.0.0"),
        "provider": {
            "organization": (card_json.get("provider") or {}).get(
                "organization", "smarthome-agent-harness"
            ),
            "url": (card_json.get("provider") or {}).get("url", runtime_url),
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "description": s.get("description", ""),
                "tags": list(s.get("tags") or []),
                "examples": list(s.get("examples") or []),
            }
            for s in card_json.get("skills", [])
        ],
        "securitySchemes": schemes,
        "security": security,
    }
