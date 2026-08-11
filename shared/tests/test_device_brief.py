"""Tests for the device brief the orchestrator attaches to each delegation.

The brief exists to remove a specialist's opening `discover_devices` cycle, which
measured at 1.0-1.3s of an LLM turn whose only output was that single call. Three
things decide whether it actually helps:

  - It must name the RIGHT devices. Every `deviceType` in CATEGORY_TYPES has to
    exist in the catalog; a plausible invented name matches nothing, produces no
    brief, and the specialist falls back to discovering — which is the module's own
    success path, so the mistake looks like correct behaviour. Two invented names
    ("light_bulb", "environment_sensor") were in the first draft.
  - It must stay SMALL. The full discovery payload is ~1,800 tokens. Pasting that
    in would move the cost from a round trip into the prompt of every delegated
    request rather than removing it — the exact trap the spec named.
  - It must be a HINT. No live state, and the specialist keeps `discover_devices`.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_brief as db  # noqa: E402
import device_catalog as dc  # noqa: E402


# ---------------------------------------------------------------------------
# The tables must agree with the catalog
# ---------------------------------------------------------------------------

def test_every_mapped_device_type_exists_in_the_catalog():
    """The regression guard for the bug that shipped in the first draft.

    `light_bulb` and `environment_sensor` are entirely reasonable names for what
    this catalog calls `light` and `sensor`. Matching nothing is silent: the brief
    comes back empty, the specialist discovers as before, and the only symptom is
    an optimisation that quietly does nothing.
    """
    real = {d["deviceType"] for d in dc.devices()}
    mapped = {t for types in db.CATEGORY_TYPES.values() for t in types}
    assert mapped <= real, f"not in the catalog: {sorted(mapped - real)}"


def test_every_catalog_device_type_is_reachable():
    """No device may be unreachable by any category.

    Otherwise a request about it silently gets no brief, and nobody finds out
    except by noticing the latency never improved for that device.
    """
    real = {d["deviceType"] for d in dc.devices()}
    mapped = {t for types in db.CATEGORY_TYPES.values() for t in types}
    assert real <= mapped, f"no category reaches: {sorted(real - mapped)}"


def test_every_category_has_words_and_types():
    assert set(db.CATEGORY_TYPES) == set(db.CATEGORY_WORDS)


def test_room_keys_are_catalog_rooms():
    assert set(db.ROOM_WORDS) <= set(dc.load_catalog().get("rooms", {}))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Give the living room light strip a calm ocean feel", ["living"]),
    ("dim the bedroom", ["bedroom"]),
    ("客厅的灯带调成海洋效果", ["living"]),
    ("卧室的灯", ["bedroom"]),
    ("what is the temperature", []),
])
def test_rooms_in(text, expected):
    assert db.rooms_in(text) == expected


@pytest.mark.parametrize("text,expected_category", [
    ("make the lights cosy", "light"),
    ("turn on the fan", "fan"),
    ("what is the humidity", "sensor"),
    ("replace the air purifier filter", "purifier"),
    ("preheat the oven", "oven"),
    ("start the rice", "cooker"),
    ("客厅的灯光", "light"),
])
def test_categories_in(text, expected_category):
    assert expected_category in db.categories_in(text)


def test_single_words_match_on_boundaries_only():
    """`led` must not match "called", `ice` must not match "device".

    Substring matching for every word looked simpler and is wrong: "which device
    is it" would have matched the ice maker, putting an irrelevant appliance into a
    lighting brief.
    """
    assert "icemaker" not in db.categories_in("which device did I mean")
    assert "light" not in db.categories_in("the agent called it that")


def test_multiword_and_cjk_still_match_as_substrings():
    assert "sensor" in db.categories_in("how is the air quality")
    assert "light" in db.categories_in("把灯带调暗")


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

def test_room_and_category_both_narrow():
    ids = [d["deviceId"] for d in db.relevant_devices(
        "Give the living room light strip a calm ocean feel")]
    assert "living-strip-1" in ids
    # The kitchen appliances and the fan are in neither the room nor the category.
    assert not any(i.startswith("kitchen-") for i in ids)
    assert "living-fan-1" not in ids


def test_a_room_alone_gives_that_room():
    ids = [d["deviceId"] for d in db.relevant_devices("sort out the bedroom")]
    assert set(ids) == {"bedroom-light-1", "bedroom-humidifier-1"}


def test_a_category_alone_spans_rooms():
    ids = [d["deviceId"] for d in db.relevant_devices("turn the lights down")]
    assert "bedroom-light-1" in ids
    assert "living-strip-1" in ids


def test_neither_room_nor_category_matches_nothing():
    # Not "everything": a whole-house request would otherwise reproduce the full
    # payload this module exists to avoid.
    assert db.relevant_devices("sort out my whole house") == []


def test_a_film_request_includes_the_tv_backlight():
    """The device the request is actually about.

    "watching a film … follow the TV" is the scene-sync path, and the TV backlight
    is the only device with `sync_mode`. An earlier version of the category table
    missed it, which would have handed the sync specialist a brief without its
    sync master.
    """
    ids = [d["deviceId"] for d in db.relevant_devices(
        "I'm watching a film in the living room, make the lights follow the TV")]
    assert "living-tvlight-1" in ids


# ---------------------------------------------------------------------------
# Size — the whole point
# ---------------------------------------------------------------------------

def test_the_brief_is_far_smaller_than_the_full_payload():
    full = len(json.dumps(dc.discovery_payload()))
    brief = len(db.delegation_context(
        "Give the living room light strip a calm ocean feel"))
    assert brief * 4 < full, (
        f"brief {brief} chars vs full payload {full}: not a saving worth the "
        f"round trip it replaces")


def test_too_many_matches_yields_no_brief_rather_than_a_truncated_one():
    """Half a device list presented as the room's contents is worse than none.

    A truncated brief looks complete, so the specialist would not know to call
    `discover_devices` for the devices that were dropped.
    """
    assert db.device_brief("turn the lights down", max_devices=1) == ""


def test_capability_bounds_are_stated_compactly():
    brief = db.device_brief("dim the bedroom")
    assert "bedroom-light-1" in brief
    assert "brightness 0-100" in brief
    # One line per device, not a JSON schema.
    assert brief.count("\n") == 0
    assert "{" not in brief


def test_power_is_not_listed():
    # Every writable device has it; twelve lines of "power: boolean" is noise.
    assert "power" not in db.device_brief("dim the bedroom")


def test_enum_values_are_listed_because_they_cannot_be_guessed():
    brief = db.device_brief("Give the living room light strip a calm ocean feel")
    # "There is no ocean effect on a light strip; there is wave" — the specialist
    # can only know that from the enum.
    assert "wave" in brief
    assert "segments x30" in brief


def test_read_only_devices_are_flagged():
    brief = db.device_brief("what is the temperature in the living room")
    assert "living-sensor-1" in brief
    assert "read-only" in brief


# ---------------------------------------------------------------------------
# It must remain a hint
# ---------------------------------------------------------------------------

def test_the_context_says_the_list_may_be_incomplete():
    ctx = db.delegation_context("dim the bedroom")
    assert "may be incomplete" in ctx
    assert "discover_devices" in ctx


def test_the_context_says_it_carries_no_live_state():
    """Otherwise a specialist reports a brightness it was never told.

    The brief is generated from the static catalog and knows nothing about what is
    currently on.
    """
    ctx = db.delegation_context("dim the bedroom")
    assert "NO live state" in ctx
    assert "query_device_state" in ctx


def test_no_brief_means_an_empty_context_not_a_stub():
    # "" so the delegated message is byte-for-byte what the model composed.
    assert db.delegation_context("sort out my whole house") == ""
