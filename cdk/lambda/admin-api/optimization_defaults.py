"""Static maps for AgentCore Optimization handlers (spec §5).

`agentType` is one of "text", "voice", "tool_desc". For text/voice the
component ARN is the agent runtime ARN (read from env at import time, same
source as the existing /sessions stop handler). For tool_desc each gateway
target gets its own bundle, so `component_arn_for("tool_desc", target_arn=...)`
is required and raises ValueError if `target_arn` is missing.
"""

import os

_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_VOICE_RUNTIME_ARN = os.environ.get("VOICE_AGENT_RUNTIME_ARN", "")

_VALID_AGENT_TYPES = ("text", "voice", "tool_desc")
_OPT_KINDS = ("rec", "bundle", "abtest")


def component_arn_for(agent_type: str, *, target_arn: str | None = None) -> str:
    if agent_type == "text":
        return _RUNTIME_ARN
    if agent_type == "voice":
        return _VOICE_RUNTIME_ARN
    if agent_type == "tool_desc":
        if not target_arn:
            raise ValueError("tool_desc requires target_arn=...")
        return target_arn
    raise ValueError(f"Unknown agentType: {agent_type}")


def json_path_for(agent_type: str, *, tool_name: str | None = None) -> str:
    if agent_type in ("text", "voice"):
        return "$.configuration.system_prompt"
    if agent_type == "tool_desc":
        if not tool_name:
            return "$.configuration.tools"
        return f"$.configuration.tools.{tool_name}.description"
    raise ValueError(f"Unknown agentType: {agent_type}")


def opt_sk(kind: str, ident: str) -> str:
    """Build a reserved sort key like `__opt_rec_{id}__`."""
    if kind not in _OPT_KINDS:
        raise ValueError(f"Unknown opt kind: {kind}")
    return f"__opt_{kind}_{ident}__"


def parse_opt_sk(skill_name: str) -> tuple[str, str] | None:
    """Reverse of `opt_sk`. Returns (kind, ident) or None if not an opt SK."""
    if not (skill_name.startswith("__opt_") and skill_name.endswith("__")):
        return None
    inner = skill_name[len("__opt_"):-len("__")]
    for kind in _OPT_KINDS:
        prefix = f"{kind}_"
        if inner.startswith(prefix):
            return kind, inner[len(prefix):]
    return None
