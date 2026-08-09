"""Tests for the agent roster — the single source of truth for names and slugs.

The roster used to be copy-pasted into four scripts. These tests pin the derived
maps and the two slug constraints that were measured against the real CLI and
service, so a roster addition that would fail at deploy time fails here instead.
"""
import pytest

from common import agents


def test_derived_maps_agree_with_the_roster():
    assert set(agents.AGENT_NAMES) == set(agents.AGENTS)
    for name, (long_name, slug) in agents.AGENTS.items():
        assert agents.AGENT_LONG_NAMES[name] == long_name
        assert agents.AGENT_SHORT_SLUG[name] == slug
        assert agents.LONG_NAME_TO_AGENT[long_name] == name


def test_the_three_deployed_agents_keep_their_existing_identifiers():
    """These are live: the slug names the CFN stack and runtime, and the long name
    is the Registry record. Changing either orphans deployed infrastructure."""
    assert agents.AGENT_SHORT_SLUG == {
        "energy-optimization": "sha2aenergy",
        "home-security": "sha2asecurity",
        "appliance-maintenance": "sha2amaintenance",
    }
    assert agents.AGENT_LONG_NAMES == {
        "energy-optimization": "energy-optimization-agent",
        "home-security": "home-security-agent",
        "appliance-maintenance": "appliance-maintenance-agent",
    }


def test_current_roster_validates():
    agents.validate_roster()


def test_a_hyphenated_slug_is_rejected():
    """`agentcore create --name` takes alphanumerics only."""
    original = dict(agents.AGENTS)
    agents.AGENTS["bad"] = ("bad-agent", "sha2a-bad")
    try:
        with pytest.raises(ValueError, match="alphanumeric"):
            agents.validate_roster()
    finally:
        agents.AGENTS.clear()
        agents.AGENTS.update(original)


def test_an_overlong_slug_is_rejected():
    """The runtime name is <slug>_<slug>-<10 random> and the service caps it at
    48, so a 19-character slug produces a 49-character name."""
    original = dict(agents.AGENTS)
    agents.AGENTS["bad"] = ("bad-agent", "s" * 19)
    try:
        with pytest.raises(ValueError, match="limit is 18"):
            agents.validate_roster()
    finally:
        agents.AGENTS.clear()
        agents.AGENTS.update(original)


def test_a_slug_starting_with_a_digit_is_rejected():
    original = dict(agents.AGENTS)
    agents.AGENTS["bad"] = ("bad-agent", "2sha2abad")
    try:
        with pytest.raises(ValueError, match="start with a letter"):
            agents.validate_roster()
    finally:
        agents.AGENTS.clear()
        agents.AGENTS.update(original)


def test_duplicate_slugs_are_rejected():
    original = dict(agents.AGENTS)
    agents.AGENTS["clone"] = ("clone-agent", "sha2aenergy")
    try:
        with pytest.raises(ValueError, match="duplicate slug"):
            agents.validate_roster()
    finally:
        agents.AGENTS.clear()
        agents.AGENTS.update(original)


def test_duplicate_long_names_are_rejected():
    original = dict(agents.AGENTS)
    agents.AGENTS["clone"] = ("energy-optimization-agent", "sha2aclone")
    try:
        with pytest.raises(ValueError, match="duplicate long name"):
            agents.validate_roster()
    finally:
        agents.AGENTS.clear()
        agents.AGENTS.update(original)


def test_every_slug_fits_the_runtime_name_cap():
    for agent, slug in agents.AGENT_SHORT_SLUG.items():
        runtime_name_len = len(slug) * 2 + 1 + 11  # <slug>_<slug>-<10 random>
        assert runtime_name_len <= 48, f"{agent}: {runtime_name_len}"


def test_the_scripts_no_longer_define_their_own_copies():
    """The whole point of this module — a second copy silently diverges."""
    import os
    registry_dir = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(agents.__file__))))
    for script in ("deploy.py", "teardown.py", "demo_reset.py", "smoke_test.py"):
        path = os.path.join(registry_dir, script)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for table in ("AGENT_NAMES", "AGENT_LONG_NAMES", "AGENT_SHORT_SLUG"):
            assert f"{table} = (" not in src and f"{table} = {{" not in src, (
                f"{script} redefines {table} — import it from common.agents")
