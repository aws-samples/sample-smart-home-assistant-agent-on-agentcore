"""Rendering + parsing for A2A agent card records stored in AgentCore Registry.

Two descriptor parts, mirroring the Skills split:
  - cardMd.inlineContent          human-readable markdown (frontmatter + body)
  - cardDefinition.inlineContent  canonical A2A AgentCard JSON (single source of truth)

On edit, the UI hydrates from cardDefinition — the markdown is regenerated, never parsed back.
"""
import json
import re

# Slug: leading letter, [a-z0-9-], max 63 chars.
A2A_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

AUTH_SCHEMES = ("none", "bearer", "apiKey")
CAPABILITY_KEYS = ("streaming", "pushNotifications", "stateTransitionHistory")


def validate_form(form):
    """Return (ok, error_msg). Used by POST and PUT handlers before render."""
    name = form.get("name", "")
    if not isinstance(name, str) or not A2A_NAME_RE.match(name):
        return False, f"Invalid name '{name}' (use a-z, 0-9, hyphens; max 63 chars)"
    if not form.get("description", "").strip():
        return False, "description is required"
    if len(form.get("description", "")) > 500:
        return False, "description must be at most 500 characters"
    endpoint = form.get("endpoint", "")
    if not isinstance(endpoint, str) or not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        return False, "endpoint must be an http(s) URL"
    if not form.get("version", "").strip():
        return False, "version is required"
    if form.get("auth") not in AUTH_SCHEMES:
        return False, f"auth must be one of {AUTH_SCHEMES}"
    skills = form.get("skills") or []
    if not isinstance(skills, list) or len(skills) < 1:
        return False, "at least one skill is required"
    for i, s in enumerate(skills):
        if not isinstance(s, dict):
            return False, f"skill[{i}] must be an object"
        if not A2A_NAME_RE.match(s.get("id", "") or ""):
            return False, f"skill[{i}].id is invalid"
        if not s.get("name", "").strip():
            return False, f"skill[{i}].name is required"
    return True, ""


def _quote(s):
    """Quote a scalar for YAML frontmatter."""
    return '"' + str(s).replace('"', '\\"') + '"'


def build_card_md(form):
    """Render the cardMd markdown from a form dict."""
    caps = form.get("capabilities") or {}
    enabled_caps = [k for k in CAPABILITY_KEYS if caps.get(k)]
    tags = form.get("tags") or []

    lines = ["---"]
    lines.append(f"description: {_quote(form.get('description', ''))}")
    lines.append(f"endpoint: {_quote(form.get('endpoint', ''))}")
    lines.append(f"version: {_quote(form.get('version', ''))}")
    if form.get("provider"):
        lines.append(f"provider: {_quote(form['provider'])}")
    lines.append(f"auth: {_quote(form.get('auth', 'none'))}")
    if enabled_caps:
        lines.append(f"x-capabilities: {_quote(','.join(enabled_caps))}")
    if tags:
        lines.append(f"x-tags: {_quote(','.join(tags))}")
    lines.append("---")
    lines.append("")
    lines.append("## Description")
    lines.append("")
    lines.append(form.get("description", ""))
    lines.append("")
    lines.append("## Skills")
    lines.append("")
    for s in form.get("skills", []):
        sid = s.get("id", "")
        sname = s.get("name", "")
        sdesc = s.get("description", "")
        lines.append(f"- **{sid}** — {sname}: {sdesc}")
        examples = s.get("examples") or []
        if examples:
            ex_str = ", ".join(_quote(e) for e in examples)
            lines.append(f"  - examples: {ex_str}")
    return "\n".join(lines)


A2A_PROTOCOL_VERSION = "0.3.0"  # AgentCore Registry accepts protocolVersion in the card JSON


def build_card_definition(form):
    """Render the A2A AgentCard JSON (canonical) from a form dict.

    Includes `protocolVersion` per the A2A spec — AgentCore Registry rejects
    cards that don't declare a supported protocol version.
    """
    caps_form = form.get("capabilities") or {}
    capabilities = {k: bool(caps_form.get(k)) for k in CAPABILITY_KEYS}

    card = {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": form.get("name", ""),
        "description": form.get("description", ""),
        "url": form.get("endpoint", ""),
        "version": form.get("version", ""),
        "capabilities": capabilities,
        "authentication": {"schemes": [form.get("auth", "none")]},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": s.get("id", ""),
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "tags": list(s.get("tags") or []),
                "examples": list(s.get("examples") or []),
            }
            for s in (form.get("skills") or [])
        ],
        "tags": list(form.get("tags") or []),
    }
    if form.get("provider"):
        # A2A spec requires provider to carry both organization + url.
        # Use the endpoint URL origin as a sensible default when no separate
        # provider URL is supplied by the form.
        provider_url = form.get("providerUrl") or form.get("endpoint", "") or "https://example.com"
        card["provider"] = {"organization": form["provider"], "url": provider_url}
    return json.dumps(card)


def parse_card_definition(raw):
    """Parse a cardDefinition JSON string back into a form dict. Safe on empty/bad input."""
    empty = {
        "name": "",
        "description": "",
        "endpoint": "",
        "version": "",
        "provider": "",
        "capabilities": {k: False for k in CAPABILITY_KEYS},
        "auth": "none",
        "tags": [],
        "skills": [],
    }
    if not raw:
        return empty
    try:
        card = json.loads(raw)
    except (ValueError, TypeError):
        return empty

    caps_card = card.get("capabilities") or {}
    provider = card.get("provider") or {}
    auth_schemes = ((card.get("authentication") or {}).get("schemes") or ["none"])

    return {
        "name": card.get("name", ""),
        "description": card.get("description", ""),
        "endpoint": card.get("url", ""),
        "version": card.get("version", ""),
        "provider": provider.get("organization", "") if isinstance(provider, dict) else "",
        "capabilities": {k: bool(caps_card.get(k)) for k in CAPABILITY_KEYS},
        "auth": auth_schemes[0] if auth_schemes else "none",
        "tags": list(card.get("tags") or []),
        "skills": [
            {
                "id": s.get("id", ""),
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "examples": list(s.get("examples") or []),
                # Preserve any tags the card carried; the form editor doesn't
                # surface skill-level tags, but the A2A spec allows them.
                "tags": list(s.get("tags") or []),
            }
            for s in (card.get("skills") or [])
        ],
    }
