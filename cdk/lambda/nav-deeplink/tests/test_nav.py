"""Tests for nav-deeplink — keyword to DeepLink resolution.

The behaviours worth pinning down: a miss must return the page catalog rather
than a bare error (the agent has to be able to tell the user what exists), the
longest alias must win so "music sync" doesn't lose to a shorter substring, and
no output may carry a brand name — the scheme is deliberately neutral.
"""

import pytest


@pytest.fixture
def mod(lambda_module):
    return lambda_module("nav_deeplink_index")


def test_matches_a_chinese_alias(mod):
    out = mod.handler({"page": "打开群控页面"}, None)
    assert out["matched"] is True
    assert out["page"] == "group_control"
    assert out["deepLink"] == "superapp://group_control"


def test_matches_an_english_alias(mod):
    out = mod.handler({"page": "open the automation page"}, None)
    assert out["matched"] is True
    assert out["page"] == "automation"


def test_exact_page_name_matches(mod):
    out = mod.handler({"page": "video_sync"}, None)
    assert out["page"] == "video_sync"
    assert out["deepLink"] == "superapp://video_sync"


def test_longest_alias_wins(mod):
    """"music sync" contains no other alias, but "lights with music" and
    "music mode" overlap conceptually — the specific one must win over any
    shorter accidental substring."""
    out = mod.handler({"page": "I want lights with music please"}, None)
    assert out["page"] == "music_sync"


def test_unmatched_returns_the_catalog(mod):
    out = mod.handler({"page": "the teleporter page"}, None)
    assert out["matched"] is False
    pages = [p["page"] for p in out["availablePages"]]
    assert "group_control" in pages and "settings" in pages
    assert "reason" in out


def test_empty_query_returns_the_catalog(mod):
    out = mod.handler({}, None)
    assert out["matched"] is False
    assert out["availablePages"]


def test_params_are_appended_and_sorted(mod):
    out = mod.handler({"page": "device_list", "params": {"room": "living", "filter": "on"}}, None)
    assert out["deepLink"] == "superapp://devices?filter=on&room=living"


def test_params_accepts_a_json_string(mod):
    out = mod.handler({"page": "device_list", "params": '{"room": "bedroom"}'}, None)
    assert out["deepLink"] == "superapp://devices?room=bedroom"


def test_malformed_params_are_ignored_not_fatal(mod):
    out = mod.handler({"page": "device_list", "params": "not json"}, None)
    assert out["deepLink"] == "superapp://devices"


def test_no_user_identity_is_required(mod):
    """Navigation is user-independent by design — no `user_id` in the event and
    no identity error, unlike the device Lambdas."""
    out = mod.handler({"page": "settings"}, None)
    assert out["matched"] is True
    assert "error" not in out


def test_every_page_is_reachable_by_its_own_name(mod):
    for page in mod.PAGES:
        out = mod.handler({"page": page["page"]}, None)
        assert out["matched"] is True, page["page"]
        assert out["page"] == page["page"]


def test_the_scheme_carries_no_brand(mod):
    assert mod.SCHEME == "superapp"
    for page in mod.PAGES:
        out = mod.handler({"page": page["page"]}, None)
        assert out["deepLink"].startswith("superapp://")
