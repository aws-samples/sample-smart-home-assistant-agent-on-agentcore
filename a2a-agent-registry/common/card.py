"""AgentCard construction for A2A sample agents (a2a-sdk 0.3.x).

Reads per-agent ``card.json`` and builds the canonical ``a2a.types.AgentCard``
used by the A2A server. OAuth2 scheme fields are filled from env vars
(``A2A_TOKEN_URL``, ``EXPECTED_SCOPE``) injected by ``deploy.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any


A2A_PROTOCOL_VERSION = "0.3.0"


def load_card_json(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def build_agent_card(card_json: dict[str, Any]) -> Any:
    """Build an ``a2a.types.AgentCard`` object from a card.json dict.

    ``url`` is pulled from ``AGENTCORE_RUNTIME_URL`` env if present (set by
    the Runtime), else the dict's ``url`` field, else a placeholder.
    """
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentProvider,
        AgentSkill,
        ClientCredentialsOAuthFlow,
        OAuth2SecurityScheme,
        OAuthFlows,
        SecurityScheme,
    )

    runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL") or card_json.get("url", "") or "https://example.com"

    skills = [
        AgentSkill(
            id=s["id"],
            name=s.get("name", s["id"]),
            description=s.get("description", ""),
            tags=list(s.get("tags") or []),
            examples=list(s.get("examples") or []),
        )
        for s in card_json.get("skills", [])
    ]

    provider = None
    prov = card_json.get("provider") or {}
    if prov.get("organization"):
        provider = AgentProvider(
            organization=prov["organization"],
            url=prov.get("url", runtime_url),
        )

    security_schemes = None
    security = None
    token_url = os.environ.get("A2A_TOKEN_URL")
    scope = os.environ.get("EXPECTED_SCOPE", "a2a-server/invoke")
    if token_url:
        scheme = OAuth2SecurityScheme(
            description="Cognito client_credentials m2m",
            flows=OAuthFlows(
                client_credentials=ClientCredentialsOAuthFlow(
                    token_url=token_url,
                    scopes={scope: "Invoke A2A agents"},
                )
            ),
        )
        security_schemes = {"oauth2": SecurityScheme(root=scheme)}
        security = [{"oauth2": [scope]}]

    return AgentCard(
        protocol_version=A2A_PROTOCOL_VERSION,
        name=card_json["name"],
        description=card_json.get("description", ""),
        url=runtime_url,
        version=card_json.get("version", "1.0.0"),
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=skills,
        provider=provider,
        security_schemes=security_schemes,
        security=security,
    )


def render_card_for_registry(
    card_json: dict[str, Any],
    runtime_url: str,
    token_url: str,
    scope: str,
) -> dict[str, Any]:
    """Render the AgentCard as a plain-JSON dict for
    ``descriptors.a2a.agentCard.inlineContent`` in AgentCore Registry."""
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
        "securitySchemes": {
            "oauth2": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": token_url,
                        "scopes": {scope: "Invoke A2A agents"},
                    }
                },
            }
        },
        "security": [{"oauth2": [scope]}],
    }
