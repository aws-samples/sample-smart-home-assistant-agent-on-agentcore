"""A short device summary the orchestrator can hand to a specialist.

Why this exists
---------------
Measured on the deployed system: a specialist's FIRST event-loop cycle exists
only to call `discover_devices`. The call itself is cheap (~0.2s) but the cycle
around it is an LLM turn, and that costs 1.0-1.3s of the ~7s the specialist
takes. Delegation is two serial agent turns already; paying for a third just to
learn the room's contents is avoidable, because the catalog is STATIC and shared —
the orchestrator can state it up front at no extra call.

Why not just paste `discovery_payload()` in
-----------------------------------------
Because that is 12 devices with full capability schemas, about 1,800 tokens. It
would remove one 1.2s cycle and add 1,800 tokens to the prompt of every delegated
request — moving the cost rather than removing it, which is the specific trap the
spec named. So this trims twice:

  1. **By relevance.** Only devices in the rooms and categories the request
     mentions. A lighting request gets the lights, not the rice cooker.
  2. **By shape.** One line per device: id, name, room, and only the capability
     bounds a specialist needs to pick a legal value (ranges, enum values,
     segment counts). No `actions` list, no types, no connectivity — the
     specialist's tools already state what they accept.

Measured output: 4 devices, about 90 tokens. Roughly a twentieth of the full
payload.

The brief is a HINT, never an authority
---------------------------------------
`discover_devices` stays in every specialist's tool list and the prompt says the
brief may be incomplete. Three reasons that matters:

  - Room/category matching is heuristic. When it guesses wrong, the specialist
    must be able to look for itself rather than confidently act on a partial view.
  - The brief carries no live STATE — no power, no current brightness. It says
    what exists and what it accepts, not what it is doing. A specialist that
    needs state still calls `query_device_state`.
  - It is generated from the same `shared/device-catalog.json` the validating
    Lambda uses, so it cannot describe a device the system would then refuse to
    command. But a catalog edit deployed to one side first would make it stale,
    and a stale hint the specialist can override is survivable where a stale
    authority is not.
"""

from __future__ import annotations

import re

try:  # pragma: no cover - import shape differs between container and repo
    from . import device_catalog
except ImportError:  # pragma: no cover
    import device_catalog  # type: ignore

# Room aliases as a user says them. The catalog's keys are terse (`living`), and
# a request says "living room" or 「客厅」. Both languages because the chatbot is
# bilingual and a Chinese request is not an edge case here.
ROOM_WORDS: dict[str, tuple[str, ...]] = {
    "living": ("living room", "livingroom", "living", "lounge", "客厅", "起居室"),
    "bedroom": ("bedroom", "bed room", "卧室", "睡房"),
    "kitchen": ("kitchen", "厨房"),
}

# Device categories as a user refers to them, mapped to catalog `deviceType`s.
# Coarser than deviceType on purpose: "the lights" should reach the strip, the TV
# backlight and the matrix, which are three separate types.
#
# Every value here must be a deviceType that EXISTS in device-catalog.json.
# `test_device_brief.py` asserts that against the catalog, because a plausible
# invented name ("light_bulb" for what the catalog calls "light",
# "environment_sensor" for "sensor") matches nothing and produces no brief — and
# no brief is the module's own success path, so the mistake looks like correct
# behaviour. Both of those were in the first version of this table.
CATEGORY_TYPES: dict[str, tuple[str, ...]] = {
    "light": ("light_strip", "light", "tv_light", "led_matrix"),
    "fan": ("fan",),
    "sensor": ("sensor",),
    "purifier": ("air_purifier",),
    "humidifier": ("humidifier",),
    "oven": ("oven",),
    "cooker": ("rice_cooker",),
    "icemaker": ("ice_maker",),
    "plug": ("plug",),
    "tv": ("tv_light",),
}

CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "light": ("light", "lights", "lamp", "strip", "backlight", "matrix", "bulb",
              "led", "colour", "color", "brightness", "dim", "灯", "灯带", "灯光",
              "背光", "亮度"),
    "fan": ("fan", "breeze", "airflow", "风扇", "电扇"),
    "sensor": ("temperature", "humidity", "sensor", "air quality", "pm2.5",
               "pm25", "co2", "温度", "湿度", "空气", "传感器"),
    "purifier": ("purifier", "air purifier", "filter", "净化器", "滤网"),
    "humidifier": ("humidifier", "mist", "加湿器"),
    "oven": ("oven", "bake", "preheat", "roast", "烤箱", "预热"),
    "cooker": ("rice", "cooker", "电饭煲", "米饭"),
    "icemaker": ("ice", "制冰"),
    "plug": ("plug", "socket", "outlet", "插座"),
    "tv": ("tv", "television", "screen", "film", "movie", "cinema",
           "电视", "屏幕", "电影"),
}

# Capability keys worth stating: the ones whose BOUNDS a specialist cannot guess.
# `power` is omitted deliberately — every writable device has it and "power:
# boolean" on twelve lines is pure noise.
_BOUNDED_CAPS = ("brightness", "speed", "fan_speed", "mist_level", "temperature",
                 "target_humidity", "color_temp", "effect", "effect_speed",
                 "mode", "segments", "ice_size", "bluetooth", "sync_mode")

# Cap on the brief's size. A request naming no room and no category ("sort out my
# whole house") would otherwise match everything and reproduce the full payload
# this module exists to avoid. When the cap bites the brief is omitted entirely
# rather than truncated: half a device list presented as the room's contents is
# worse than none, because the specialist would not know to look further.
MAX_DEVICES = 6

# ASCII punctuation only. `[^a-z0-9]` looked equivalent and is not: it deletes
# every CJK character, so a Chinese request matched nothing at all and silently
# got no brief. The chatbot is bilingual, so that is half the traffic, not an edge
# case. Unicode letters and digits are kept; the rest becomes a space.
_PUNCT = re.compile(r"[^\w.+]+", re.UNICODE)


def _tokens(text: str) -> str:
    """Lowercased text with punctuation flattened, for substring matching.

    Kept as a string rather than a set of words: several match phrases contain
    spaces ("air quality", "living room"), and CJK has no spaces to split on in
    the first place.
    """
    return " " + _PUNCT.sub(" ", (text or "").lower()) + " "


def _matches(haystack: str, needles: tuple[str, ...]) -> bool:
    for needle in needles:
        if " " in needle or not needle.isascii():
            # Multi-word and CJK needles: plain substring.
            if needle in haystack:
                return True
        elif f" {needle} " in haystack:
            # Single ASCII words on boundaries, so "led" does not match "called"
            # and "ice" does not match "device" — which it did before this split.
            return True
    return False


def rooms_in(text: str) -> list[str]:
    """Catalog room keys the text refers to, in catalog order."""
    hay = _tokens(text)
    return [room for room, words in ROOM_WORDS.items() if _matches(hay, words)]


def categories_in(text: str) -> list[str]:
    """Device categories the text refers to."""
    hay = _tokens(text)
    return [cat for cat, words in CATEGORY_WORDS.items() if _matches(hay, words)]


def _cap_summary(capabilities: dict) -> str:
    """The capability bounds worth stating, as one compact clause."""
    parts = []
    for name in _BOUNDED_CAPS:
        cap = capabilities.get(name)
        if not isinstance(cap, dict):
            continue
        if "min" in cap and "max" in cap:
            unit = cap.get("unit", "")
            parts.append(f"{name} {cap['min']}-{cap['max']}{unit}")
        elif cap.get("values"):
            parts.append(f"{name} " + "/".join(str(v) for v in cap["values"]))
        elif cap.get("count") is not None:
            parts.append(f"{name} x{cap['count']}")
        elif cap.get("type"):
            parts.append(name)
    return ", ".join(parts)


def relevant_devices(request: str) -> list[dict]:
    """Catalog devices this request plausibly concerns.

    Both filters narrow when present and are ignored when absent, so "dim the
    bedroom" gives the bedroom's devices and "dim the lights" gives every light.
    A request naming neither returns [] rather than everything — see MAX_DEVICES.
    """
    rooms = rooms_in(request)
    cats = categories_in(request)
    if not rooms and not cats:
        return []

    wanted_types: set[str] = set()
    for cat in cats:
        wanted_types.update(CATEGORY_TYPES.get(cat, ()))

    out = []
    for device in device_catalog.devices():
        if rooms and device.get("room") not in rooms:
            continue
        if wanted_types and device.get("deviceType") not in wanted_types:
            continue
        out.append(device)
    return out


def device_brief(request: str, max_devices: int = MAX_DEVICES) -> str:
    """One line per relevant device, or "" when a brief would not help.

    "" for three distinct reasons, all of which mean "let the specialist
    discover for itself":
      - the request names no room and no device category;
      - nothing in the catalog matched;
      - too many devices matched, so the brief would be as big as the full
        payload it exists to replace.
    """
    matched = relevant_devices(request)
    if not matched or len(matched) > max_devices:
        return ""

    rooms = device_catalog.load_catalog().get("rooms", {})
    lines = []
    for device in matched:
        room_key = device.get("room") or ""
        room_name = (rooms.get(room_key, {}) or {}).get("en", room_key)
        caps = _cap_summary(device.get("capabilities") or {})
        flag = " [read-only]" if device_catalog.is_readonly(device) else ""
        line = (f"- {device['deviceId']} — {device_catalog.label(device)} "
                f"({room_name}){flag}")
        if caps:
            line += f": {caps}"
        lines.append(line)
    return "\n".join(lines)


def delegation_context(request: str, max_devices: int = MAX_DEVICES) -> str:
    """The brief plus the framing a specialist needs, or "".

    The framing is what keeps this a hint. Without "may be incomplete", a
    specialist treats the list as the room's full contents and never calls
    `discover_devices` even when the guess was wrong; without "no live state" it
    reports brightness it was never told.
    """
    brief = device_brief(request, max_devices=max_devices)
    if not brief:
        return ""
    return (
        "\n\nDevices already identified for this request (so you need not call "
        "discover_devices):\n"
        f"{brief}\n"
        "This list may be incomplete and carries NO live state — no power, no "
        "current brightness. Call discover_devices if you need a device that is "
        "not listed, or query_device_state if you need to know what a device is "
        "doing right now."
    )
