"""Gateway tools for the home-security agent, built per request.

Until 2026-08-12 this agent was a system prompt that ranked security gaps in a
setup "the user stated". It never read the setup. So it produced a generic
checklist — plausible, unfalsifiable, and identical whether the user owned twelve
devices or none. A skill could have printed the same checklist, which is precisely
why the agent needed to become one.

What it can do now that neither a tool nor a skill can:

  discover_devices      the ACTUAL fleet — which devices, which rooms, which
                        connectivity, so a finding names a device the user owns
  query_device_state    live state, which is where real findings come from: a
                        plug left on while the house reports empty, an oven still
                        powered, a purifier reporting no state at all
  query_sensor_history   whether an anomaly is an event or a baseline — a motion
                        report at 3am matters differently if it happens nightly
  WebSearch             published advisories for the protocols and device classes
                        the user actually runs, from vendor and standards domains

That last one is the part worth pointing at. Correlating a live fleet against
current public advisories is not a lookup: it needs the fleet enumerated first, a
query composed per device class from what was found, results filtered for
relevance, and findings ranked by exposure. The answer changes week to week
because the web changes, and it cannot be precomputed into a skill document.

Web search is on a SEPARATE gateway in us-east-1 (the connector is not offered in
us-west-2) and is separately grantable in Tool Policy, so an operator can give
this agent live-web reach without giving it device control, or the reverse.

Identity model is unchanged: the caller's idToken goes to both gateways, Cedar
evaluates the real end user at each, and this runtime holds no permissions of its
own.
"""

from __future__ import annotations

import logging

from common.gateway_tools import (
    DISCOVER,
    QUERY_HISTORY,
    QUERY_STATE,
    WEB_SEARCH,
    GatewaySession,
)

logger = logging.getLogger(__name__)

WANTED = (DISCOVER, QUERY_STATE, QUERY_HISTORY, WEB_SEARCH)

# Domains an advisory lookup is worth trusting. Passed as a per-request include
# filter (connector v1.2.0+) rather than left to the open web, because a security
# finding sourced from a content farm is worse than no finding — it reads exactly
# as authoritative in a summary. Standards bodies, national CERTs and the vendors
# whose protocols this fleet actually speaks.
ADVISORY_DOMAINS = [
    "csa-iot.org", "zigbeealliance.org", "cve.org", "nvd.nist.gov",
    "cisa.gov", "kb.cert.org", "us-cert.gov", "owasp.org",
    "matter-smarthome.de", "wi-fi.org", "bluetooth.com",
]


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
            """List the user's devices with ids, rooms, connectivity and
            capabilities. Call this FIRST for any assessment. A finding about a
            device the user does not own is noise, and a generic checklist is what
            this agent exists to stop producing — every finding must name a real
            device id from this call."""
            return session.call(DISCOVER, {})
        tools.append(discover_devices)

    if session.has(QUERY_STATE):
        @strands_tool
        def query_device_state(device_id: str = "", device_type: str = "") -> str:
            """Read live state for the whole fleet (pass no arguments) or one
            device. This is where real findings come from rather than inferred
            ones: something powered that should not be, a device reporting no state
            at all, a lock or plug left in an exposed state. A device with no
            reported state is unknown, not safe — report it as unknown."""
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
            """Read a metric over a window, with min / max / average / latest
            computed. Use it to tell an EVENT from a BASELINE before escalating:
            an unusual reading that recurs nightly is a pattern, and calling it an
            intrusion is a false positive the user will act on. `hours` is 1 to
            168, defaulting to a week."""
            args: dict = {"hours": hours}
            if device_id:
                args["device_id"] = device_id
            if metric:
                args["metric"] = metric
            return session.call(QUERY_HISTORY, args)
        tools.append(query_sensor_history)

    if session.has(WEB_SEARCH):
        @strands_tool
        def search_advisories(query: str, max_results: int = 6) -> str:
            """Search published security advisories and standards guidance for the
            device classes and protocols this user actually runs.

            Compose the query from what discover_devices returned — the protocol,
            the device class, the year — rather than searching for a product name
            you are not certain exists. Results are restricted server-side to
            standards bodies, national CERTs and protocol vendors, so a query that
            returns nothing means there is no advisory on those sources, NOT that
            the setup is safe; say which.

            Cite the URL and the publication date for every claim taken from here.
            An advisory without a date cannot be weighed against a fleet."""
            return session.call(WEB_SEARCH, {
                "query": query[:200],
                "maxResults": max(1, min(int(max_results or 6), 25)),
                "filters": {"domainFilter": {"include": ADVISORY_DOMAINS}},
            })
        tools.append(search_advisories)
    else:
        # Worth a log line: the difference between "no advisories exist" and "this
        # agent cannot reach the web" is invisible in the reply otherwise.
        logger.info("web search not available to sub=%s... — assessment will be "
                    "fleet-only", caller.sub[:8])

    if not tools:
        logger.warning(
            "sub=%s... is permitted none of the security tools — Cedar policy or "
            "the user's tool permissions", caller.sub[:8])

    return tools
