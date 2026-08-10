"""The delegation prompt must name tools that exist, and not contradict itself.

`A2A_DELEGATION_RULES` is appended to the system prompt whenever a user has A2A
grants, and it routes by naming tools explicitly — `a2a_knowledge_qa_agent_
answer_from_docs` rather than "the documentation specialist". That is what made
routing reliable, and it is also what makes the prompt able to go stale: a skill
renamed in an AgentCard leaves the prompt pointing at a tool that no longer
exists, and the model quietly falls back to answering from its own knowledge. The
reply still looks fine, which is the problem.

The tool names are built by `agent/tools/a2a.py` as
`a2a_{slug(agent name)}_{slug(skill id)}`, so they are derivable from the cards —
which is what these tests do rather than restating them.
"""

import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
AGENT_PY = os.path.join(ROOT, "agent", "agent.py")
A2A_DIR = os.path.join(ROOT, "a2a-agent-registry")
assert os.path.isfile(AGENT_PY), AGENT_PY


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _constant(name):
    """Read a module-level string constant without importing agent.py.

    agent.py imports strands, boto3 and the AgentCore SDK at module scope; these
    tests need one literal out of it.
    """
    import ast

    for node in ast.parse(_read(AGENT_PY)).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in agent/agent.py")


def _slug(value):
    """Mirror of tools/a2a.py `_slug` — kept in sync by the test below.

    The character class keeps `_`, so `inspect_devices` survives intact rather
    than collapsing; getting that wrong makes every derived name subtly different
    from the real one.
    """
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_") or "x"


def _expected_tool_names():
    """Every a2a_* tool name the deployed cards can produce."""
    names = set()
    for entry in sorted(os.listdir(A2A_DIR)):
        card = os.path.join(A2A_DIR, entry, "card.json")
        if not os.path.isfile(card):
            continue
        data = json.loads(_read(card))
        agent_name = data.get("name", "")
        for skill in data.get("skills") or []:
            names.add(f"a2a_{_slug(agent_name)}_{_slug(skill.get('id', ''))}")
    return names


RULES = _constant("A2A_DELEGATION_RULES")
PROMPT = _constant("SYSTEM_PROMPT")
# Tool names the rules mention. A trailing `*` is a deliberate wildcard for a
# family of skills, so it is matched as a prefix.
MENTIONED = set(re.findall(r"\ba2a_[a-z0-9_]+\*?", RULES))


def test_the_slug_helper_matches_the_one_that_builds_the_tool_names():
    """If tools/a2a.py changes how it slugs, every name in the prompt is wrong and
    nothing anywhere would say so."""
    src = _read(os.path.join(ROOT, "agent", "tools", "a2a.py"))
    assert 'tool_name = f"a2a_{_slug(agent_name)}_{_slug(skill.get(\'id\', \'x\'))}"' in src
    assert 'r"[^a-z0-9_]+"' in src, (
        "the slug character class changed; update _slug in this test to match — "
        "note it must keep `_` or multi-word skill ids collapse")


def test_every_tool_the_rules_name_actually_exists():
    """A renamed skill leaves the prompt routing to a tool that is not there, and
    the model falls back to answering from general knowledge."""
    expected = _expected_tool_names()
    unknown = []
    for mention in MENTIONED:
        if mention.endswith("*"):
            prefix = mention[:-1]
            if not any(n.startswith(prefix) for n in expected):
                unknown.append(mention)
        elif mention not in expected:
            unknown.append(mention)
    assert not unknown, (
        f"the delegation rules route to tool(s) no AgentCard produces: "
        f"{sorted(unknown)}. Known: {sorted(expected)}")


def test_every_deployed_specialist_is_routable():
    """A specialist nobody is told to call is a specialist that never gets used.
    Skill-level granularity is deliberate — each card skill becomes its own tool —
    but each AGENT must appear in the routing table at least once."""
    agents = {n.rsplit("_", 1)[0] for n in _expected_tool_names()}
    # Compare on the agent prefix, since a wildcard covers a whole family.
    covered = set()
    for mention in MENTIONED:
        stem = mention.rstrip("*")
        for agent in agents:
            if stem.startswith(agent) or agent.startswith(stem):
                covered.add(agent)
    missing = agents - covered
    assert not missing, (
        f"deployed specialist(s) absent from the routing rules, so the model is "
        f"never told to use them: {sorted(missing)}")


def test_the_rules_override_the_capability_list_explicitly():
    """The capability list and the routing table disagreed about the knowledge
    base: the list claimed query_knowledge_base for documentation questions, which
    is exactly what knowledge-QA exists for. Whichever the model read last won."""
    assert "OVERRIDE" in RULES
    assert "query_knowledge_base" in RULES, (
        "the rules no longer say which of the two paths wins for documentation")


def test_the_fast_path_still_keeps_single_device_actions_local():
    """Delegating a light switch adds seconds for nothing. Measured: the fast
    paths call control_device / query_device_state / query_sensor_history /
    navigate_to_page directly, and this is the instruction that keeps them there."""
    assert "DO IT YOURSELF" in RULES
    for tool in ("control_device", "query_device_state", "query_sensor_history",
                 "discover_devices", "navigate_to_page"):
        assert tool in RULES, f"{tool} is no longer listed as a direct call"


def test_the_rules_tell_the_model_to_refuse_rather_than_improvise():
    """The refusal is the honest answer when a domain has no registered tool, and
    it is what makes an ungranted specialist visible instead of silently
    substituted for."""
    assert "IF NO TOOL MATCHES" in RULES
    assert "general knowledge" in RULES


def test_pending_actions_are_explained_to_the_orchestrator():
    """A saved scene hands back actions for the ORCHESTRATOR to apply, which is
    what keeps a scene-driven command under the same per-user authorisation as a
    typed one. If the prompt does not say so, the model reports the scene as
    already applied and nothing happens."""
    assert "pendingActions" in RULES
    assert "control_device" in RULES


def test_the_capability_list_points_at_the_routing_rules():
    """The list is read first and is where a model looks for what it can do; it
    has to hand off to the routing table rather than answering the question
    itself."""
    assert "a2a_<agent>_<skill>" in PROMPT
    assert "routing rules" in PROMPT.lower()


def test_the_prompt_mirror_is_regenerated_when_the_rules_change():
    """agent_prompt_defaults mirrors SYSTEM_PROMPT for the Admin Console's
    "Revert to Default". test_prompt_defaults_mirror.py enforces equality; this
    just points at it, so a failure there is not mistaken for an unrelated bug."""
    mirror = os.path.join(ROOT, "cdk", "lambda", "admin-api",
                          "agent_prompt_defaults.py")
    assert PROMPT in _read(mirror), (
        "cdk/lambda/admin-api/agent_prompt_defaults.py no longer matches "
        "SYSTEM_PROMPT — the console would offer a prompt the agent never ran")
