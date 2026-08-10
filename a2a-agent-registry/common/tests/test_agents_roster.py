"""Tests for the agent roster — the single source of truth for names and slugs.

The roster used to be copy-pasted into four scripts. These tests pin the derived
maps and the two slug constraints that were measured against the real CLI and
service, so a roster addition that would fail at deploy time fails here instead.
"""
import os

import pytest

from common import agents

# a2a-agent-registry/ — two levels up from common/agents.py, not three. The
# earlier three-level version pointed at the repo root, where none of the agent
# directories exist; the "scripts no longer define their own copies" check passed
# only because it skips paths that are absent.
REGISTRY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(agents.__file__)))


def test_derived_maps_agree_with_the_roster():
    assert set(agents.AGENT_NAMES) == set(agents.AGENTS)
    for name, (long_name, slug) in agents.AGENTS.items():
        assert agents.AGENT_LONG_NAMES[name] == long_name
        assert agents.AGENT_SHORT_SLUG[name] == slug
        assert agents.LONG_NAME_TO_AGENT[long_name] == name


def test_deployed_agents_keep_their_existing_identifiers():
    """These are live: the slug names the CFN stack and runtime, and the long name
    is the Registry record. Changing either orphans deployed infrastructure.

    Subset checks, not equality — adding an agent is expected, renaming a deployed
    one is not.
    """
    for name, slug, long_name in (
        ("energy-optimization", "sha2aenergy", "energy-optimization-agent"),
        ("home-security", "sha2asecurity", "home-security-agent"),
        ("appliance-maintenance", "sha2amaintenance", "appliance-maintenance-agent"),
        ("device-control", "sha2adevice", "device-control-agent"),
    ):
        assert agents.AGENT_SHORT_SLUG[name] == slug
        assert agents.AGENT_LONG_NAMES[name] == long_name


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


def test_every_agent_has_the_files_deploy_expects():
    """deploy.py loads system_prompt.md and card.json by path and decides on the
    tools path by whether tools.py exists — a missing file fails at render."""
    import json
    for name in agents.AGENT_NAMES:
        agent_dir = os.path.join(REGISTRY_DIR, name)
        assert os.path.isdir(agent_dir), name
        for required in ("card.json", "system_prompt.md"):
            assert os.path.exists(os.path.join(agent_dir, required)), f"{name}/{required}"
        card = json.load(open(os.path.join(agent_dir, "card.json"), encoding="utf-8"))
        assert card["name"] == agents.AGENT_LONG_NAMES[name], (
            f"{name}/card.json name must match the roster's long name — it is the "
            f"Registry record name and the AgentCard name")
        assert card.get("skills"), f"{name} publishes no skills, so nothing can be granted"
        for skill in card["skills"]:
            assert skill.get("id"), f"{name} has a skill with no id"


def test_the_tool_using_agents_are_the_expected_ones():
    """deploy.py picks the tools path by the presence of tools.py, and a
    tool-using agent additionally requires a verified user identity on every
    request. Which agents cross that line is worth pinning: adding tools.py to an
    agent silently changes its auth requirements."""
    with_tools = {
        name for name in agents.AGENT_NAMES
        if os.path.exists(os.path.join(REGISTRY_DIR, name, "tools.py"))
    }
    assert with_tools == {"device-control", "light-effect", "knowledge-qa",
                          "scene-orchestration"}, with_tools
    # The three original advisors stay prompt-only — they touch no user data, and
    # requiring an identity they never had would break them.
    prompt_only = set(agents.AGENT_NAMES) - with_tools
    assert prompt_only == {"energy-optimization", "home-security",
                           "appliance-maintenance"}, prompt_only


def test_every_tools_module_exports_build_tools():
    """deploy.py generates `from <pkg>.tools import build_tools`, so a tools.py
    without that name fails at container start rather than at render."""
    import re
    for name in agents.AGENT_NAMES:
        path = os.path.join(REGISTRY_DIR, name, "tools.py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        assert re.search(r"^def build_tools\(", src, re.M), f"{name}/tools.py"


# Parameter names that would let the model choose whose data a tool acts on.
# Identity is closed over from the verified caller (see common/gateway_tools.py and
# scene-orchestration/tools.py) and must never appear in a model-facing signature.
FORBIDDEN_PARAMS = frozenset({
    "user_id", "userid", "user", "sub", "email", "actor_id", "actorid",
    "authorization", "token", "id_token", "access_token", "principal", "scope",
})


def test_no_tools_module_exposes_identity_to_the_model():
    """A tool signature carrying user_id / sub / email would let the model name
    someone else's scope — or a prompt injection could.

    Matched on parameter NAMES parsed from the AST, not as substrings of the
    signature text. The substring form flagged `subject` (a trigger field: which
    device or metric a scene watches) because it contains "sub", and a false
    positive on a legitimate parameter is how a check like this gets loosened until
    it stops catching the real thing.
    """
    import ast
    for name in agents.AGENT_NAMES:
        path = os.path.join(REGISTRY_DIR, name, "tools.py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorated = any(
                (isinstance(d, ast.Name) and d.id == "strands_tool")
                or (isinstance(d, ast.Attribute) and d.attr == "strands_tool")
                or (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "strands_tool")
                for d in node.decorator_list)
            if not decorated:
                continue
            checked += 1
            args = node.args
            params = [a.arg for a in
                      (args.posonlyargs + args.args + args.kwonlyargs)]
            bad = sorted(set(p.lower() for p in params) & FORBIDDEN_PARAMS)
            assert not bad, (
                f"{name}/tools.py tool {node.name!r} exposes {bad} to the model: "
                f"{params}")
        # A tools.py with no decorated tool means the decorator was renamed and
        # this check silently stopped applying.
        assert checked, f"{name}/tools.py has no @strands_tool functions to check"


def test_the_scripts_no_longer_define_their_own_copies():
    """The whole point of this module — a second copy silently diverges."""
    for script in ("deploy.py", "teardown.py", "demo_reset.py", "smoke_test.py"):
        path = os.path.join(REGISTRY_DIR, script)
        # Assert rather than skip: a wrong path made this vacuous before.
        assert os.path.exists(path), path
        src = open(path, encoding="utf-8").read()
        for table in ("AGENT_NAMES", "AGENT_LONG_NAMES", "AGENT_SHORT_SLUG"):
            assert f"{table} = (" not in src and f"{table} = {{" not in src, (
                f"{script} redefines {table} — import it from common.agents")
