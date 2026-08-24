"""From an AgentCard's `url` back to the AgentCore Runtime behind it.

Three callers need this same hop and used to be able to disagree about it:

  - the conformance check, to read a runtime's `authorizerConfiguration`
  - the gateway-target reconcile, to tell "already fronted" from "needs a target"
  - the ops dashboard, to know which runtimes' spans belong to this project

Two URL shapes, and each 404s at the other's path, so guessing is not an option:

    direct     https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations
    gateway    https://{gwId}.gateway.bedrock-agentcore.{region}.amazonaws.com/{target}

The gateway shape needs one extra call: the target's name is the URL's last segment
and its `passthrough.endpoint` is the direct URL. This is not future-proofing — our
own eight cards moved from the first shape to the second on 2026-08-15, and a third
party registering their own runtime uses the first.

Lives in the admin Lambda rather than `shared/` because it is the only deployment unit
that holds `bedrock-agentcore-control` credentials for this. The PURE half (parsing a
URL) is separated from the half that calls AWS so the parsing is testable offline,
which is where the encoding mistakes actually happen.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

logger = logging.getLogger(__name__)

RUNTIME_URL_MARKER = "/runtimes/"
GATEWAY_HOST_MARKER = ".gateway.bedrock-agentcore."
INVOCATIONS_SUFFIX = "/invocations"


# ---------------------------------------------------------------------------
# Pure: URL <-> ARN
# ---------------------------------------------------------------------------

def runtime_arn_from_url(url: str) -> str:
    """The full agentRuntime ARN inside a Runtime data-plane URL, or "".

    The ARN is URL-ESCAPED inside the path (`%3A` for `:`, `%2F` for `/`), which is
    why this unquotes rather than splitting on `:`.
    """
    if not url or RUNTIME_URL_MARKER not in url:
        return ""
    tail = url.split(RUNTIME_URL_MARKER, 1)[1]
    arn = unquote(tail.split(INVOCATIONS_SUFFIX, 1)[0])
    return arn if arn.startswith("arn:") else ""


def runtime_id_from_url(url: str) -> str:
    """The agentRuntimeId inside a Runtime data-plane URL, or "".

    The ARN's own last segment. Returns "" for a gateway URL rather than guessing —
    `resolve` is what handles that shape.
    """
    arn = runtime_arn_from_url(url)
    return arn.rsplit("/", 1)[-1] if "/" in arn else ""


def gateway_target_from_url(url: str) -> tuple[str, str]:
    """`(gatewayId, targetName)` for a gateway URL, or `("", "")`."""
    if not url or GATEWAY_HOST_MARKER not in url:
        return "", ""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    gateway_id = host.split(".", 1)[0]
    target_name = url.rstrip("/").rsplit("/", 1)[-1]
    # A bare gateway host with no path names no target. Returning the host as the
    # target would look up a target named after the gateway and report "no such
    # target", which reads like the target was deleted.
    if not target_name or target_name == host:
        return gateway_id, ""
    return gateway_id, target_name


def gateway_base_url(url: str) -> str:
    """`https://{gwId}.gateway...amazonaws.com` for a gateway URL, else "".

    Lets the manifest publish this deployment's gateway without another environment
    variable to lose: an APPROVED record already routed through the gateway names it.
    """
    if not url or GATEWAY_HOST_MARKER not in url:
        return ""
    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}" if host else ""


# ---------------------------------------------------------------------------
# Needs AWS: the gateway hop
# ---------------------------------------------------------------------------

def resolve(url: str, agentcore_control) -> tuple[str, str]:
    """`(agentRuntimeId, how it was resolved)` for a card's URL.

    Never raises: a failure to resolve is reported as an empty id plus a human
    sentence, because every caller has something useful to say about "could not
    check" and nothing useful to do with an exception.
    """
    if GATEWAY_HOST_MARKER in (url or ""):
        gateway_id, target_name = gateway_target_from_url(url)
        if not target_name:
            return "", f"gateway {gateway_id} url names no target"
        try:
            for page in agentcore_control.get_paginator(
                    "list_gateway_targets").paginate(gatewayIdentifier=gateway_id):
                for tgt in page.get("items", []):
                    if tgt.get("name") != target_name:
                        continue
                    full = agentcore_control.get_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=tgt["targetId"])
                    endpoint = target_endpoint(full)
                    rid = runtime_id_from_url(endpoint)
                    if rid:
                        return rid, f"gateway {gateway_id} target {target_name}"
                    return "", (f"gateway target {target_name} is not a runtime "
                                "passthrough")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not resolve gateway target for %s: %s", url, exc)
            return "", f"gateway target lookup failed: {exc}"
        return "", f"no target named {target_name} on {gateway_id}"

    rid = runtime_id_from_url(url)
    return ((rid, "direct runtime url") if rid
            else ("", "url is neither a runtime nor a known gateway"))


def resolve_arn(url: str, agentcore_control) -> str:
    """The full agentRuntime ARN behind a card's URL, or "".

    The ARN rather than the id, because callers that aggregate telemetry need the
    runtime NAME too (`service.name` is `<runtimeName>.<endpoint>`), and the name is
    only derivable from the ARN's last segment. A gateway URL costs the same hop as
    `resolve`: the target's endpoint is itself a direct runtime URL.
    """
    direct = runtime_arn_from_url(url)
    if direct:
        return direct
    if GATEWAY_HOST_MARKER not in (url or ""):
        return ""
    gateway_id, target_name = gateway_target_from_url(url)
    if not target_name:
        return ""
    try:
        for page in agentcore_control.get_paginator(
                "list_gateway_targets").paginate(gatewayIdentifier=gateway_id):
            for tgt in page.get("items", []):
                if tgt.get("name") != target_name:
                    continue
                full = agentcore_control.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=tgt["targetId"])
                return runtime_arn_from_url(target_endpoint(full))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not resolve the runtime ARN for %s: %s", url, exc)
    return ""


def target_endpoint(target_detail: dict) -> str:
    """The passthrough endpoint of a GetGatewayTarget response, or ""."""
    return (((target_detail.get("targetConfiguration") or {}).get("http")
             or {}).get("passthrough") or {}).get("endpoint", "")


def list_targets(agentcore_control, gateway_id: str) -> list[dict]:
    """`[{name, targetId, endpoint, runtimeId}]` for every target on a gateway.

    One GetGatewayTarget per target, because ListGatewayTargets does not return the
    endpoint — and the endpoint is the only thing that identifies which agent a target
    fronts. Matching on the NAME instead would break the moment a card is renamed.
    """
    out = []
    for page in agentcore_control.get_paginator("list_gateway_targets").paginate(
            gatewayIdentifier=gateway_id):
        for tgt in page.get("items", []):
            name = tgt.get("name") or ""
            try:
                full = agentcore_control.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=tgt["targetId"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read gateway target %s: %s", name, exc)
                continue
            endpoint = target_endpoint(full)
            out.append({
                "name": name,
                "targetId": tgt["targetId"],
                "status": tgt.get("status", ""),
                "endpoint": endpoint,
                "runtimeId": runtime_id_from_url(endpoint),
            })
    return out
