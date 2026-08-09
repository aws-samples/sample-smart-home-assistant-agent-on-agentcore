"""Gateway tools for the device-control agent, built per request.

`build_tools(caller)` is called once per A2A request with the caller identity
`common.server` verified, and returns that request's tools. It must be a factory
rather than a module-level list: the tools carry the calling user's identity, so a
list built once would pin the first caller and act as them for everyone after.

Every tool here goes through the SAME AgentCore Gateway the orchestrator uses,
authenticated with the end user's own idToken. That is the whole point of
forwarding the token instead of giving this runtime its own IoT permissions:

  - the Gateway validates the user's JWT (CUSTOM_JWT)
  - Cedar evaluates that user against the per-tool policies, so revoking
    `control_device` for someone in the Admin Console stops this agent too
  - iot-control still resolves the device against the catalog and clamps
    out-of-range parameters
  - the MQTT topic stays `smarthome/{sub}/...`, so per-user isolation holds

A runtime with `iot:Publish` on `smarthome/*` would bypass all four. This one has
no IoT permissions at all.

`user_id` is injected here from the verified identity and never appears in a
tool's signature, so the model cannot address another user's devices — the same
guarantee the orchestrator's wrappers give.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# Gateway tool names arrive prefixed with their target
# (`SmartHomeDeviceControl___control_device`), so match on the suffix.
_CONTROL = "control_device"
_DISCOVER = "discover_devices"
_QUERY_STATE = "query_device_state"
_QUERY_HISTORY = "query_sensor_history"


def _gateway_url() -> str:
    """The tools Gateway URL, from whichever env var the platform set.

    `agentcore deploy` injects `AGENTCORE_GATEWAY_<NAME>_URL`, so the exact name
    depends on the gateway's name — hence the prefix scan rather than a hardcoded
    key. An explicit AGENTCORE_GATEWAY_URL wins if set.
    """
    direct = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    if direct:
        return direct
    for key, value in os.environ.items():
        if key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_URL"):
            return value
    return ""


def _mcp_text(result) -> str:
    """Flatten an MCP tool result into the string a Strands tool must return.

    `call_tool_sync` returns an MCPToolResult, which is a TypedDict — so the
    content is `result["content"]`, not `result.content`, and each item is a dict
    with a "text" key rather than an object with a `.text` attribute. Handling
    only the attribute form leaves the model reading a stringified Python dict
    with the payload buried in it: technically the data, practically unusable.
    Both shapes are handled because a future SDK version may return either.
    """
    content = None
    if isinstance(result, dict):
        content = result.get("content")
    if content is None:
        content = getattr(result, "content", None)
    if not content:
        return str(result)

    texts = []
    for item in content:
        if isinstance(item, dict):
            if "text" in item:
                texts.append(item["text"])
        elif hasattr(item, "text"):
            texts.append(item.text)
    if texts:
        return "\n".join(texts)
    return json.dumps(content, default=str)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`.

    Returns an empty list when there is no Gateway configured or no verified
    user — an agent with no tools still answers, and saying "I could not reach
    your devices" beats acting on the wrong ones. `common.server` refuses the
    request before this is reached when the user token is missing, so the empty
    case here is a configuration problem rather than an auth one.
    """
    gateway_url = _gateway_url()
    if not gateway_url:
        logger.error(
            "no AGENTCORE_GATEWAY_*_URL in the environment — this agent cannot "
            "reach any device tools")
        return []
    if not caller.sub:
        logger.error("no verified user identity — refusing to build device tools")
        return []

    from mcp.client.streamable_http import streamablehttp_client
    from strands import tool as strands_tool
    from strands.tools.mcp.mcp_client import MCPClient

    # The user's own token, so the Gateway and Cedar see the real end user rather
    # than this agent's service identity.
    client = MCPClient(lambda: streamablehttp_client(
        gateway_url, headers={"Authorization": f"Bearer {caller.raw_token}"}))

    # MCPClient is a context manager whose background pump has to stay alive for
    # as long as the tools might be called. Entering it here and leaving it open
    # for the request is deliberate; the per-request Agent is discarded when the
    # request ends, and the client with it.
    client.start()

    available: dict[str, str] = {}
    try:
        mcp_tools = []
        pagination_token = None
        while True:
            page = client.list_tools_sync(pagination_token=pagination_token)
            mcp_tools.extend(page)
            if page.pagination_token is None:
                break
            pagination_token = page.pagination_token
        for t in mcp_tools:
            name = getattr(t, "tool_name", "")
            for suffix in (_CONTROL, _DISCOVER, _QUERY_STATE, _QUERY_HISTORY):
                if name == suffix or name.endswith("___" + suffix):
                    available[suffix] = name
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not list gateway tools")
        client.stop(None, None, None)
        raise RuntimeError(f"could not reach the device gateway: {exc}") from exc

    # Cedar's default-deny means a tool the user is not permitted simply is not
    # listed, so this doubles as a report of what this user may do.
    logger.info("device tools available to sub=%s...: %s",
                caller.sub[:8], sorted(available))

    def call(suffix: str, args: dict) -> str:
        """Invoke a gateway tool with the caller's identity injected."""
        return _mcp_text(client.call_tool_sync(
            tool_use_id=str(uuid.uuid4()),
            name=available[suffix],
            arguments={**args, "user_id": caller.sub},
        ))

    tools: list = []

    if _DISCOVER in available:
        @strands_tool
        def discover_devices() -> str:
            """List the user's devices with their ids, rooms, and what each one
            can actually do. Call this FIRST when a request names a room, a
            category ("the lights"), or anything you cannot map to an exact
            device id — the ids and capability ranges come from here, never from
            memory."""
            return call(_DISCOVER, {})
        tools.append(discover_devices)

    if _QUERY_STATE in available:
        @strands_tool
        def query_device_state(device_id: str = "", device_type: str = "") -> str:
            """Read what devices are doing RIGHT NOW — power, brightness, mode,
            speed, sensor readings, online status. Pass a `device_id` for one
            device, or neither argument for all of them. A device that has
            reported no state means the simulator is closed, NOT that the device
            is off; say so rather than guessing."""
            args = {}
            if device_id:
                args["device_id"] = device_id
            if device_type:
                args["device_type"] = device_type
            return call(_QUERY_STATE, args)
        tools.append(query_device_state)

    if _QUERY_HISTORY in available:
        @strands_tool
        def query_sensor_history(device_id: str = "", metric: str = "",
                                 hours: int = 24) -> str:
            """Read a sensor metric over a time window, with min / max / average /
            latest already computed. Use this for trends and past values; use
            query_device_state for the current one. `metric` is one the device
            reports (temperature, humidity, pm25, co2, water_level, filter_life,
            bin_level) — omit it for all of them. `hours` is 1 to 168."""
            args = {"hours": hours}
            if device_id:
                args["device_id"] = device_id
            if metric:
                args["metric"] = metric
            return call(_QUERY_HISTORY, args)
        tools.append(query_sensor_history)

    if _CONTROL in available:
        @strands_tool
        def control_device(device_id: str, command: dict) -> str:
            """Send ONE command to ONE device. `device_id` is an exact id from
            discover_devices. `command` is an object with an `action` and that
            action's parameters, e.g. {"action": "setBrightness",
            "brightness": 30} or {"action": "setPower", "power": false}.

            Call it once per device — there is no batch form. Out-of-range
            numbers are clamped rather than rejected, and the reply says so, so
            report the clamped value to the user rather than the one requested.
            A device that does not support the action returns an error naming
            what it does support."""
            return call(_CONTROL, {"device_id": device_id, "command": command})
        tools.append(control_device)

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the device tools — Cedar policy or "
            "the user's tool permissions", caller.sub[:8])

    return tools
