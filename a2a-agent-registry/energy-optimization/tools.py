"""Gateway tools for the energy-optimization agent, built per request.

This agent had no tools until 2026-08-12. It was a system prompt instructed to
"state the assumptions you used", which meant every kWh and every dollar it
produced was invented during generation: unrepeatable, uncheckable, and something
a plain skill could have done just as well. That is the case for it being a
sub-agent at all, and it did not hold.

With tools, answering "what would dimming the living room save" is a chain that no
single tool call can shortcut:

  discover_devices        which devices exist, and which of them even dim
  query_device_state      what is on right now, and at what brightness
  query_sensor_history    how the room has actually behaved over up to a week
  power_reference         rated draw per device type, so the arithmetic is auditable

and then per-device arithmetic and a ranking across the fleet. That is a dozen or
more calls with intermediate results carried between them, which is the shape of
work that belongs behind its own context window rather than inline in the
orchestrator's.

Same identity model as every other tool-using sub-agent: the caller's own idToken
goes to the Gateway, Cedar evaluates the real end user, and this runtime holds no
device permissions of its own. See common/gateway_tools.py.
"""

from __future__ import annotations

import json
import logging

from common import power_profile
from common.gateway_tools import (
    DISCOVER,
    QUERY_HISTORY,
    QUERY_STATE,
    GatewaySession,
)

logger = logging.getLogger(__name__)

WANTED = (DISCOVER, QUERY_STATE, QUERY_HISTORY)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`.

    `power_reference` is registered unconditionally because it reads a table
    shipped in this image rather than anything belonging to the user — it stays
    useful when Cedar has denied every device tool, which is the difference
    between "I could not read your devices, here is the general figure" and
    silence.
    """
    from strands import tool as strands_tool

    tools: list = []

    @strands_tool
    def power_reference() -> str:
        """Rated power draw per device type, and the electricity rates to price it
        with. Call this ONCE before any energy arithmetic, and use these numbers
        rather than recalling typical wattages — the point is that the user can
        check the figures.

        A device type with `unknown_load` (a smart plug) has a draw this system
        genuinely cannot know: ask what is plugged in rather than assuming one.
        `scales_with` names the capability that moves the draw, so a light at 30%
        brightness is not priced at its full rating. `duty_cycle` applies to
        heating and compressor loads that cycle while reporting themselves on."""
        return json.dumps(power_profile.as_reference())
    tools.append(power_reference)

    try:
        session = GatewaySession(caller, WANTED)
    except RuntimeError as exc:
        # Answering from the reference table alone is a worse answer but a real
        # one, and it says which half is missing.
        logger.error("could not open a gateway session: %s", exc)
        return tools

    if session.has(DISCOVER):
        @strands_tool
        def discover_devices() -> str:
            """List the user's devices with ids, rooms and capabilities. Call this
            FIRST for any question about what the user could save: which devices
            exist, and which of them can dim or vary speed at all, decides what
            advice is even applicable. Never assume a device is present."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
        @strands_tool
        def query_device_state(device_id: str = "", device_type: str = "") -> str:
            """Read what devices are doing RIGHT NOW — power, brightness, speed,
            mode. Pass a `device_id` for one, or neither argument for the whole
            fleet, which is usually what you want for an audit. A device reporting
            no state means the simulator is closed, NOT that the device is off; say
            so rather than counting it as zero."""
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
                                 hours: int = 168) -> str:
            """Read a metric over a time window with min / max / average / latest
            already computed. Defaults to a full week, because a savings estimate
            from a single reading is a guess with a number attached.

            `metric` is one the device reports (temperature, humidity, pm25, co2,
            water_level, filter_life, bin_level) — omit it for all of them.
            `hours` is 1 to 168. Use this to justify a recommendation: a room that
            never leaves 21C does not need the fan it is running."""
            args: dict = {"hours": hours}
            if device_id:
                args["device_id"] = device_id
            if metric:
                args["metric"] = metric
            return session.call(QUERY_HISTORY, args)
        tools.append(query_sensor_history)

    if len(tools) == 1:
        logger.warning(
            "sub=%s... is permitted no device tools — the energy answer will come "
            "from the reference table only", caller.sub[:8])

    return tools
