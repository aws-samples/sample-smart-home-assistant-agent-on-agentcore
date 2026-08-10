"""Gateway tools for the light-effect agent, built per request.

Same contract as every other tool-using sub-agent: the caller's verified identity
is injected, never a parameter, and every call goes through the Gateway the
orchestrator uses so Cedar still governs it. See common/gateway_tools.py.

What is specific to this agent is the shape of the work. A lighting effect is not
one command — it is a palette plus an animation plus a speed, and what a fixture
can actually render varies:

  - `living-strip-1` has 30 addressable segments and 7 effects
  - `living-tvlight-1` has 4 segments (the TV's edges) and the same 7 effects
  - `living-led-1` is a 16x16 matrix with named modes, not segments
  - `bedroom-light-1` is a single colour and a colour temperature — no effect at all

So the agent has to read the fixture's capabilities before it can compose
anything, which is why `discover_devices` is not optional here. A palette longer
than the strip is truncated by the catalog rather than rejected, and the reply
says so — worth relaying, because "I used the first 4 of your 8 colours" is the
honest description of what the TV backlight did.
"""

from __future__ import annotations

import logging

from common.gateway_tools import CONTROL, DISCOVER, QUERY_STATE, GatewaySession

logger = logging.getLogger(__name__)

WANTED = (DISCOVER, CONTROL, QUERY_STATE)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`."""
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
            """List the user's devices with their ids, rooms and capabilities.

            Call this FIRST, every time. You cannot compose an effect without
            knowing which fixtures exist and what each one supports: the
            `segments.count` tells you how many colours a palette may contain,
            `effect.values` is the ONLY set of animation names that fixture
            accepts, and a light with no `effect` capability can take a colour but
            not an animation."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
        @strands_tool
        def query_device_state(device_id: str = "") -> str:
            """Read what a light is currently showing — power, brightness, colour,
            active effect. Useful when the user asks for a change relative to now
            ("warmer", "dimmer", "slower") rather than an absolute effect."""
            args = {}
            if device_id:
                args["device_id"] = device_id
            return session.call(QUERY_STATE, args)
        tools.append(query_device_state)

    if session.has(CONTROL):
        @strands_tool
        def set_effect(device_id: str, effect: str, colors: list | None = None,
                       effect_speed: int | None = None) -> str:
            """Apply an animated effect to one addressable fixture.

            `effect` MUST be one of that device's `effect.values` from
            discover_devices — inventing a name gets an error listing the valid
            ones. `colors` is a list of #RRGGBB strings, one per segment; supply
            up to the fixture's `segments.count` and the rest are dropped with a
            warning you should pass on. `effect_speed` is 1 (slowest) to 10.

            Only fixtures that declare an `effect` capability accept this. For a
            plain light use set_color or set_color_temp."""
            command: dict = {"action": "setEffect", "effect": effect}
            if colors:
                command["colors"] = colors
            if effect_speed is not None:
                command["effect_speed"] = effect_speed
            return session.call(CONTROL, {"device_id": device_id, "command": command})
        tools.append(set_effect)

        @strands_tool
        def set_color(device_id: str, color: str) -> str:
            """Set one fixture to a single colour. `color` is #RRGGBB.

            Use for fixtures with no `effect` capability, or when the mood is a
            steady colour rather than an animation."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setColor", "color": color}})
        tools.append(set_color)

        @strands_tool
        def set_color_temp(device_id: str, color_temp: int) -> str:
            """Set a fixture's white balance in Kelvin (2000 warm to 6500 cool).

            The right tool for "cosy", "warm", "daylight" — a colour temperature
            reads as natural white, where an orange RGB value reads as coloured
            light. Out-of-range values are clamped and the reply says so."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setColorTemp", "color_temp": color_temp}})
        tools.append(set_color_temp)

        @strands_tool
        def set_brightness(device_id: str, brightness: int) -> str:
            """Set a fixture's brightness, 0 to 100. Also powers it on."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setBrightness", "brightness": brightness}})
        tools.append(set_brightness)

        @strands_tool
        def set_matrix_mode(device_id: str, mode: str) -> str:
            """Set the LED matrix's animation mode.

            The matrix takes named modes (rainbow / breathing / chase / sparkle /
            fire / ocean / aurora / solid) rather than a segment palette, so it
            needs its own call. Check the device's `mode.values` from
            discover_devices for the accepted names."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setMode", "mode": mode}})
        tools.append(set_matrix_mode)

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the lighting tools — check Cedar "
            "policy and the user's tool permissions", caller.sub[:8])
    return tools
