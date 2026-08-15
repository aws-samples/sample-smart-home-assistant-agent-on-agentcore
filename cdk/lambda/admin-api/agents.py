"""The agent fleet — a read model for the Admin Console's Agents page.

"Agent" has not been a first-class entity in this control plane. The first-class
entities are the Cognito user and the skill; an agent appeared only as
`agentType: 'text' | 'voice'`, a two-value enum in the prompt and optimization
paths. With one orchestrator, five specialists and a navigation tool there is no
single place that answers "what agents exist, where do they run, and are they
healthy".

This module builds that list by DERIVING it from what is already true, rather than
introducing a hand-maintained registry:

  1. Runtime ARNs from the dashboard's `_all_runtime_arns()` — text, voice, and
     whatever `DASHBOARD_EXTRA_RUNTIME_ARNS` carries. `a2a-agent-registry/deploy.py`
     appends to that variable on every deploy, so a newly deployed sub-agent shows
     up here with no code change.
  2. Approved AGENT records from AWS Agent Registry, which carry the AgentCard —
     the description, skills and invocation URL a human actually wants to read.
  3. Optional per-agent metadata rows in DynamoDB, for the things neither source
     knows: a display label, a Chinese name, which orchestrator owns it.

Deriving rather than listing is the same choice the A2A authorisation tree already
makes, and it is why that tree needs no frontend change when an agent is added.
A hardcoded fleet list would have to be edited in lockstep with three deploy
scripts, and would be wrong in exactly the situation it matters — right after
someone adds an agent.

The three sources are joined on the runtime NAME, because that is the only
identifier they share. Getting from a Registry record to that name is the fragile
part and it lives in `runtime_name_for_record`: a card's `url` is only sometimes a
runtime ARN, and since the A2A gateway cutover it usually is not. Records whose
runtime is not in the ARN list still appear, flagged, since an approved record with
no live runtime is worth seeing rather than hiding.

Identity comes from the RECORD, never from the runtime. A joined row's label is the
Registry record name (`energy-optimization-agent`), not the runtime name it happens
to run on (`sha2aenergy_sha2aenergy`) — the runtime name is a deploy-time artifact of
an 18-character CLI slug limit, and for a third-party agent the runtime may not even
be in this account. The runtime contributes liveness and metrics; that is all.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger()

# Metadata rows live on the __global__ partition with an SK prefix so they can be
# fetched with one begins_with query. The skills table has no GSI and several
# call sites already fall back to full-table scans; adding another scan for a page
# that loads on every visit is the wrong direction.
AGENT_META_SK_PREFIX = "__agent_"

# An agent's role in the fleet. `orchestrator` is the user-facing entry point;
# `specialist` is reached over A2A; `tool` is not an agent at all but appears in
# the fleet view because the customer's architecture counts it as one of the seven
# entities and operators ask where it went.
KIND_ORCHESTRATOR = "orchestrator"
KIND_SPECIALIST = "specialist"
KIND_VOICE = "voice"
KIND_TOOL = "tool"
# The bundles runtime is the same image as the orchestrator with
# ENABLE_BUNDLE_HOOK=1, used only when a tenant is in `ab-bundles` mode. It is
# infrastructure for A/B testing rather than an agent of its own, so listing it as
# a second orchestrator would overstate the fleet — but hiding it would make its
# token spend unattributable, which is why it is in the dashboard allowlist.
KIND_VARIANT = "variant"


def _meta_sk(agent_id: str) -> str:
    return f"{AGENT_META_SK_PREFIX}{agent_id}__"


def _agent_id_from_sk(sk: str) -> str:
    body = sk[len(AGENT_META_SK_PREFIX):] if sk.startswith(AGENT_META_SK_PREFIX) else sk
    return body[:-2] if body.endswith("__") else body


def load_metadata(table) -> dict[str, dict]:
    """Per-agent metadata rows, keyed by agent id.

    Absent rows are normal — metadata is additive, and the fleet renders without
    it. A failure here is logged and swallowed for the same reason: a missing
    label should not blank the page.
    """
    from boto3.dynamodb.conditions import Key

    out: dict[str, dict] = {}
    try:
        resp = table.query(
            KeyConditionExpression=Key("userId").eq("__global__")
            & Key("skillName").begins_with(AGENT_META_SK_PREFIX)
        )
        for item in resp.get("Items", []):
            out[_agent_id_from_sk(item.get("skillName", ""))] = item
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent metadata query failed: %s", exc)
    return out


def _runtime_id_from_arn(arn: str) -> str:
    if not arn or ":" not in arn:
        return ""
    tail = arn.split(":")[-1]
    return tail.split("/", 1)[1] if "/" in tail else tail


def _runtime_name(arn: str) -> str:
    """`...runtime/sha2alight_sha2alight-KAoL` → `sha2alight_sha2alight`."""
    rid = _runtime_id_from_arn(arn)
    return rid.rsplit("-", 1)[0] if "-" in rid else rid


def _runtime_name_from_card_url(url: str) -> str:
    """Pull the runtime name out of an AgentCard's DIRECT invocation URL.

    Only the `/runtimes/<percent-encoded arn>/invocations` shape. A gateway target
    URL carries no ARN at all and returns "" here — `runtime_name_for_record` is what
    handles that, from an ARN its caller resolved with an AWS call.
    """
    if not url:
        return ""
    from urllib.parse import unquote

    decoded = unquote(url)
    marker = "/runtimes/"
    if marker not in decoded:
        return ""
    tail = decoded.split(marker, 1)[1]
    arn = tail.split("/invocations", 1)[0]
    return _runtime_name(arn)


def runtime_name_for_record(rec: dict) -> str:
    """The join key for one Registry record: the runtime name behind its card.

    Prefers `rec["runtimeArn"]`, which the caller resolved — and that preference is
    the whole point. A card's `url` used to BE the runtime ARN, so this module read
    the key straight out of it. On 2026-08-15 all eight of our cards moved behind the
    A2A gateway, where the URL is `https://<gw>.gateway.../<targetName>` and contains
    no ARN, so the key silently became "" and every specialist split into two rows:
    one runtime row with no identity, and one record row falsely flagged as having no
    live runtime. Resolving a gateway target to its runtime needs an AWS call, which
    is why the ARN arrives from the caller instead of being parsed here.

    The URL fallback still matters: a third party registering their own runtime URL
    directly is a fully supported shape, and it needs no resolution.
    """
    return (_runtime_name(rec.get("runtimeArn") or "")
            or _runtime_name_from_card_url((rec.get("card") or {}).get("url", "")))


def _kind_for(runtime_name: str, agent_id: str, is_registry_record: bool) -> str:
    """Classify an agent for the fleet view.

    Name-shape based, which is a heuristic — but the alternative is a hardcoded
    map that goes stale, and metadata rows can override it per agent.
    """
    if "voice" in runtime_name or "voice" in agent_id:
        return KIND_VOICE
    if "bundles" in runtime_name:
        return KIND_VARIANT
    if is_registry_record or runtime_name.startswith("sha2a"):
        return KIND_SPECIALIST
    return KIND_ORCHESTRATOR


def _errors_of(health: dict):
    """One error count per agent, from the two CloudWatch metrics.

    AWS/Bedrock-AgentCore has no `Errors` metric: it emits `UserErrors` and
    `SystemErrors`, and `_fetch_health` keeps that split per runtime. Reading a
    non-existent `errors` key is why this column read `--` for every agent on the
    first live call — an "unknown" an operator would take as "nothing to see"
    while an agent was in fact failing. The dashboard's own aggregate adds the
    two metrics, so summing them here keeps the page and the dashboard agreeing.

    None only when NEITHER metric reported, which is genuinely unknown.
    """
    parts = [health.get("userErrors"), health.get("systemErrors")]
    known = [p for p in parts if isinstance(p, (int, float))]
    return float(sum(known)) if known else None


def build_fleet(runtime_arns: list[str], registry_records: list[dict],
                metadata: dict[str, dict],
                health_runtimes: list[dict] | None = None,
                gateway_tools: list[dict] | None = None) -> list[dict]:
    """Join the three sources into one fleet list.

    `health_runtimes` is the dashboard's existing per-runtime breakdown, already
    returned by the backend and previously consumed only in aggregate — passing it
    through fills the invocations / errors / latency columns with no new query.
    """
    by_runtime: dict[str, dict] = {}
    order: list[str] = []

    for arn in runtime_arns:
        name = _runtime_name(arn)
        if not name:
            continue
        # The runtime name is `<project>_<runtime>`; the project half is the
        # natural id. `smarthome_bundles` would otherwise collapse onto the
        # orchestrator's id, giving two rows the same trackBy key and the same
        # metadata row.
        head, _, tail = name.partition("_")
        agent_id = (name if tail and tail != head else head) or name
        by_runtime[name] = {
            "agentId": agent_id,
            "runtimeName": name,
            "runtimeArn": arn,
            "runtimeId": _runtime_id_from_arn(arn),
            "kind": _kind_for(name, agent_id, False),
            "skills": [],
            "recordId": "",
            "description": "",
            "live": True,
        }
        order.append(name)

    for rec in registry_records or []:
        card = rec.get("card") or {}
        name = runtime_name_for_record(rec)
        entry = by_runtime.get(name)
        if entry is None:
            # Approved record with no runtime in the ARN allowlist. Surfaced rather
            # than dropped: either the runtime was torn down and the record
            # orphaned, or the deploy that registered it never ran
            # `patch-text-agent`, so DASHBOARD_EXTRA_RUNTIME_ARNS never learned
            # about it. Both are worth an operator's attention, and silently
            # omitting the agent is how the second one goes unnoticed.
            name = name or f"record:{rec.get('recordId', '')}"
            entry = {
                "agentId": (card.get("name") or rec.get("displayName")
                            or rec.get("name") or name),
                "runtimeName": name,
                "runtimeArn": "",
                "runtimeId": "",
                "kind": KIND_SPECIALIST,
                "skills": [],
                "recordId": "",
                "description": "",
                "live": False,
            }
            by_runtime[name] = entry
            order.append(name)
        entry["kind"] = KIND_SPECIALIST
        entry["recordId"] = rec.get("recordId", "")
        entry["registryStatus"] = rec.get("status", "")
        entry["displayName"] = (rec.get("displayName") or card.get("name")
                                or entry.get("displayName", ""))
        entry["description"] = card.get("description", "") or entry["description"]
        entry["version"] = card.get("version", "")
        entry["skills"] = [
            {"id": s.get("id", ""), "name": s.get("name", s.get("id", "")),
             "description": s.get("description", "")}
            for s in (card.get("skills") or [])
        ]
        entry["invocationUrl"] = card.get("url", "")

    # dashboard._fetch_health keys its per-runtime rows by `name`, which is the
    # same runtime name this join is built on (`_runtime_name_from_arn`), so no
    # translation is needed. Its metric labels are the raw CloudWatch ones.
    health_by_runtime = {}
    for row in health_runtimes or []:
        name = row.get("name") or ""
        if name:
            health_by_runtime[name] = row

    fleet = []
    for name in order:
        entry = by_runtime[name]
        meta = metadata.get(entry["agentId"], {})
        # Metadata wins where present — it is the only place a human-chosen label
        # or a corrected kind can live.
        if meta.get("label"):
            entry["displayName"] = meta["label"]
        if meta.get("labelZh"):
            entry["displayNameZh"] = meta["labelZh"]
        if meta.get("kind"):
            entry["kind"] = meta["kind"]
        if meta.get("description"):
            entry["description"] = meta["description"]
        if meta.get("orchestrator"):
            entry["orchestrator"] = meta["orchestrator"]
        entry.setdefault("displayName", entry["agentId"])

        health = health_by_runtime.get(name, {})
        entry["invocations"] = health.get("invocations")
        entry["errors"] = _errors_of(health)
        entry["userErrors"] = health.get("userErrors")
        entry["systemErrors"] = health.get("systemErrors")
        entry["throttles"] = health.get("throttles")
        entry["latencyP95Ms"] = health.get("latencyP95")
        fleet.append(entry)

    # The navigation DeepLink is a Gateway Lambda target, not an agent, but the
    # customer's architecture counts it among the seven entities and an operator
    # looking for it should find it here rather than concluding it was dropped.
    for tool in gateway_tools or []:
        if tool.get("name") == "navigate_to_page":
            fleet.append({
                "agentId": "navigate_to_page",
                "displayName": "navigate_to_page",
                "runtimeName": "",
                "runtimeArn": "",
                "runtimeId": "",
                "kind": KIND_TOOL,
                "description": tool.get("description", ""),
                "skills": [],
                "recordId": "",
                "live": True,
                "targetName": tool.get("targetName", ""),
                # A Lambda target emits no AgentCore runtime metrics, so these are
                # None rather than absent: every row carries the same keys, and a
                # consumer reading `row["invocations"]` should not have to know
                # which rows are agents. None renders as "--" (unknown), which is
                # the honest value — not 0, which would read as "never called".
                "invocations": None,
                "errors": None,
                "userErrors": None,
                "systemErrors": None,
                "throttles": None,
                "latencyP95Ms": None,
            })
            break

    return fleet
