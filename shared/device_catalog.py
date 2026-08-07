"""Device catalog loading and command validation, shared by the IoT Lambdas.

`device-catalog.json` is the single source of truth for the simulated fleet
(see the `$comment` block inside it). This module is the Python half of that
contract: iot-control validates against it, iot-discovery serves it, and both
get the file copied in next to them at build time by
scripts/01-install-deps.sh.

Three behaviours here are deliberate departures from the hardcoded table this
replaces (cdk/lambda/iot-control/index.py's DEVICE_COMMANDS):

1. Out-of-range integers are CLAMPED, not rejected. The old code returned an
   error for "set the fan to 20", which left the agent with nothing useful to
   say. Clamping plus a warning is what lets it answer "that fan goes up to 8,
   so I set it to 8" — the behaviour the requirements actually describe.
2. Declared types are ENFORCED. The old table carried a "types" key that
   validate_command never read, so {"action": "setPower", "power": "yes"}
   passed validation and reached the device as a string.
3. Read-only devices reject writes with a hint rather than a bare error, so
   "turn on the thermometer" can be answered by querying instead.
"""

import json
import os

_CATALOG = None

# Colors are the one string-shaped capability, and the frontend parses them with
# slice()/parseInt, so anything but #RRGGBB silently renders as garbage.
_HEX_LEN = 7


def _catalog_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "device-catalog.json")


def load_catalog():
    """Parse and cache the catalog. Raises if it is missing or malformed —
    a Lambda with no catalog cannot validate anything, and failing at import
    is far easier to diagnose than silently accepting every command."""
    global _CATALOG
    if _CATALOG is None:
        with open(_catalog_path(), encoding="utf-8") as fh:
            _CATALOG = json.load(fh)
    return _CATALOG


def devices():
    return load_catalog()["devices"]


def device_by_id(device_id):
    for d in load_catalog()["devices"]:
        if d["deviceId"] == device_id:
            return d
    return None


def device_ids():
    return [d["deviceId"] for d in load_catalog()["devices"]]


def resolve_device(device_id=None, device_type=None):
    """Find a device by id, falling back to the first of a given type.

    The type fallback exists for backward compatibility: skills and the voice
    agent's all-devices loop were written against `device_type` when that was
    also the topic segment. Ambiguous by construction once a type has more than
    one instance, so callers that care about a specific unit must pass deviceId.
    """
    if device_id:
        d = device_by_id(device_id)
        if d:
            return d, None
        return None, f"Unknown device '{device_id}'. Known devices: {', '.join(device_ids())}"
    if device_type:
        matches = [d for d in load_catalog()["devices"] if d["deviceType"] == device_type]
        if not matches:
            known = sorted({d["deviceType"] for d in load_catalog()["devices"]})
            return None, f"Unknown device type '{device_type}'. Known types: {', '.join(known)}"
        return matches[0], None
    return None, "Either device_id or device_type is required"


def is_readonly(device):
    """True for devices that only report (sensors) — they declare no actions."""
    return not device.get("actions")


def validate_command(device, command):
    """Validate and normalise one command against a device's capabilities.

    Returns (ok, normalised_command, warnings). `normalised_command` carries
    clamped values, so callers must forward THAT rather than the input. On
    failure `normalised_command` is None and warnings[0] is the reason.
    """
    warnings = []

    action = command.get("action")
    if not action:
        return False, None, ["Missing 'action' in command"]

    if is_readonly(device):
        metrics = ", ".join(device.get("capabilities", {}).keys())
        return False, None, [
            f"'{device['deviceId']}' is a read-only sensor and accepts no commands. "
            f"Query it instead for: {metrics}"
        ]

    actions = device.get("actions", {})
    if action not in actions:
        return False, None, [
            f"Invalid action '{action}' for {device['deviceId']}. "
            f"Valid: {', '.join(actions.keys())}"
        ]

    spec = actions[action]
    # Side effects the device performs itself: setting a fan speed runs the fan,
    # picking a light effect turns the light on. Seeded before the caller's own
    # parameters so an explicit value still wins, and applied here rather than in
    # each caller so the simulator and the control path agree on what a command
    # means.
    out = dict(spec.get("implies") or {})
    out.update(command)

    for param in spec.get("required", []):
        if param not in command:
            return False, None, [f"Missing required parameter '{param}' for action '{action}'"]

    caps = device.get("capabilities", {})
    allowed = set(spec.get("required", [])) | set(spec.get("optional", []))
    # Wire parameter names usually match the capability they set, but not always
    # — setOscillation carries `enabled`, setEffect carries `colors` for the
    # `segments` capability. `params` states those mappings explicitly; guessing
    # from `writes` silently validated colors against the effect enum.
    param_caps = spec.get("params", {})

    for param in allowed:
        if param not in command:
            continue
        value = command[param]
        cap_name = param_caps.get(param, param)
        cap = caps.get(cap_name)
        if not cap:
            # Every accepted parameter must resolve to a capability, or it would
            # reach the device unvalidated.
            return False, None, [
                f"'{param}' on action '{action}' maps to unknown capability "
                f"'{cap_name}' for {device['deviceId']} (catalog bug)"
            ]
        ok, normalised, warning = _check_value(param, value, cap, device)
        if not ok:
            return False, None, [warning]
        if warning:
            warnings.append(warning)
        out[param] = normalised

    return True, out, warnings


def _check_value(param, value, cap, device):
    """Validate one parameter. Returns (ok, normalised_value, warning_or_None)."""
    kind = cap.get("type")

    if kind == "boolean":
        # Enforced, unlike the old table's unread "types" key. Note bool is a
        # subclass of int in Python, so this must precede any numeric check.
        if not isinstance(value, bool):
            return False, None, f"'{param}' must be true or false, got {value!r}"
        return True, value, None

    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, None, f"'{param}' must be a number, got {value!r}"
        low, high = cap.get("min"), cap.get("max")
        if low is not None and value < low:
            return True, low, (
                f"{label(device)} accepts {param} from {low} to {high}; "
                f"{value} is below the minimum, so it was set to {low}."
            )
        if high is not None and value > high:
            return True, high, (
                f"{label(device)} accepts {param} from {low} to {high}; "
                f"{value} is above the maximum, so it was set to {high}."
            )
        return True, value, None

    if kind == "enum":
        values = cap.get("values", [])
        if value not in values:
            return False, None, (
                f"Invalid value for '{param}': {value!r}. Valid: {', '.join(map(str, values))}"
            )
        return True, value, None

    if kind == "color":
        if not isinstance(value, str) or not value.startswith("#") or len(value) != _HEX_LEN:
            return False, None, f"'{param}' must be a #RRGGBB hex color, got {value!r}"
        try:
            int(value[1:], 16)
        except ValueError:
            return False, None, f"'{param}' is not valid hex: {value!r}"
        return True, value.lower(), None

    if kind == "segments":
        # A per-segment color list for addressable strips. Longer lists are
        # truncated rather than refused: an effect generator that does not know
        # the strip length should still produce something visible.
        if not isinstance(value, list):
            return False, None, f"'{param}' must be a list of #RRGGBB colors"
        count = cap.get("count", len(value))
        if len(value) > count:
            return True, value[:count], (
                f"{label(device)} has {count} segments; the extra "
                f"{len(value) - count} color(s) were dropped."
            )
        return True, value, None

    if kind == "readonly":
        return False, None, f"'{param}' is read-only and cannot be set"

    return True, value, None


def label(device):
    """Human-readable device name, for messages the agent relays to the user."""
    name = device.get("displayName") or {}
    if isinstance(name, dict):
        return name.get("en") or device["deviceId"]
    return name or device["deviceId"]


def discovery_payload():
    """The device list as the agent sees it.

    `deviceType` and `powerOn`/`powerOff` are load-bearing and must stay:
    agent/voice_session.py's turn_on_all_devices reads exactly those fields to
    run its power-on loop, so dropping them would break all-devices voice
    control silently.
    """
    cat = load_catalog()
    rooms = cat.get("rooms", {})
    out = []
    for d in cat["devices"]:
        entry = {
            "deviceId": d["deviceId"],
            "deviceType": d["deviceType"],
            "displayName": label(d),
            "room": d.get("room"),
            "roomName": (rooms.get(d.get("room"), {}) or {}).get("en", d.get("room")),
            "connectivity": d.get("connectivity"),
            "capabilities": d.get("capabilities", {}),
            "actions": list(d.get("actions", {}).keys()),
            "readOnly": is_readonly(d),
        }
        if d.get("powerOn"):
            entry["powerOn"] = d["powerOn"]
        if d.get("powerOff"):
            entry["powerOff"] = d["powerOff"]
        out.append(entry)
    return out
