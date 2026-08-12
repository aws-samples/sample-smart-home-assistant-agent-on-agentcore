"""Which agents can reach each Gateway tool — GENERATED, do not edit.

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
    'WebSearch': ['smarthome', 'sha2asecurity'],
    'control_device': ['smarthome', 'sha2adevice', 'sha2alight', 'sha2async'],
    'discover_devices': ['smarthome', 'sha2adevice', 'sha2aenergy', 'sha2alight', 'sha2amaintenance', 'sha2asecurity', 'sha2async'],
    'navigate_to_page': ['smarthome'],
    'query_device_state': ['smarthome', 'sha2adevice', 'sha2aenergy', 'sha2alight', 'sha2amaintenance', 'sha2asecurity', 'sha2async'],
    'query_knowledge_base': ['smarthome', 'sha2amaintenance', 'sha2aqa'],
    'query_sensor_history': ['smarthome', 'sha2adevice', 'sha2aenergy', 'sha2amaintenance', 'sha2asecurity'],
}
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
