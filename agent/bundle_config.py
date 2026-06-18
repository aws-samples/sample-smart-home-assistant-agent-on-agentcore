"""W3C baggage-driven AgentCore Configuration Bundle hook.

When the AgentCore Gateway is splitting traffic for an A/B test, it injects
the assigned variant's bundle reference into each request via a W3C
`baggage` header (RFC 7230 list). This module parses that header and, if
present, returns the corresponding system prompt or tool description from
the bundle. Returns None on any failure so the caller falls back to the
existing DDB prompt-resolution path. See spec §7.
"""
import logging
import os
from urllib.parse import unquote

logger = logging.getLogger(__name__)
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

_JSON_PATH_FOR_AGENT = {
    "text": "system_prompt",
    "voice": "system_prompt",
}

_cached_client = None


def _client():
    global _cached_client
    if _cached_client is None:
        import boto3
        _cached_client = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)
    return _cached_client


def _parse_baggage(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (value or "").split(","):
        item = item.strip()
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        k = k.strip()
        v = unquote(v.strip())
        if k:
            out[k] = v
    return out


def _get_header(headers: dict, name: str) -> str | None:
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def load_from_baggage(headers: dict, agent_type: str) -> str | None:
    raw = _get_header(headers, "baggage")
    if not raw:
        return None
    parsed = _parse_baggage(raw)
    bundle_arn = parsed.get("bundle-arn")
    version_id = parsed.get("bundle-version-id")
    if not bundle_arn or not version_id:
        return None
    try:
        # bundleId (not bundleArn) is the real AgentCore parameter name; we
        # accept either an ARN or a UUID in the baggage value and pass it
        # through verbatim. See cdk/lambda/admin-api/optimization.py for the
        # admin-side equivalent.
        resp = _client().get_configuration_bundle_version(
            bundleId=bundle_arn, versionId=version_id,
        )
    except Exception as e:  # noqa: BLE001 — fail-open: any error → DDB fallback
        logger.warning("bundle load failed: arn=%s version=%s err=%s", bundle_arn, version_id, e)
        return None

    key = _JSON_PATH_FOR_AGENT.get(agent_type)
    if not key:
        return None
    components = resp.get("components", [])
    if not components:
        return None
    cfg = components[0].get("configuration", {})
    val = cfg.get(key)
    if not isinstance(val, str):
        return None
    return val


def register_before_model_call_hook(agent, headers: dict | None) -> None:
    """Register a Strands BeforeModelCallEvent hook that overrides
    `system_prompt` from W3C baggage. No-op when baggage is missing or
    the bundle does not resolve. See AgentCore A/B testing config-bundle
    docs for the contract.

    The hook captures `headers` at registration time (per-request agent
    construction guarantees fresh headers per invocation).
    """
    from strands.hooks import BeforeModelCallEvent  # type: ignore

    captured_headers = headers or {}

    def _on_before_model_call(event):
        try:
            prompt = load_from_baggage(captured_headers, "text")
        except Exception as e:  # noqa: BLE001
            logger.warning("bundle hook resolve failed: %s", e)
            return
        if not isinstance(prompt, str) or not prompt:
            return
        # Strands exposes the model-call kwargs on event.kwargs; mutate in place
        # so the underlying model.converse picks up the new system_prompt.
        if hasattr(event, "kwargs") and isinstance(event.kwargs, dict):
            event.kwargs["system_prompt"] = prompt

    # Strands' hook registration API: agent.hooks.add_callback(EventCls, fn)
    # (newer versions) or agent.hooks.register(EventCls, fn) (older).
    register_fn = getattr(agent.hooks, "add_callback", None) or getattr(agent.hooks, "register", None)
    if register_fn is None:
        logger.warning("agent.hooks has no add_callback/register; skipping hook")
        return
    register_fn(BeforeModelCallEvent, _on_before_model_call)
