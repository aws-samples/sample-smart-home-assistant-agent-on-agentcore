"""Reading a user's `__settings__` row when all you have is their Cognito sub.

The deployment keys the same person two ways, and both are load-bearing:

  - `smarthome-skills` rows (settings, prompts, grants) are keyed by EMAIL,
    because that is what the chatbot sends as `userId` and what the orchestrator
    reads at runtime.
  - `smarthome-scenarios` rows and the per-user scheduling secret are keyed by
    the Cognito SUB, because a scene is written by a sub-agent holding a verified
    idToken and `sub` is the claim that cannot change.

Nothing needed to cross that line until a scene's schedule started depending on
the owner's coordinates: the scenarios table holds a sub, the coordinates live
under an email. Hence this module, shared by the two processes that have to make
the crossing (the admin API's reconcile and the runner's nightly solar pass) so
they cannot resolve it differently and disagree about whether a user has a
location.

The lookup fails SOFT and returns `{}`. A caller must treat that as "no
coordinates", never as a reason to guess a location — a guessed one turns the
lights on at the wrong time in a way nobody thinks to check.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SETTINGS_SORT_KEY = "__settings__"


def _looks_like_email(value: str) -> bool:
    return "@" in value


def email_for_sub(cognito, user_pool_id: str, sub: str) -> str:
    """The email for a Cognito sub, or "" if it cannot be resolved.

    A `sub = "..."` filter, which Cognito supports natively — listing every user
    and scanning would be a full pool walk per lookup.
    """
    if not user_pool_id or not sub:
        return ""
    try:
        resp = cognito.list_users(
            UserPoolId=user_pool_id, Filter=f'sub = "{sub}"', Limit=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not resolve sub %s...: %s", sub[:8], type(exc).__name__)
        return ""
    for user in resp.get("Users", []):
        for attr in user.get("Attributes", []):
            if attr.get("Name") == "email" and attr.get("Value"):
                return attr["Value"]
        # A user with no email attribute still has a username, and that is what
        # the agent would have used as its row key.
        if user.get("Username"):
            return user["Username"]
    return ""


def read_settings(skills_table, user_id: str, cognito=None,
                  user_pool_id: str = "") -> dict:
    """The `__settings__` row for `user_id`, which may be a sub OR an email.

    Tries the value as given first: it is already an email in the common case, and
    that keeps this to one GetItem. Only on a miss, and only when handed a Cognito
    client, does it resolve sub -> email and try again.

    RAISES if the table read itself fails. That is deliberate and the caller must
    keep the distinction: "this user has no coordinates" and "we could not find out"
    look identical here (both are `{}`) and call for opposite actions — the first
    means a solar schedule should not exist, the second means leave the existing one
    alone. A caller that swallowed the difference would delete a working schedule on
    a momentary DynamoDB error and report success.
    """
    if skills_table is None or not user_id:
        return {}

    def _get(key: str) -> dict:
        return skills_table.get_item(
            Key={"userId": key, "skillName": SETTINGS_SORT_KEY}
        ).get("Item") or {}

    item = _get(user_id)
    if item or _looks_like_email(user_id) or cognito is None:
        return item

    # A failure to RESOLVE, unlike a failure to read, is soft: it means we could
    # not turn a sub into an email, and the sub itself already had no row.
    email = email_for_sub(cognito, user_pool_id or os.environ.get(
        "COGNITO_USER_POOL_ID", ""), user_id)
    if not email or email == user_id:
        return {}
    return _get(email)


def coordinates(settings: dict) -> tuple[float, float] | None:
    """(latitude, longitude) as floats, or None when either is absent.

    Stored as Decimal because DynamoDB rejects a Python float. Both or neither:
    one without the other locates nothing, so a half-set pair is treated as unset
    rather than as an error to raise at 05:00.
    """
    lat, lon = settings.get("latitude"), settings.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def timezone_of(settings: dict) -> str:
    """The IANA timezone name, or "" meaning UTC."""
    return str(settings.get("timezone") or "")
