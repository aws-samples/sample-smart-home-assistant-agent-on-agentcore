"""Rated power draw per device type, for the energy agent's arithmetic.

Why this file exists. The energy agent used to be a system prompt and nothing
else: it was told to "state the assumptions you used", so every figure it produced
was invented at generation time. Two runs of the same question gave two different
answers, and neither could be checked. A skill could have done that, which is
exactly the criticism — the agent had no reason to be an agent.

The fleet's own catalog (shared/device-catalog.json) carries capabilities, not
watts, because nothing else in the system needed them: the simulator renders state
and the control Lambda validates ranges. So the numbers live here.

These are RATED figures for the simulated fleet, not measurements. They are
representative values for the device class rather than any specific product, and
the agent is required to say so — a fabricated precise answer and a labelled
estimate are very different things to a user deciding whether to rewire a room.

`scales_with` names the capability that moves the draw, so a light at 30%
brightness is not billed at its full rating. Linear scaling between `idle_watts`
and `watts`, which is close enough for lighting and fans and is stated as the
assumption rather than hidden.
"""

from __future__ import annotations

# deviceType -> profile. Keys match `deviceType` in shared/device-catalog.json;
# `_unknown` catches a device type added to the catalog without a profile here,
# which must read as "unknown" rather than silently as zero — a device costing
# nothing is a plausible-looking answer and a wrong one.
PROFILES: dict[str, dict] = {
    "light":        {"watts": 10,   "idle_watts": 0.3, "scales_with": "brightness"},
    "light_strip":  {"watts": 22,   "idle_watts": 0.4, "scales_with": "brightness"},
    "led_matrix":   {"watts": 15,   "idle_watts": 0.4, "scales_with": "brightness"},
    "tv_light":     {"watts": 18,   "idle_watts": 0.4, "scales_with": "brightness"},
    "fan":          {"watts": 45,   "idle_watts": 0.5, "scales_with": "speed"},
    "air_purifier": {"watts": 55,   "idle_watts": 1.0, "scales_with": "fan_speed"},
    "humidifier":   {"watts": 30,   "idle_watts": 0.5, "scales_with": "mist_level"},
    # Duty-cycled appliances. `duty_cycle` is the share of wall-clock time the
    # element actually draws while the device reports itself on — an oven holding
    # 180C is not drawing its full element rating continuously, and treating it as
    # if it were overstates a kitchen by an order of magnitude.
    "oven":         {"watts": 2400, "idle_watts": 1.0, "duty_cycle": 0.35},
    "rice_cooker":  {"watts": 700,  "idle_watts": 0.8, "duty_cycle": 0.45,
                     "keep_warm_watts": 45},
    "ice_maker":    {"watts": 120,  "idle_watts": 1.5, "duty_cycle": 0.60},
    # A plug's own draw is negligible; what matters is whatever is plugged in,
    # which this system genuinely cannot know. Reported as unknown rather than
    # guessed, so the agent asks instead of inventing a load.
    "plug":         {"watts": None, "idle_watts": 0.5, "unknown_load": True},
    "sensor":       {"watts": 0.5,  "idle_watts": 0.5},
}

_UNKNOWN = {"watts": None, "idle_watts": None, "unknown_load": True}

# US retail rates, stated so the agent quotes them rather than reinventing them
# per call. Overridable by the user in the request; the agent is told to say which
# rate it used either way.
RATES = {
    "flat_usd_per_kwh": 0.16,
    "tou_peak_usd_per_kwh": 0.30,
    "tou_offpeak_usd_per_kwh": 0.10,
    "tou_peak_hours": "16:00-21:00 local, weekdays",
}


def profile(device_type: str) -> dict:
    """The profile for one device type. Unknown types report unknown, not zero."""
    return PROFILES.get(device_type, _UNKNOWN)


def as_reference() -> dict:
    """The whole table plus rates, for the agent to read once per request."""
    return {
        "note": ("Rated representative values for the simulated fleet, not "
                 "measurements. Linear scaling between idle_watts and watts for "
                 "devices with scales_with; duty_cycle applies to heating and "
                 "compressor loads. Say that these are estimates."),
        "profiles": PROFILES,
        "rates": RATES,
    }
