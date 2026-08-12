"""Gateway tools for the appliance-maintenance agent, built per request.

Before 2026-08-12 this agent recited maintenance intervals from the model's
training data: "clean the filter every three months". Correct-sounding, generic,
and wrong in the one way that matters — it is the same answer for a purifier whose
filter reads 12% as for one that reads 95%. A skill document could have said it
better, because at least a document can be reviewed.

The chain it runs now cannot collapse into one call:

  discover_devices        which appliances exist and which report a wear metric
  query_sensor_history    the measured trend — filter_life falling 4 points a week
                          is a date, not an interval
  query_device_state      where that metric stands right now
  query_knowledge_base    what the manual actually documents for THIS appliance

The output is a service date derived from a measured slope and checked against a
documented threshold, with both shown. Two sources, per appliance, cross-referenced
— which is why it belongs in its own context rather than inline.

Note the two identifiers. Device tools partition on the caller's `sub`; the
knowledge base scopes by EMAIL. They are not interchangeable, and using the wrong
one returns another scope's documents or, more often, none — so the KB call passes
`caller.email` explicitly. See common/gateway_tools.py.
"""

from __future__ import annotations

import logging

from common.gateway_tools import (
    DISCOVER,
    QUERY_HISTORY,
    QUERY_KB,
    QUERY_STATE,
    GatewaySession,
)

logger = logging.getLogger(__name__)

WANTED = (DISCOVER, QUERY_STATE, QUERY_HISTORY, QUERY_KB)

# Metrics that wear down and therefore imply a service date rather than a state.
# Named here so the prompt can point at them and the agent does not have to guess
# which of a device's metrics is the consumable one.
WEAR_METRICS = ("filter_life", "water_level", "bin_level")


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`."""
    try:
        session = GatewaySession(caller, WANTED)
    except RuntimeError as exc:
        logger.error("could not open a gateway session: %s", exc)
        return []

    from strands import tool as strands_tool

    tools: list = []

    if session.has(DISCOVER):
        @strands_tool
        def discover_devices() -> str:
            """List the user's appliances with ids, rooms and capabilities. Call
            this FIRST: which appliances exist, and which of them report a wear
            metric (filter_life, water_level, bin_level), decides whether an answer
            can be measured or only quoted from the manual."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
        @strands_tool
        def query_device_state(device_id: str = "", device_type: str = "") -> str:
            """Read live state for one appliance or the whole fleet. Gives the
            CURRENT value of a wear metric — where the consumable stands today.
            Pair it with query_sensor_history to get a rate; a single value cannot
            produce a date. An appliance reporting no state is unknown, not
            healthy."""
            args = {}
            if device_id:
                args["device_id"] = device_id
            if device_type:
                args["device_type"] = device_type
            return session.call(QUERY_STATE, args)
        tools.append(query_device_state)

    if session.has(QUERY_HISTORY):
        @strands_tool
        def query_sensor_history(device_id: str = "", metric: str = "",
                                 hours: int = 168) -> str:
            """Read a metric over a window with min / max / average / latest
            computed. This is what turns an interval into a date: filter_life
            falling from 60 to 44 over a week is about 2.3 points a day, so the
            30% service threshold is roughly six days out.

            Ask for a wear metric by name (filter_life, water_level, bin_level) for
            a service estimate, or omit `metric` for everything the device reports.
            `hours` is 1 to 168; a week is the default because a shorter window
            gives a slope too noisy to date anything from."""
            args: dict = {"hours": hours}
            if device_id:
                args["device_id"] = device_id
            if metric:
                args["metric"] = metric
            return session.call(QUERY_HISTORY, args)
        tools.append(query_sensor_history)

    if session.has(QUERY_KB):
        @strands_tool
        def query_knowledge_base(query: str) -> str:
            """Search the product manuals and maintenance guides for what is
            DOCUMENTED for this appliance — the service threshold, the procedure,
            the part. Use it to check a measured trend against the manufacturer's
            own figure rather than against a remembered rule of thumb, and cite the
            document. When the manual and the measurement disagree, report both;
            the manual sets the threshold and the measurement sets the date."""
            # The KB partitions on email, not on the sub the device tools use.
            return session.call(QUERY_KB, {"query": query},
                               user_id=caller.email)
        tools.append(query_knowledge_base)

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the maintenance tools — Cedar policy "
            "or the user's tool permissions", caller.sub[:8])

    return tools
