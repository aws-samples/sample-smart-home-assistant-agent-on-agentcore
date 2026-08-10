"""The tool-consumer map must match what the agents actually declare.

The Tool Policy page shows, next to each Gateway tool, which agents call it — so
an administrator can see that revoking `control_device` stops the user's chat
commands, their scheduled scenes AND two specialists, rather than discovering that
afterwards.

That map is generated from source (`scripts/gen-tool-consumers.py`) because the
admin Lambda is packaged from its own directory and cannot import the agent tree.
A generated file goes stale silently: the page keeps rendering, just with the
wrong answer to "who breaks". These tests re-derive the map from the same two
sources and compare.
"""

import ast
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
A2A = os.path.join(ROOT, "a2a-agent-registry")
AGENT_PY = os.path.join(ROOT, "agent", "agent.py")

sys.path.insert(0, LAMBDA_DIR)
sys.path.insert(0, A2A)

import tool_consumers  # noqa: E402

assert os.path.isfile(AGENT_PY), AGENT_PY


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _orchestrator_suffixes():
    """What agent.py wraps, plus navigate_to_page.

    `navigate_to_page` is deliberately NOT in `scoped_suffixes` — a deep link is
    identical for every user, so there is no identity to inject. It is still a
    tool the orchestrator calls, so reading only `scoped_suffixes` would claim
    nobody uses it.
    """
    src = _read(AGENT_PY)
    match = re.search(r"scoped_suffixes = \(([^)]*)\)", src, re.S)
    assert match, "scoped_suffixes not found; the generator reads the same shape"
    return set(re.findall(r'"([a-z_]+)"', match.group(1))) | {"navigate_to_page"}


def _declared_by_subagents():
    """slug -> {suffix}, from each tools.py `WANTED`."""
    from common import gateway_tools
    from common.agents import AGENTS

    out = {}
    for directory, (_long, slug) in AGENTS.items():
        path = os.path.join(A2A, directory, "tools.py")
        if not os.path.isfile(path):
            continue
        wanted = set()
        for node in ast.walk(ast.parse(_read(path))):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "WANTED"
                       for t in node.targets):
                continue
            for element in getattr(node.value, "elts", []):
                if isinstance(element, ast.Name):
                    value = getattr(gateway_tools, element.id, None)
                    if isinstance(value, str):
                        wanted.add(value)
                elif isinstance(element, ast.Constant) and isinstance(element.value, str):
                    wanted.add(element.value)
        if wanted:
            out[slug] = wanted
    return out


def test_the_generated_map_matches_the_declarations():
    """The whole point: a stale map answers "who breaks" wrongly, silently."""
    expected: dict[str, set] = {}
    for suffix in _orchestrator_suffixes():
        expected.setdefault(suffix, set()).add("smarthome")
    for slug, suffixes in _declared_by_subagents().items():
        for suffix in suffixes:
            expected.setdefault(suffix, set()).add(slug)

    actual = {k: set(v) for k, v in tool_consumers.TOOL_CONSUMERS.items()}
    assert actual == expected, (
        "tool_consumers.py disagrees with the agents' declared tool lists — run "
        "./venv/bin/python scripts/gen-tool-consumers.py")


def test_the_orchestrator_is_listed_for_every_tool_it_wraps():
    """It is the agent every user's chat goes through, so omitting it from a row
    would understate the blast radius of a revoke more than any other error."""
    for suffix in _orchestrator_suffixes():
        assert "smarthome" in tool_consumers.consumers_for(suffix), suffix


def test_control_device_names_more_than_the_orchestrator():
    """The example that motivated the feature. If this ever collapses to one
    agent, either the map broke or the specialists lost their device tools."""
    consumers = tool_consumers.consumers_for("control_device")
    assert "smarthome" in consumers
    assert len(consumers) >= 2, (
        "control_device is used by the device-control and light-effect "
        "specialists too; a single-entry row hides that")


def test_a_prefixed_gateway_name_resolves():
    """The Gateway hands out `SmartHomeDeviceControl___control_device`, and the
    /tools payload carries whichever form the target declared."""
    assert (tool_consumers.consumers_for("SmartHomeDeviceControl___control_device")
            == tool_consumers.consumers_for("control_device"))


def test_an_unknown_tool_returns_empty_rather_than_guessing():
    """A tool nothing declares is genuinely unused by the agents; claiming an
    owner would be worse than saying nothing."""
    assert tool_consumers.consumers_for("some_new_tool") == []
    assert tool_consumers.consumers_for("") == []


def test_the_returned_list_is_a_copy():
    """A caller mutating the result would corrupt the map for the rest of the
    container's life."""
    first = tool_consumers.consumers_for("control_device")
    first.append("injected")
    assert "injected" not in tool_consumers.consumers_for("control_device")


def test_navigate_to_page_is_orchestrator_only():
    """A deep link is user-independent, so no sub-agent has a reason to build one
    and none declares it. Worth pinning: it is also the one tool deliberately
    absent from scoped_suffixes, so a naive generator would drop it entirely."""
    assert tool_consumers.consumers_for("navigate_to_page") == ["smarthome"]


def test_a_prompt_only_agent_claims_no_tools():
    """home-security, appliance-maintenance and energy-optimization ship no
    tools.py, so they must not appear anywhere in the map."""
    prompt_only = {"sha2asecurity", "sha2amaintenance", "sha2aenergy"}
    listed = {a for agents in tool_consumers.TOOL_CONSUMERS.values() for a in agents}
    assert not (prompt_only & listed), (
        f"prompt-only agent(s) listed as tool consumers: {sorted(prompt_only & listed)}")


def test_the_generated_file_says_it_is_generated():
    body = _read(os.path.join(LAMBDA_DIR, "tool_consumers.py"))
    assert "GENERATED" in body
    assert "gen-tool-consumers.py" in body


def test_the_tools_endpoint_attaches_consumers():
    """The map is useless if /tools does not carry it to the page."""
    import inspect

    import index

    src = inspect.getsource(index.list_gateway_tools)
    assert "tool_consumers.consumers_for" in src
    assert '"consumers"' in src
