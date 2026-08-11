"""Tests for the per-user timezone and coordinates on the `__settings__` row.

These three fields decide when an unattended automation runs, which is why they
are validated at write time rather than at use time. A bad timezone accepted here
surfaces much later as a schedule Scheduler refuses to create — reported nowhere
the admin who typed it is looking. And half a location (a latitude with no
longitude) computes a sunrise for a place that does not exist, so it is rejected
rather than partially stored.
"""

from decimal import Decimal

import index


def test_a_valid_iana_timezone_is_accepted():
    out, err = index._validate_location({"timezone": "Asia/Shanghai"}, {})
    assert err == ""
    assert out["timezone"] == "Asia/Shanghai"


def test_an_unknown_timezone_is_refused_with_an_actionable_message():
    out, err = index._validate_location({"timezone": "Asia/Shangai"}, {})
    assert out == {}
    # The message has to name the format, because the common mistake is a
    # plausible misspelling rather than a wrong idea.
    assert "Asia/Shanghai" in err or "IANA" in err


def test_a_posix_style_offset_is_refused():
    """`UTC+8` looks right and is not an IANA name. Accepting it would create a
    schedule that never observes DST and drifts for half the year."""
    _, err = index._validate_location({"timezone": "UTC+8"}, {})
    assert err


def test_an_empty_timezone_clears_it_rather_than_failing():
    out, err = index._validate_location({"timezone": ""}, {"timezone": "Asia/Tokyo"})
    assert err == ""
    assert out["timezone"] == ""


def test_an_absent_timezone_keeps_the_stored_one():
    out, err = index._validate_location({}, {"timezone": "Europe/London"})
    assert err == ""
    assert out["timezone"] == "Europe/London"


def test_coordinates_are_stored_as_decimal():
    """DynamoDB rejects a Python float outright — see shared/scenarios._storable
    for the same lesson learned the hard way."""
    out, err = index._validate_location(
        {"latitude": 31.23, "longitude": 121.47}, {})
    assert err == ""
    assert isinstance(out["latitude"], Decimal)
    assert isinstance(out["longitude"], Decimal)
    assert float(out["latitude"]) == 31.23


def test_a_string_coordinate_from_a_form_field_is_accepted():
    out, err = index._validate_location(
        {"latitude": "31.23", "longitude": "-121.47"}, {})
    assert err == ""
    assert float(out["longitude"]) == -121.47


def test_out_of_range_coordinates_are_refused():
    _, err = index._validate_location({"latitude": 91, "longitude": 0}, {})
    assert "latitude" in err
    _, err = index._validate_location({"latitude": 0, "longitude": 181}, {})
    assert "longitude" in err


def test_a_non_numeric_coordinate_is_refused():
    _, err = index._validate_location({"latitude": "north", "longitude": 0}, {})
    assert err


def test_one_coordinate_without_the_other_is_refused():
    _, err = index._validate_location({"latitude": 31.23}, {})
    assert "together" in err
    _, err = index._validate_location({"longitude": 121.47}, {})
    assert "together" in err


def test_both_coordinates_can_be_cleared_together():
    existing = {"latitude": Decimal("31.23"), "longitude": Decimal("121.47")}
    out, err = index._validate_location(
        {"latitude": None, "longitude": None}, existing)
    assert err == ""
    assert out["latitude"] is None and out["longitude"] is None


def test_clearing_only_one_coordinate_is_refused():
    existing = {"latitude": Decimal("31.23"), "longitude": Decimal("121.47")}
    _, err = index._validate_location({"latitude": ""}, existing)
    assert "together" in err


def test_a_timezone_only_edit_leaves_stored_coordinates_alone():
    existing = {"latitude": Decimal("31.23"), "longitude": Decimal("121.47")}
    out, err = index._validate_location({"timezone": "Asia/Shanghai"}, existing)
    assert err == ""
    assert out["latitude"] == Decimal("31.23")
    assert out["longitude"] == Decimal("121.47")


def test_a_stored_decimal_survives_the_round_trip_to_json():
    """`Decimal` is not JSON-serialisable, so GET has to convert. Returning the
    raw value would make the whole settings response a 500."""
    assert index._coord_out(Decimal("31.23")) == 31.23
    assert index._coord_out(None) is None
    assert index._coord_out("") is None


def test_settings_handlers_unquote_the_path_userid():
    """An email in a URL path arrives percent-encoded.

    Writing `admin%40smarthome.local` as the partition key produces a row the
    AGENT can never find, because the agent reads by plain email. This went
    unnoticed while settings held only `modelId` — the console wrote and read the
    same escaped key, so it was self-consistent. The coordinates broke that
    symmetry: they are written by the console and read by the agent.

    Asserted on the source because the handlers close over a module-level table
    and a behavioural test here would need the whole boto3 resource mocked for one
    line of string handling.
    """
    import inspect

    for handler in (index.get_settings, index.update_settings):
        src = inspect.getsource(handler)
        assert "unquote(path_params.get(\"userId\"" in src, handler.__name__
