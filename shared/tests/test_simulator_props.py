"""Guards for the simulator's scene props: the virtual clock and the media source.

The simulator has no JS test runner, and these three failure modes are all
invisible to `tsc` and to a glance at the running page:

  - A component that keeps calling `Date.now()` still animates, just against a
    different clock from everything else. At 1x nothing looks wrong, so the bug
    ships and only appears when someone accelerates time in a demo.
  - A `t()` key that does not exist renders as its own name, exactly as it did in
    the admin console.
  - `sync_mode` is a device capability the control Lambda validates against the
    catalog. If the simulator offers a mode the catalog does not declare, the
    button works locally and the agent's equivalent command is rejected — the two
    disagree in the direction that is hardest to notice.
"""

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "device-simulator", "src")
CATALOG = os.path.join(ROOT, "shared", "device-catalog.json")
assert os.path.isdir(SIM), SIM


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _locale_keys(name):
    body = _read(os.path.join(SIM, "i18n", "locales", f"{name}.ts"))
    return set(re.findall(r"""^\s*['"]([A-Za-z0-9_.\-]+)['"]\s*:""", body, re.M))


def _tsx_files():
    for base, _dirs, files in os.walk(SIM):
        if "i18n" in base.split(os.sep):
            continue
        for f in files:
            if f.endswith((".ts", ".tsx")):
                yield os.path.join(base, f)


# ---------------------------------------------------------------------------
# One clock
# ---------------------------------------------------------------------------

# Files allowed to call Date.now() directly. The clock itself must, since it is
# the thing translating real time; publishState stamps a wire timestamp, which
# should be real time because the cloud stores it; MediaSync uses it for animation
# throttling and beat phase, which are real-time concerns by definition.
REAL_TIME_ALLOWED = {
    "devices/virtualClock.ts",
    "state/publishState.ts",
    "components/MediaSync.tsx",
}


def test_time_sensitive_code_reads_the_virtual_clock():
    """A component still on Date.now() drifts away from the rest of the UI as soon
    as the multiplier is anything but 1 — and looks perfectly fine at 1x."""
    offenders = []
    for path in _tsx_files():
        rel = os.path.relpath(path, SIM).replace(os.sep, "/")
        if rel in REAL_TIME_ALLOWED:
            continue
        body = _read(path)
        if "Date.now()" in body or re.search(r"new Date\(\s*\)", body):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} read real time directly; use nowMs()/nowSeconds() from "
        f"devices/virtualClock so a speed change moves every consumer together")


def test_the_sensor_samples_on_virtual_time():
    """The sensor history is what a threshold trigger reads. On a fixed real-time
    interval at 60x, the timestamps jump five virtual hours between samples and the
    series a scene evaluates is almost empty."""
    body = _read(os.path.join(SIM, "components", "SensorDevice.tsx"))
    assert "nowSeconds()" in body
    assert "getMultiplier()" in body, (
        "the sampling interval does not scale with the clock multiplier")


def test_the_clock_re_anchors_when_the_speed_changes():
    """Multiplying elapsed time retroactively would throw the clock years out on
    the first speed change."""
    body = _read(os.path.join(SIM, "devices", "virtualClock.ts"))
    fn = body[body.index("export function setMultiplier"):]
    fn = fn[:fn.index("\n}")]
    assert "virtualAnchorMs = current" in fn
    assert "realAnchorMs = REAL()" in fn


def test_the_clock_never_runs_backwards_by_multiplier():
    body = _read(os.path.join(SIM, "devices", "virtualClock.ts"))
    assert "Math.max(1, next)" in body, (
        "a multiplier below 1 would run the simulation slower than real time, "
        "which no demo wants and which makes the clock lag the schedule")


# ---------------------------------------------------------------------------
# The sync contract
# ---------------------------------------------------------------------------

def _catalog_device(device_id):
    for device in json.load(open(CATALOG, encoding="utf-8"))["devices"]:
        if device["deviceId"] == device_id:
            return device
    raise AssertionError(f"{device_id} not in the catalog")


def test_the_sync_modes_the_panel_offers_are_the_ones_the_catalog_declares():
    """The control Lambda validates `sync_mode` against the catalog, so a mode the
    panel offers but the catalog does not know is a button that works locally and
    a command from the agent that gets rejected."""
    declared = set(_catalog_device("living-tvlight-1")
                   ["capabilities"]["sync_mode"]["values"])
    body = _read(os.path.join(SIM, "components", "MediaSync.tsx"))
    offered = set(re.findall(r"""setModeAndPublish\('(\w+)'\)""", body))
    assert offered, "no sync modes found in MediaSync — did the handler get renamed?"
    assert offered <= declared, (
        f"MediaSync offers {sorted(offered - declared)}, which the catalog does not "
        f"declare for living-tvlight-1 (valid: {sorted(declared)})")


def test_the_backlight_still_declares_the_segments_the_sync_drives():
    """Four edges to four segments. A count change here silently truncates or pads
    the sync."""
    device = _catalog_device("living-tvlight-1")
    assert device["capabilities"]["segments"]["count"] == 4
    body = _read(os.path.join(SIM, "devices", "mediaSource.ts"))
    assert "[string, string, string, string]" in body, (
        "edgeColors no longer returns exactly four colours")


def test_the_panel_finds_its_device_by_capability_not_by_id():
    """A hardcoded device id would crash the app if the device were renamed or
    removed; a capability lookup just hides the panel."""
    body = _read(os.path.join(SIM, "App.tsx"))
    assert "d.capabilities.sync_mode" in body
    assert "living-tvlight-1" not in body


def test_every_mqtt_handler_takes_the_topic_before_the_payload():
    """MessageCallback is `(topic, payload)`. A one-argument handler receives the
    TOPIC STRING and every field read off it is undefined — so an agent command is
    silently ignored while the component's own buttons keep working. Observed live:
    useDeviceState handled the same message correctly and only the media panel
    ignored it, which made it look like a payload-shape problem."""
    client = _read(os.path.join(SIM, "mqtt", "MqttClient.ts"))
    assert "cb(topic, payload)" in client, (
        "the dispatch signature changed; this check no longer describes it")

    offenders = []
    for path in _tsx_files():
        body = _read(path)
        for match in re.finditer(r"\.subscribe\(\s*\w+\s*,\s*(?:\(([^)]*)\)|(\w+))",
                                 body):
            args = match.group(1)
            if args is None:
                continue  # a named handler; its own signature is checked by tsc
            params = [a.strip() for a in args.split(",") if a.strip()]
            if len(params) < 2:
                offenders.append(
                    (os.path.relpath(path, ROOT), args.strip() or "(none)"))
    assert not offenders, (
        f"MQTT handler(s) declaring fewer than two parameters, so the payload is "
        f"actually the topic: {offenders}")


def test_the_media_panel_unsubscribes_on_unmount():
    """A handler left registered after unmount keeps setting state on a dead
    component, and accumulates one stale handler per remount."""
    body = _read(os.path.join(SIM, "components", "MediaSync.tsx"))
    assert "mqtt.unsubscribe(topic, handler)" in body


def test_a_device_name_in_a_translated_string_uses_the_localised_name():
    """`displayName.en` spliced into a Chinese sentence reads as a bug. The catalog
    already has a `displayName(device, language)` helper for this."""
    body = _read(os.path.join(SIM, "components", "MediaSync.tsx"))
    assert "displayName.en" not in body
    assert "displayName(device, language)" in body


def test_a_state_report_is_throttled_rather_than_sent_per_frame():
    """60fps x one publish per frame per device is 60 MQTT messages a second that
    no light can resolve."""
    body = _read(os.path.join(SIM, "components", "MediaSync.tsx"))
    assert "lastPublish" in body
    assert "PUBLISH_INTERVAL_MS" in body


def test_the_publish_cadence_is_slower_than_the_state_debounce():
    """publishState re-arms a 400ms debounce on every report, so a FASTER cadence
    publishes nothing at all: each call cancels the pending flush before it fires.
    Observed live — the on-screen sync looked perfect while the cloud held the
    light's last manual state, with no error and no missing MQTT log line."""
    media = _read(os.path.join(SIM, "components", "MediaSync.tsx"))
    publish_state = _read(os.path.join(SIM, "state", "publishState.ts"))

    cadence = int(re.search(r"const PUBLISH_INTERVAL_MS = (\d+)", media).group(1))
    debounce = int(re.search(r"const DEBOUNCE_MS = (\d+)", publish_state).group(1))
    assert cadence > debounce, (
        f"a {cadence}ms publish cadence against a {debounce}ms debounce starves "
        f"the flush — nothing reaches the cloud")


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------

T_CALL = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.\-]+)['"]\s*\)""")

# Keys built by interpolation, listed because a scan cannot find them.
DYNAMIC = [("media.scene.", ["sunset", "ocean", "forest", "neon", "fireplace"])]


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_every_simulator_key_is_defined(locale):
    keys = _locale_keys(locale)
    used = {}
    for path in _tsx_files():
        for key in T_CALL.findall(_read(path)):
            used.setdefault(key, set()).add(os.path.relpath(path, ROOT))
    missing = {k: sorted(v) for k, v in used.items() if k not in keys}
    assert not missing, (
        f"{locale}.ts is missing {len(missing)} key(s), each of which renders as "
        f"its own name: {missing}")


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_the_interpolated_scene_keys_exist(locale):
    keys = _locale_keys(locale)
    missing = [f"{p}{s}" for p, suffixes in DYNAMIC for s in suffixes
               if f"{p}{s}" not in keys]
    assert not missing, f"{locale}.ts is missing {missing}"


def test_the_scene_list_matches_the_translated_scenes():
    """SCENE_NAMES drives the dropdown; a scene added there without a translation
    renders its key as the option label."""
    body = _read(os.path.join(SIM, "devices", "mediaSource.ts"))
    block = body[body.index("const SCENES:"):]
    block = block[:block.index("};")]
    scenes = set(re.findall(r"^\s+(\w+):", block, re.M))
    listed = {s for _p, suffixes in DYNAMIC for s in suffixes}
    assert scenes == listed, f"mediaSource scenes {sorted(scenes)} != {sorted(listed)}"


def test_the_two_simulator_locales_agree():
    en, zh = _locale_keys("en"), _locale_keys("zh")
    assert not (en - zh), f"defined in en but not zh: {sorted(en - zh)}"
    assert not (zh - en), f"defined in zh but not en: {sorted(zh - en)}"
