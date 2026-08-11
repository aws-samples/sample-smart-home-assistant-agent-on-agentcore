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

The session plumbing lives in common/gateway_tools.py, shared with the other
tool-using sub-agents — `user_id` is injected there from the verified identity and
never appears in a tool's signature, so the model cannot address another user's
devices.
"""

from __future__ import annotations

import logging

from common.gateway_tools import (
    CONTROL,
    DISCOVER,
    QUERY_HISTORY,
    QUERY_STATE,
    GatewaySession,
)

logger = logging.getLogger(__name__)

WANTED = (DISCOVER, CONTROL, QUERY_STATE, QUERY_HISTORY)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`.

    Returns an empty list when there is no Gateway configured or no verified
    user — an agent with no tools still answers, and saying "I could not reach
    your devices" beats acting on the wrong ones. `common.server` refuses the
    request before this is reached when the user token is missing, so the empty
    case here is a configuration problem rather than an auth one.
    """
    try:
        session = GatewaySession(caller, WANTED)
    except RuntimeError as exc:
        logger.error("could not open a gateway session: %s", exc)
        return []

    from strands import tool as strands_tool

    tools: list = []

    if session.has(DISCOVER):
        @strands_tool
        def discover_devices() -> str:
            """List the user's devices with their ids, rooms, and what each one
            can actually do.

            SKIP THIS CALL when the request already contains a "Devices already
            identified for this request" list — it carries the same ids and
            capability ranges, so calling this costs a round trip and returns what
            you were already given.

            Call it when the request has no such list, when the device you need is
            missing from it, when what is listed contradicts the request, or when
            the request names a room or a category ("the lights") you cannot map to
            an exact device id. Ids and ranges come from here or from the request,
            never from memory."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
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
            return session.call(QUERY_STATE, args)
        tools.append(query_device_state)

    if session.has(QUERY_HISTORY):
        @strands_tool
        def query_sensor_history(device_id: str = "", metric: str = "",
                                 hours: int = 24) -> str:
            """Read a sensor metric over a time window, with min / max / average /
            latest already computed. Use this for trends and past values; use
            query_device_state for the current one. `metric` is one the device
            reports (temperature, humidity, pm25, co2, water_level, filter_life,
            bin_level) — omit it for all of them. `hours` is 1 to 168."""
            args: dict = {"hours": hours}
            if device_id:
                args["device_id"] = device_id
            if metric:
                args["metric"] = metric
            return session.call(QUERY_HISTORY, args)
        tools.append(query_sensor_history)

    if session.has(CONTROL):
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
            return session.call(CONTROL, {"device_id": device_id,
                                          "command": command})
        tools.append(control_device)

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the device tools — Cedar policy or "
            "the user's tool permissions", caller.sub[:8])

    return tools
