#!/usr/bin/env python3
"""Regenerate the admin Lambda's map of which agents use which Gateway tool.

The Tool Policy page needs to say who breaks when a tool is revoked. That
answer lives in two places, and neither is visible to the admin Lambda (which is
packaged from its own directory):

  - each sub-agent's `tools.py` declares `WANTED`, the Gateway tool suffixes it
    asks `GatewaySession` for — and the session only ever exposes those;
  - `agent/agent.py` wraps a fixed tuple of suffixes so it can inject the
    caller's identity, which is precisely the orchestrator's own tool list.

Both are read here and written into `cdk/lambda/admin-api/tool_consumers.py`.
`test_tool_consumers.py` fails when the generated map and the sources disagree,
so an agent that gains or loses a tool cannot silently leave the page wrong.

Usage:  ./venv/bin/python scripts/gen-tool-consumers.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
A2A = ROOT / "a2a-agent-registry"
AGENT_PY = ROOT / "agent" / "agent.py"
TARGET = ROOT / "cdk" / "lambda" / "admin-api" / "tool_consumers.py"

HEADER = '''"""Which agents can reach each Gateway tool — GENERATED, do not edit.

The Tool Policy page lists the Gateway's tools as a flat set of checkboxes. That
was right when one agent existed. With an orchestrator and six specialists it
hides what an administrator needs before revoking a tool: *who breaks*. Revoking
`control_device` stops the user's chat commands, their scheduled scenes, the
light-effect specialist and the device-control specialist — and nothing on that
page said so.

Derived from what each agent declares, never hand-maintained:
  - a sub-agent's `tools.py` names its Gateway tools in `WANTED`
  - `agent/agent.py` wraps `scoped_suffixes`, which is the orchestrator's own list

Regenerate with:  ./venv/bin/python scripts/gen-tool-consumers.py
Enforced by:      cdk/lambda/admin-api/tests/test_tool_consumers.py
"""

# fmt: off
TOOL_CONSUMERS: dict[str, list[str]] = {
'''

FOOTER = '''}
# fmt: on


def consumers_for(tool_name: str) -> list[str]:
    """Agent ids that can call `tool_name`, or [] when nothing declares it.

    Matches on the bare suffix: the Gateway prefixes a tool with its target
    (`SmartHomeDeviceControl___control_device`) and callers hold either form.
    """
    if not tool_name:
        return []
    suffix = tool_name.split("___")[-1]
    return list(TOOL_CONSUMERS.get(suffix, []))
'''


def orchestrator_suffixes() -> list[str]:
    """The suffixes agent.py wraps, plus the ones it deliberately does not.

    `scoped_suffixes` is only the tools that need an identity injected, so reading
    it alone under-reports what the orchestrator can call — and this map exists to
    answer "who breaks if I revoke this", where a false "nobody" is the worst
    possible answer.

    Two tools are unscoped on purpose:
      - `navigate_to_page`: a deep link is identical for every user, so there is
        no identity to inject.
      - `WebSearch`: it reads public pages, and the connector rejects an
        unexpected `user_id` outright. It comes from a second gateway, so it is
        only claimed here when agent.py actually opens that client.
    """
    src = AGENT_PY.read_text(encoding="utf-8")
    match = re.search(r"scoped_suffixes = \(([^)]*)\)", src, re.S)
    if not match:
        raise SystemExit("scoped_suffixes not found in agent/agent.py")
    found = re.findall(r'"([a-z_]+)"', match.group(1))
    extra = ["navigate_to_page"]
    # Derived from the code rather than hardcoded: if the second MCP client is
    # ever removed, this stops claiming the orchestrator can search the web.
    if "WEBSEARCH_GATEWAY_URL" in src:
        extra.append("WebSearch")
    return found + extra


def subagent_wanted() -> dict[str, list[str]]:
    """agentId -> Gateway tool suffixes, from each agent's tools.py `WANTED`.

    The constants (`CONTROL`, `QUERY_KB`, …) are resolved from
    `common/gateway_tools.py` rather than assumed, so renaming one there does not
    quietly drop an agent from the map.
    """
    sys.path.insert(0, str(A2A))
    from common.agents import AGENTS  # noqa: PLC0415
    from common import gateway_tools  # noqa: PLC0415

    out: dict[str, list[str]] = {}
    for directory, (_long, slug) in AGENTS.items():
        tools_py = A2A / directory / "tools.py"
        if not tools_py.is_file():
            continue  # a prompt-only agent reaches no Gateway tool
        tree = ast.parse(tools_py.read_text(encoding="utf-8"))
        wanted: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "WANTED"
                       for t in node.targets):
                continue
            for element in getattr(node.value, "elts", []):
                if isinstance(element, ast.Name):
                    value = getattr(gateway_tools, element.id, None)
                    if isinstance(value, str):
                        wanted.append(value)
                elif isinstance(element, ast.Constant) and isinstance(element.value, str):
                    wanted.append(element.value)
        if wanted:
            out[slug] = sorted(set(wanted))
    return out


def main() -> int:
    orchestrator = orchestrator_suffixes()
    subagents = subagent_wanted()

    # tool suffix -> [agent ids], orchestrator first because it is the one an
    # operator recognises and the one every user's chat goes through.
    mapping: dict[str, list[str]] = {}
    for suffix in orchestrator:
        mapping.setdefault(suffix, []).append("smarthome")
    for agent_id, suffixes in sorted(subagents.items()):
        for suffix in suffixes:
            mapping.setdefault(suffix, []).append(agent_id)

    lines = [HEADER]
    for suffix in sorted(mapping):
        lines.append(f"    {suffix!r}: {mapping[suffix]!r},\n")
    lines.append(FOOTER)
    TARGET.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} "
          f"({len(mapping)} tools, {1 + len(subagents)} agents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
