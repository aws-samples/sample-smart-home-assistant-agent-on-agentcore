"""Sunrise and sunset times, computed rather than fetched.

A pure function of (date, latitude, longitude). No network, no API key, no
failure mode beyond "this place has no sunrise today", which is a real answer
above the Arctic circle and is reported as such.

Why not an external API
-----------------------
A sunrise service would be one more thing that can be down at 05:00, and the
Lambda that recomputes tomorrow's times has no other reason to reach the
internet. The formulas here are the standard NOAA solar position equations and
agree with authoritative tables to within about a minute, which is finer than the
minute-resolution cron they end up in.

Why not approximate it on the sweep
-----------------------------------
The condition sweep runs every five minutes, so "is it sunset yet" would be
±5 minutes — visibly worse than the `time` trigger's to-the-minute accuracy, for
a trigger users would reasonably expect to be the more precise of the two.

Accuracy and its limits, stated plainly:

  - Times are computed for the centre of the solar disc crossing the standard
    -0.833 degree horizon (refraction plus the sun's radius), the same convention
    as published tables.
  - Accurate to roughly a minute at mid latitudes. The error grows near the poles,
    where the sun crosses the horizon at a shallow angle and a minute of time is a
    tiny change in altitude.
  - Above about 66 degrees the sun may not rise or set at all on a given date. Then
    there is no answer, and `None` is returned rather than a fabricated time.

Everything is UTC. Local time is the caller's problem, because the caller is the
one holding the user's timezone (see cdk/lambda/admin-api/scenario_schedules.py).
"""

from __future__ import annotations

import datetime as _dt
import math

# The solar elevation counted as sunrise/sunset: the sun's upper limb on the
# horizon, allowing for atmospheric refraction. Published tables use this, so
# using anything else here would make results look wrong against every reference
# a user might check.
HORIZON_DEGREES = -0.833

SUNRISE = "sunrise"
SUNSET = "sunset"
EVENTS = (SUNRISE, SUNSET)


class SolarError(ValueError):
    """A request that cannot be answered, with a reason meant for the model."""


def _julian_day(date: _dt.date) -> float:
    """The Julian day number at 00:00 UTC on `date`."""
    y, m, d = date.year, date.month, date.day
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5)


def _solar_noon_and_hour_angle(date: _dt.date, latitude: float,
                               longitude: float) -> tuple[float, float | None]:
    """(solar noon in UTC hours, hour angle in degrees) for `date`.

    The hour angle is None when the sun does not cross the horizon that day —
    polar day or polar night. Everything else here is the NOAA sequence:
    Julian century, geometric mean longitude and anomaly, equation of the centre,
    apparent longitude, obliquity, declination, equation of time.
    """
    jd = _julian_day(date)
    # Evaluated at local solar noon rather than 00:00: the declination moves
    # slightly through the day, and noon is the moment the result is centred on.
    t = (jd + 0.5 - longitude / 360.0 - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(mean_anom)
    centre = (math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
              + math.sin(3 * m_rad) * 0.000289)
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * t
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    obliquity = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))))
                         / 60.0) / 60.0)
    obliquity_corrected = obliquity + 0.00256 * math.cos(math.radians(omega))

    declination = math.degrees(math.asin(
        math.sin(math.radians(obliquity_corrected))
        * math.sin(math.radians(apparent_long))))

    var_y = math.tan(math.radians(obliquity_corrected / 2.0)) ** 2
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2.0 * eccentricity * math.sin(m_rad)
        + 4.0 * eccentricity * var_y * math.sin(m_rad)
        * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m_rad))

    solar_noon_utc = (720.0 - 4.0 * longitude - eq_time) / 60.0

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    cos_ha = ((math.cos(math.radians(90.0 - HORIZON_DEGREES))
               / (math.cos(lat_rad) * math.cos(dec_rad)))
              - math.tan(lat_rad) * math.tan(dec_rad))
    if cos_ha > 1.0 or cos_ha < -1.0:
        # Not a numerical wobble: the sun genuinely never reaches (polar night) or
        # never leaves (polar day) the horizon here today.
        return solar_noon_utc, None
    return solar_noon_utc, math.degrees(math.acos(cos_ha))


def solar_event_utc(event: str, date: _dt.date, latitude: float,
                    longitude: float) -> _dt.datetime | None:
    """The UTC datetime of `event` on `date` at that place, or None.

    None means the event does not occur there that day. Raises SolarError on
    arguments that cannot describe a place or an event, because those are caller
    bugs rather than facts about the sky.
    """
    if event not in EVENTS:
        raise SolarError(f"event must be one of {list(EVENTS)}; got {event!r}")
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise SolarError("latitude and longitude must be numbers") from None
    if not -90.0 <= latitude <= 90.0:
        raise SolarError(f"latitude must be between -90 and 90; got {latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise SolarError(f"longitude must be between -180 and 180; got {longitude}")

    solar_noon, hour_angle = _solar_noon_and_hour_angle(date, latitude, longitude)
    if hour_angle is None:
        return None

    offset_hours = hour_angle * 4.0 / 60.0
    when = solar_noon - offset_hours if event == SUNRISE else solar_noon + offset_hours

    # `when` can fall outside 0-24 near the date line, where local sunrise belongs
    # to the previous or next UTC day. timedelta arithmetic on midnight carries
    # correctly, so no clamping is needed — clamping here would silently move the
    # time to the wrong day.
    midnight = _dt.datetime.combine(date, _dt.time(0, 0), tzinfo=_dt.timezone.utc)
    return midnight + _dt.timedelta(hours=when)


def sunrise_utc(date: _dt.date, latitude: float,
                longitude: float) -> _dt.datetime | None:
    return solar_event_utc(SUNRISE, date, latitude, longitude)


def sunset_utc(date: _dt.date, latitude: float,
               longitude: float) -> _dt.datetime | None:
    return solar_event_utc(SUNSET, date, latitude, longitude)


def next_occurrence_utc(event: str, latitude: float, longitude: float,
                        offset_minutes: int = 0,
                        now: _dt.datetime | None = None,
                        search_days: int = 200) -> _dt.datetime | None:
    """The next time `event` (plus offset) happens after `now`, in UTC.

    `now` must be timezone-aware; it is a parameter rather than a call to
    `datetime.now()` so this stays a pure function and the tests can pin a date.

    Searches forward day by day. `search_days` bounds it: at high latitudes the
    next sunrise can be months away, and beyond that limit there is genuinely no
    answer to give rather than a loop to run for longer. Returns None then, and the
    caller must say so rather than substituting a time.
    """
    if now is None:
        raise SolarError("now is required — pass an aware datetime")
    if now.tzinfo is None:
        raise SolarError("now must be timezone-aware")
    now = now.astimezone(_dt.timezone.utc)
    delta = _dt.timedelta(minutes=int(offset_minutes))

    # Starts a day early: an event late on the previous UTC day can still be in the
    # future once a negative offset is applied.
    day = (now - _dt.timedelta(days=1)).date()
    for _ in range(search_days + 1):
        base = solar_event_utc(event, day, latitude, longitude)
        if base is not None:
            candidate = base + delta
            if candidate > now:
                return candidate
        day += _dt.timedelta(days=1)
    return None


def cron_for_utc(when: _dt.datetime) -> str:
    """A one-shot-shaped daily cron at `when`'s UTC minute.

    `cron(m H * * ? *)` in six fields with `?` in day-of-week, which is what
    EventBridge Scheduler requires; a five-field Unix expression is rejected.
    Recomputed daily by the runner, so the date part is deliberately absent —
    an `at()` expression would need deleting and recreating rather than updating.

    The schedule is created with `ScheduleExpressionTimezone="UTC"` regardless of
    the owner's zone, because a solar time is already an absolute instant. Handing
    Scheduler a local zone here would apply the offset twice.
    """
    when = when.astimezone(_dt.timezone.utc)
    return f"cron({when.minute} {when.hour} * * ? *)"
