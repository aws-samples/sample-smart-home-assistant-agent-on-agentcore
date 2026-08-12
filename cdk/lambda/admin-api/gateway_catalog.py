"""Which tools each AgentCore Gateway exposes, and where each one is governed.

Two callers used to walk the gateway's targets independently — `list_gateway_tools`
for the Tool Policy page and `_get_tool_action_map` for the Cedar action name — and
they parsed the same `targetConfiguration` two slightly different ways. That was
survivable while there was one gateway holding one kind of target. Two things broke
it at once:

  - A second gateway. The `web-search` connector is not offered in us-west-2
    (`create_gateway_target` answers "Connector integration web-search is not
    available for this account", which reads like an entitlement problem and is
    actually regional), so it lives on a gateway in us-east-1. A tool's Cedar
    policy has to be written to the policy engine in ITS OWN region, so "which
    gateway is this tool on" became a question with a real answer.
  - A second kind of target. Both old parsers read `mcp.lambda.toolSchema` and
    nothing else, so a `mcp.connector` target contributed no tools. The Tool
    Policy page would simply not list web search, and `build_cedar_statement`
    would raise "not exposed by any gateway target" for it — an ungovernable tool
    that looks, from the console, like a tool nobody granted.

So target parsing lives here once, and both callers read the same catalog.

The Cedar action name is `{targetName}___{toolName}` for both target kinds. For a
connector the tool name is the *configuration* name rather than anything in a
schema — `configurations: [{"name": "WebSearch"}]` on a target called
`SmartHomeWebSearch` yields `SmartHomeWebSearch___WebSearch`, which is confirmed
against the live `tools/list` output rather than inferred from the shape.
"""

from __future__ import annotations

import json
import logging
import os
from collections import namedtuple

import boto3

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-west-2")

# The tools gateway: Lambda targets for device control, discovery, state, history,
# the knowledge base and navigation.
GATEWAY_ID = os.environ.get("GATEWAY_ID", "")
# The web-search gateway. Separate id AND region, because the connector is only
# offered in us-east-1 — see the module docstring. Empty when it has not been
# provisioned, which must degrade to "the other gateway still works" rather than
# to an error: web search is an added capability, not a dependency.
WEBSEARCH_GATEWAY_ID = os.environ.get("WEBSEARCH_GATEWAY_ID", "")
WEBSEARCH_GATEWAY_REGION = os.environ.get("WEBSEARCH_GATEWAY_REGION", "us-east-1")

Gateway = namedtuple("Gateway", "id region label")

_clients: dict[str, object] = {}
_catalog: list[dict] | None = None


def gateways() -> list[Gateway]:
    """Every gateway this deployment governs, in a stable order.

    The tools gateway comes first so that a bare tool-name collision between the
    two resolves to the one that has always existed, rather than to whichever
    gateway happened to be scanned first.
    """
    out = []
    if GATEWAY_ID:
        out.append(Gateway(GATEWAY_ID, REGION, "tools"))
    if WEBSEARCH_GATEWAY_ID:
        out.append(Gateway(WEBSEARCH_GATEWAY_ID, WEBSEARCH_GATEWAY_REGION, "websearch"))
    return out


def control(region: str):
    """A cached `bedrock-agentcore-control` client for one region.

    Per-region rather than one module-level client: a gateway, its targets, its
    policy engine and its policies are all regional, so a call made against the
    wrong region does not fail with a region error — it reports that the gateway
    does not exist, which reads as "deleted".
    """
    if region not in _clients:
        _clients[region] = boto3.client("bedrock-agentcore-control", region_name=region)
    return _clients[region]


def _tool_defs_from_lambda(mcp: dict, s3_client) -> list[dict]:
    """Tool definitions from a Lambda target's schema, inline or via S3."""
    tool_schema = (mcp.get("lambda") or {}).get("toolSchema") or {}
    tool_defs = tool_schema.get("inlinePayload") or []
    if not tool_defs and "s3" in tool_schema:
        s3_uri = (tool_schema["s3"] or {}).get("uri", "")
        if s3_uri.startswith("s3://"):
            parts = s3_uri[5:].split("/", 1)
            if len(parts) == 2:
                obj = s3_client.get_object(Bucket=parts[0], Key=parts[1])
                tool_defs = json.loads(obj["Body"].read())
    return tool_defs or []


def _tool_defs_from_connector(mcp: dict) -> list[dict]:
    """Tool definitions from a built-in connector target.

    A connector carries no tool schema of its own — the Gateway snapshots that
    from the service. What names the tool is the configuration entry, so that is
    what the Cedar action is built from. The description is synthesised because
    the API returns none, and a blank description on the Tool Policy page reads as
    a broken row.
    """
    connector = mcp.get("connector") or {}
    source = connector.get("source") or {}
    connector_id = source.get("connectorId", "")
    version = source.get("version", "")
    out = []
    for cfg in connector.get("configurations") or []:
        name = cfg.get("name", "")
        if not name:
            continue
        suffix = f" (connector {connector_id}"
        suffix += f" v{version})" if version else ")"
        out.append({
            "name": name,
            "description": f"AWS-managed {connector_id} connector tool{suffix}",
            "connectorId": connector_id,
            "connectorVersion": version,
        })
    return out


def _scan(gateway: Gateway, s3_client) -> list[dict]:
    """Every tool on one gateway, with the Cedar action name for each."""
    client = control(gateway.region)
    out: list[dict] = []
    try:
        targets = client.list_gateway_targets(gatewayIdentifier=gateway.id)
    except Exception as exc:  # noqa: BLE001
        # One unreachable gateway must not empty the catalogue of the other. The
        # caller surfaces this; returning [] silently would render as "this
        # deployment has no tools", which is indistinguishable from a revoked
        # policy and sends an admin looking in the wrong place.
        logger.warning("could not list targets on gateway %s (%s): %s",
                       gateway.id, gateway.region, exc)
        raise

    for summary in targets.get("items", []):
        target_name = summary.get("name", "")
        try:
            target = client.get_gateway_target(
                gatewayIdentifier=gateway.id, targetId=summary["targetId"])
            mcp = (target.get("targetConfiguration") or {}).get("mcp") or {}
            tool_defs = _tool_defs_from_lambda(mcp, s3_client)
            kind = "lambda"
            if not tool_defs:
                tool_defs = _tool_defs_from_connector(mcp)
                kind = "connector" if tool_defs else "unknown"
            for td in tool_defs:
                name = td.get("name", "")
                if not name:
                    continue
                out.append({
                    "name": name,
                    "description": td.get("description", ""),
                    "targetName": target_name,
                    # What Cedar will actually see on the wire.
                    "actionName": f"{target_name}___{name}",
                    "gatewayId": gateway.id,
                    "region": gateway.region,
                    "gatewayLabel": gateway.label,
                    "targetKind": kind,
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read target %s on %s: %s",
                           target_name, gateway.id, exc)
    return out


def catalog(s3_client, refresh: bool = False) -> tuple[list[dict], list[str]]:
    """Every governable tool across every gateway. Returns (tools, errors).

    Cached for the life of the container, which is right for reads and wrong
    immediately after a target is registered: a warm container would keep the old
    map and `build_cedar_statement` would write a policy naming an action the
    Gateway never emits. That policy then goes ACTIVE and permits nothing — a
    silent deny that looks exactly like a successful deploy. Pass refresh=True on
    every write path.

    Errors are returned rather than raised so that one unreachable gateway still
    leaves the other's tools governable.
    """
    global _catalog
    if refresh:
        _catalog = None
    if _catalog is None:
        tools: list[dict] = []
        errors: list[str] = []
        seen: dict[str, str] = {}
        for gateway in gateways():
            try:
                found = _scan(gateway, s3_client)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{gateway.label} gateway ({gateway.region}): {exc}")
                continue
            for entry in found:
                prior = seen.get(entry["name"])
                if prior:
                    # `allowedTools` in DynamoDB is keyed on the bare tool name, so
                    # two gateways exposing the same name would make one grant mean
                    # two different tools. Keep the first and say so loudly.
                    logger.warning(
                        "tool name %r is exposed by both %s and %s; keeping %s "
                        "because per-user grants are keyed on the bare name",
                        entry["name"], prior, entry["gatewayId"], prior)
                    continue
                seen[entry["name"]] = entry["gatewayId"]
                tools.append(entry)
        _catalog = tools
        # Cache only the tools; errors are per-call so a transient failure does not
        # stick to a warm container.
        return tools, errors
    return _catalog, []


def entry_for(tool_name: str, s3_client, refresh: bool = False) -> dict | None:
    """The catalog entry for one tool, or None when no gateway exposes it."""
    tools, _ = catalog(s3_client, refresh=refresh)
    for entry in tools:
        if entry["name"] == tool_name:
            return entry
    return None


def gateway_arn(gateway_id: str, region: str) -> str:
    """The gateway's ARN, which Cedar names as the resource."""
    key = f"arn:{gateway_id}"
    if key not in _clients:
        gw = control(region).get_gateway(gatewayIdentifier=gateway_id)
        _clients[key] = gw.get("gatewayArn", "")
    return _clients[key]
