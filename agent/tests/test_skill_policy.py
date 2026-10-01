"""Per-user skill policy: which global skills a user does NOT get, and which
built-in tools the effective skill set actually asks for.

Pinned here is the fail-closed direction for the disable list and the
behaviour-preserving direction for built-ins: a skill set that declares a tool
keeps it, a skill set that declares nothing loses it, and an UNKNOWN skill set (the
DynamoDB-failed filesystem fallback) keeps today's behaviour.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import skill_policy


def _skill(name, tools=None):
    return SimpleNamespace(name=name, allowed_tools=tools)


def test_read_disabled_returns_names_from_the_policy_row():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"disabledSkills": ["browser-use", "weather-lookup"]}}
    assert skill_policy.read_disabled(table, "alice@example.com") == {"browser-use", "weather-lookup"}
    table.get_item.assert_called_once_with(
        Key={"userId": "alice@example.com", "skillName": skill_policy.SKILL_POLICY_SK})


def test_read_disabled_is_empty_for_no_row_and_for_placeholder_actors():
    table = MagicMock()
    table.get_item.return_value = {}
    assert skill_policy.read_disabled(table, "alice@example.com") == set()
    for actor in ("", "default", "__global__"):
        assert skill_policy.read_disabled(table, actor) == set()
    table.get_item.assert_called_once()  # placeholders never hit the table


def test_read_disabled_accepts_a_dynamodb_string_set():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"disabledSkills": {"code-interpreter"}}}
    assert skill_policy.read_disabled(table, "u") == {"code-interpreter"}


def test_apply_drops_disabled_skills_and_keeps_the_rest_in_order():
    skills = [_skill("browser-use"), _skill("fan-control"), _skill("weather-lookup")]
    out = skill_policy.apply_disabled(skills, {"browser-use"})
    assert [s.name for s in out] == ["fan-control", "weather-lookup"]


def test_apply_with_nothing_disabled_is_identity():
    skills = [_skill("a"), _skill("b")]
    assert skill_policy.apply_disabled(skills, set()) == skills


def test_builtin_is_wanted_only_when_an_effective_skill_declares_it():
    skills = [_skill("weather-lookup", ["http_request"]), _skill("fan-control", ["control_device"])]
    assert skill_policy.builtin_wanted(skills, "http_request") is True
    assert skill_policy.builtin_wanted(skills, "file_write") is False


def test_builtin_survives_an_unknown_skill_set():
    """`skills is None` is the DynamoDB-failed fallback; an outage must not also
    remove tools that worked yesterday."""
    assert skill_policy.builtin_wanted(None, "http_request") is True


def test_builtin_tolerates_skills_with_no_declaration():
    assert skill_policy.builtin_wanted([_skill("x", None), _skill("y", [])], "http_request") is False
