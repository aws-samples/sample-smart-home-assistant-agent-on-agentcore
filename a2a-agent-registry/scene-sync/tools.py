"""Gateway tools for the scene-sync agent, built per request.

Same contract as every other tool-using sub-agent: the caller's verified identity
is injected, never a parameter, and every call goes through the Gateway the
orchestrator uses so Cedar still governs it. See common/gateway_tools.py.

What is specific to this agent is that it drives a LIVE effect rather than storing
one, and the thing it drives has a state it does not control:

  - `sync_mode` on `living-tvlight-1` is what puts the backlight into video or
    music mode. It is inert until something sets it, which is this agent's job.
  - `bluetooth` on the same device is the speaker link, and it is READONLY. Music
    sync produces nothing until it reads `connected`, and pairing takes a moment.
    So `query_device_state` here is not a convenience — it is how the agent tells
    "about to work" from "will never work", and those need opposite replies.

Deliberately NOT here: anything that writes a scene. Saving a feast as a one-tap
command is the task-management agent's job, and the orchestrator strings the two
together. This agent holds device permissions and no table; that one holds a table
and no device permissions. Neither can do the other's half, which is what keeps a
saved scene from becoming a device path Cedar cannot see.
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

            SKIP THIS CALL when the request already contains a "Devices already
            identified for this request" list — it names the same devices and
            capabilities, including which fixture declares `sync_mode`, so calling
            this repeats a question already answered and the user waits for it.

            Call it when the request has no such list, when the fixture you need is
            missing from it, or when what is listed contradicts what was asked. A
            feast is built from what the room actually has, and that varies: only a
            device declaring a `sync_mode` capability can follow a screen or the
            music at all, `segments.count` is how many colours its palette may
            carry (the TV backlight has 4, the living-room strip 30), and
            `effect.values` is the complete set of animation names it accepts."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
        @strands_tool
        def query_device_state(device_id: str = "") -> str:
            """Read a device's current state — including `bluetooth` and `sync_mode`.

            For a MUSIC feast this is not optional. `bluetooth` is the speaker
            link and it is reported by the device, never set by you:

              - `connected` — the link is up; drive the lights.
              - `pairing` — it is coming up. Wait and read again (see
                wait_for_bluetooth) rather than reporting either success or
                failure, because both would be wrong.
              - `idle` — nothing is paired. Say so and tell the user to connect
                their speaker. Do NOT claim the feast is running.

            `sync_mode` tells you whether the device is already in video or music
            mode, so you can avoid re-setting what is already set."""
            args = {}
            if device_id:
                args["device_id"] = device_id
            return session.call(QUERY_STATE, args)
        tools.append(query_device_state)

        @strands_tool
        def wait_for_bluetooth(device_id: str, attempts: int = 6) -> str:
            """Poll `bluetooth` until it settles, then report what it settled on.

            Call this after set_sync_mode('music'). Pairing takes a couple of
            seconds, so a single read straight afterwards usually returns
            `pairing` — and treating that as failure tells the user to reconnect a
            speaker that was about to work.

            Returns as soon as the state is `connected` or `idle`. If it is still
            `pairing` after `attempts` reads, that is the answer: say the link has
            not come up yet rather than guessing which way it went.
            """
            import json
            import time

            last = ""
            for i in range(max(1, min(attempts, 10))):
                if i:
                    # Half a second, roughly the simulator's pairing time over a
                    # few reads. Long enough not to spin, short enough that the
                    # user is not left waiting on a tool call.
                    time.sleep(0.5)
                raw = session.call(QUERY_STATE, {"device_id": device_id})
                try:
                    devices = json.loads(raw).get("devices") or []
                    state = (devices[0].get("state") or {}) if devices else {}
                    last = str(state.get("bluetooth") or "")
                except (ValueError, AttributeError, IndexError, TypeError):
                    # A shape we did not expect is not a pairing answer. Hand the
                    # raw reply back so the model can read it rather than
                    # inventing a state.
                    return raw
                if last in ("connected", "idle"):
                    return json.dumps({"bluetooth": last, "reads": i + 1})
            return json.dumps({
                "bluetooth": last or "unknown",
                "reads": attempts,
                "note": "still pairing after several reads — report that the "
                        "speaker link has not come up yet, and do not claim the "
                        "music feast is running",
            })
        tools.append(wait_for_bluetooth)

    if session.has(CONTROL):
        @strands_tool
        def set_sync_mode(device_id: str, sync_mode: str) -> str:
            """Put a device into `video`, `music` or `off` sync mode.

            This is what makes the backlight follow the screen or the beat; it also
            powers the device on. Only a device whose capabilities include
            `sync_mode` accepts it — check discover_devices first, because on any
            other fixture this returns an error naming what it does support.

            After setting `music`, the speaker link still has to come up: call
            wait_for_bluetooth before you say anything about the result."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setSyncMode", "sync_mode": sync_mode}})
        tools.append(set_sync_mode)

        @strands_tool
        def set_effect(device_id: str, effect: str, colors: list | None = None,
                       effect_speed: int | None = None) -> str:
            """Apply an animated effect to a fixture that is NOT the sync master.

            The sync master follows the screen or the beat by itself once
            set_sync_mode is on — do not also drive it with an effect, or the two
            fight. Use this for the OTHER lights in the room, so the whole space
            joins in: a slow `wave` on the strip for a film, a fast `chase` for
            music.

            `effect` must be one of that device's `effect.values`. `colors` is one
            #RRGGBB per segment, up to `segments.count`; extras are dropped with a
            warning worth passing on. `effect_speed` is 1 (slowest) to 10."""
            command: dict = {"action": "setEffect", "effect": effect}
            if colors:
                command["colors"] = colors
            if effect_speed is not None:
                command["effect_speed"] = effect_speed
            return session.call(CONTROL, {"device_id": device_id, "command": command})
        tools.append(set_effect)

        @strands_tool
        def set_brightness(device_id: str, brightness: int) -> str:
            """Set a fixture's brightness, 0 to 100. Also powers it on.

            A feast is usually a dim room: the screen or the lights are meant to be
            what you look at. 15-35 suits a film, 40-70 suits music."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setBrightness", "brightness": brightness}})
        tools.append(set_brightness)

        @strands_tool
        def set_power(device_id: str, power: bool) -> str:
            """Turn one device on or off.

            Mostly for ending a feast: setting sync_mode `off` stops the following,
            and this is how the lights that joined in go back to how they were."""
            return session.call(CONTROL, {
                "device_id": device_id,
                "command": {"action": "setPower", "power": bool(power)}})
        tools.append(set_power)

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the scene-sync tools — check Cedar "
            "policy and the user's tool permissions", caller.sub[:8])
    return tools
