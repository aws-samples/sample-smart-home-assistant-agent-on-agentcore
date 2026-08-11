"""Agent ops Dashboard aggregation — `GET /dashboard`.

Dispatched by `index.py` for `/dashboard`. Feeds the Overview page's ops
dashboard (6 metric groups; see spec
`docs/superpowers/specs/2026-07-29-overview-ops-dashboard-design.md`).

Two-stage by design:

  GET /dashboard?range=7d                    → fast part (~2s)
  GET /dashboard?range=7d&part=spans&dim=user → slow part (Logs Insights, 10-20s)

The fast part is CloudWatch GetMetricData + AgentCore control-plane calls.
The slow part is a Logs Insights query over `aws/spans` — TTFT is NOT
available as a CloudWatch metric (the `bedrock-agentcore` OTel namespace
lists `gen_ai.client.operation.duration` but returns zero datapoints), so
per-request latency + token split have to come from the Strands spans.

Results are cached in the existing `smarthome-skills` table under
`__dashboard_cache_{key}__` sort keys (same "reserved SK" pattern as
`optimization.py` / `tenant_env.py`, so no extra table) because Logs
Insights bills by bytes scanned and admins reload Overview often.

Every block degrades independently: a failing block yields `None`/`{}` for
its own card rather than a 500 for the whole page.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
RUNTIME_SESSIONS_TABLE_NAME = os.environ.get(
    "RUNTIME_SESSIONS_TABLE_NAME", "smarthome-runtime-sessions"
)
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
VOICE_RUNTIME_ARN = os.environ.get("VOICE_AGENT_RUNTIME_ARN", "")
# Comma-separated extra runtime ARNs to fold into the health block (A2A
# specialist runtimes, the bundles runtime, …). Set by setup-agentcore.py /
# a2a-agent-registry/deploy.py as those runtimes come and go.
EXTRA_RUNTIME_ARNS = os.environ.get("DASHBOARD_EXTRA_RUNTIME_ARNS", "")

# Where AgentCore Runtime writes its OTel spans.
#
# It used to be the account-wide `aws/spans` group, and this page read that. It
# is not any more: AgentCore moved trace export into each runtime's OWN log
# group (a `spans` stream inside
# `/aws/bedrock-agentcore/runtimes/{runtimeId}-DEFAULT`). The cutover was clean
# and completely silent — for the orchestrator, `aws/spans` stops at
# 2026-08-05 02:12 and the runtime-local stream starts at 02:28; the sub-agents
# followed on 2026-08-09. Nothing errored, no permission was denied, and the
# Logs Insights query kept succeeding: it simply matched zero records, so the
# TTFT and token-split cards read "no data" for six days as if the system were
# idle.
#
# `aws/spans` is kept in the list rather than replaced. Both are queried and the
# results merged, because a `range=30d` window still straddles the cutover, and
# dropping the old group would erase five of those weeks' history from a page
# whose entire purpose is a trend line.
LEGACY_SPANS_LOG_GROUP = "aws/spans"
SPANS_LOG_GROUP = LEGACY_SPANS_LOG_GROUP  # retained for callers that import it

# Cache rows live on the global partition of the skills table.
CACHE_PK = "__global__"
CACHE_SK_PREFIX = "__dashboard_cache_"
CACHE_SK_SUFFIX = "__"
CACHE_TTL_SECONDS = 300  # 5 minutes

# Total wall-clock budget shared by ALL Logs Insights queries in one request.
# API Gateway's integration timeout is a hard 29s and cannot be raised, so the
# two spans queries must finish (or give up) inside this budget and still leave
# room to serialise the response.
SPANS_QUERY_BUDGET_SECONDS = 22

RANGES = {"24h": 1, "7d": 7, "30d": 30}
DIMS = ("user", "tenant", "agent")

# The 11 online evaluators actually emitting for this project. Each is its
# own metric name in the `Bedrock-AgentCore/Evaluations` namespace.
# `smarthome_SmartHomeQuality` uses a Numerical rating scale (observed mean
# ~3.47), not 0-1, so the UI must not render it as a percentage.
EVALUATORS = [
    "Builtin.Correctness",
    "Builtin.Helpfulness",
    "Builtin.InstructionFollowing",
    "Builtin.ToolSelectionAccuracy",
    "Builtin.GoalSuccessRate",
    "Builtin.Conciseness",
    "Builtin.ResponseRelevance",
    "Builtin.Refusal",
    "Builtin.Harmfulness",
    "Builtin.Stereotyping",
    "smarthome_SmartHomeQuality",
]
NON_RATIO_EVALUATORS = {"smarthome_SmartHomeQuality"}

# Service name as it appears on spans/eval metrics for the text runtime.
# Kept as its own constant because the A/B block's per-variant dimensions are
# text-runtime-only (the control/treatment endpoints hang off this runtime).
# The orchestrator's own service.name. Kept as a constant because two things
# genuinely are orchestrator-specific rather than fleet-wide:
#
#  - the A/B evaluation metrics, which are emitted by the online-eval configs
#    attached to the text runtime and to no other (verified against
#    Bedrock-AgentCore/Evaluations: only this service.name has them);
#  - the fallback entry in `_service_names()`, so the fleet filter still names the
#    orchestrator even if the ARN env vars are empty.
#
# Everything that IS fleet-wide derives its list from the runtime ARNs instead —
# see `_service_names()` — so a new sub-agent needs no code change here.
ORCHESTRATOR_SERVICE_NAME = "smarthome_smarthome.DEFAULT"


def service_name_for(agent_id: str) -> str:
    """The `service.name` a given fleet agentId reports spans under.

    Derived from the configured runtime ARNs, so it answers for any deployed
    agent rather than only the orchestrator. Returns "" for an unknown id, which
    callers treat as "no per-agent data" rather than silently falling back to the
    orchestrator's — attributing one agent's metrics to another is worse than
    showing none.
    """
    if not agent_id:
        return ""
    for arn in _all_runtime_arns():
        svc = _service_name_from_arn(arn)
        name = svc.split(".")[0]
        head, _, tail = name.partition("_")
        candidate = (name if tail and tail != head else head) or name
        if candidate == agent_id:
            return svc
    return ""

_clients = {}


def _client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=AWS_REGION)
    return _clients[service]


def _table(name=None):
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(
        name or SKILLS_TABLE_NAME
    )


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _runtime_id_from_arn(arn):
    """`...:runtime/smarthome_smarthome-ee97ToCthI` → `smarthome_smarthome-ee97ToCthI`."""
    if not arn or ":" not in arn:
        return ""
    tail = arn.split(":")[-1]
    return tail.split("/", 1)[1] if "/" in tail else tail


def _runtime_name_from_arn(arn):
    """Runtime id minus its random suffix → `smarthome_smarthome`."""
    rid = _runtime_id_from_arn(arn)
    return rid.rsplit("-", 1)[0] if "-" in rid else rid


# ---------------------------------------------------------------------------
# Service-name allowlist
#
# Every runtime OTel-tags its spans and eval metrics with
# `service.name = <agentRuntimeName>.<endpointName>`. The spans queries used to
# filter `like /smarthome/`, which silently excluded the A2A specialist
# runtimes (`sha2aenergy_sha2aenergy.DEFAULT` and friends) — their tokens are
# real spend but never reached this dashboard.
#
# An exact allowlist rather than a widened regex: this AWS account is shared
# with unrelated projects (midea-langgraph, hermes-agent, demo0731, …), and a
# prefix regex would eventually pull a stranger's traffic into these numbers.
# ---------------------------------------------------------------------------

def _service_name_from_arn(arn, endpoint="DEFAULT"):
    """`...:runtime/smarthome_smarthome-ee97ToCthI` → `smarthome_smarthome.DEFAULT`."""
    name = _runtime_name_from_arn(arn)
    return f"{name}.{endpoint}" if name else ""


def _extra_runtime_arns():
    return [a.strip() for a in EXTRA_RUNTIME_ARNS.split(",") if a.strip()]


def _all_runtime_arns():
    """Text + voice + any extras, de-duplicated, order preserved."""
    out = []
    for arn in [RUNTIME_ARN, VOICE_RUNTIME_ARN, *_extra_runtime_arns()]:
        if arn and arn not in out:
            out.append(arn)
    return out


def _service_names():
    """Exact service.name values this project owns.

    Derived from the configured runtime ARNs so a newly deployed A2A agent is
    covered by setting DASHBOARD_EXTRA_RUNTIME_ARNS — no code change needed.
    """
    out = []
    for arn in _all_runtime_arns():
        svc = _service_name_from_arn(arn)
        if svc and svc not in out:
            out.append(svc)
    if ORCHESTRATOR_SERVICE_NAME not in out:
        out.insert(0, ORCHESTRATOR_SERVICE_NAME)
    return out


def _spans_service_filter():
    """Logs Insights clause restricting spans to this project's runtimes.

    `in [...]` is an exact match — verified against aws/spans, where it
    returned only our runtimes and excluded every unrelated project.

    Still applied when querying the per-runtime groups, where it is redundant by
    construction. Cheap, and it keeps one query string correct for both sources
    rather than forking it.
    """
    names = ", ".join(f'"{n}"' for n in _service_names())
    return f"| filter resource.attributes.service.name in [{names}]\n"


def _runtime_span_log_groups():
    """The per-runtime span log groups for every runtime this project owns.

    One group per runtime, which is where AgentCore writes spans now. Empty if no
    runtime ARN is configured, in which case the caller falls back to the legacy
    account-wide group alone.
    """
    out = []
    for arn in _all_runtime_arns():
        runtime_id = _runtime_id_from_arn(arn)
        if runtime_id:
            group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
            if group not in out:
                out.append(group)
    return out


def _existing_log_groups(names):
    """Filter `names` down to the groups that exist, preserving order.

    Necessary, not defensive: `StartQuery` rejects the WHOLE request with
    ResourceNotFoundException if any single named group is missing, so one
    torn-down sub-agent runtime still listed in DASHBOARD_EXTRA_RUNTIME_ARNS
    would take the entire dashboard down with it. Verified against the live
    account by naming one real group and one fake one.

    A describe failure returns the list unfiltered rather than empty: losing the
    ability to check is not evidence that nothing exists, and the query's own
    error handling is the backstop.
    """
    logs = _client("logs")
    found = []
    for name in names:
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=name, limit=50)
        except Exception as e:  # noqa: BLE001
            logger.warning("describe_log_groups failed for %s: %s", name, e)
            return list(names)
        if any(g.get("logGroupName") == name for g in resp.get("logGroups", [])):
            found.append(name)
        else:
            logger.info("span log group %s does not exist; skipping", name)
    return found


def _spans_log_groups():
    """Every log group that could hold this project's spans, newest source first.

    Both sources are queried together because a 30d window straddles the
    2026-08-05 cutover described at LEGACY_SPANS_LOG_GROUP. Logs Insights unions
    the groups itself, and the `service.name` filter keeps other projects out of
    the legacy one.
    """
    return _existing_log_groups(
        [*_runtime_span_log_groups(), LEGACY_SPANS_LOG_GROUP]
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_sk(key):
    return f"{CACHE_SK_PREFIX}{key}{CACHE_SK_SUFFIX}"


def _cache_get(key):
    """Return the cached payload dict, or None on miss/expiry/error."""
    try:
        item = _table().get_item(
            Key={"userId": CACHE_PK, "skillName": _cache_sk(key)}
        ).get("Item")
    except Exception as e:  # noqa: BLE001 — cache must never break the request
        logger.warning("dashboard cache read failed for %s: %s", key, e)
        return None
    if not item:
        return None
    try:
        if int(item.get("expiresAt", 0)) < int(time.time()):
            return None
        payload = json.loads(item["payload"])
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("dashboard cache row unusable for %s: %s", key, e)
        return None
    payload["cached"] = True
    payload["cachedAt"] = item.get("cachedAt", "")
    return payload


def _cache_put(key, payload):
    now = int(time.time())
    try:
        _table().put_item(Item={
            "userId": CACHE_PK,
            "skillName": _cache_sk(key),
            "payload": json.dumps(payload, default=str),
            "cachedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expiresAt": now + CACHE_TTL_SECONDS,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("dashboard cache write failed for %s: %s", key, e)


# ---------------------------------------------------------------------------
# Block 1 — health (CloudWatch metrics)
# ---------------------------------------------------------------------------

def _runtime_dims(arn):
    """Per-runtime dimension set for AWS/Bedrock-AgentCore Invocations etc."""
    return [
        {"Name": "Resource", "Value": arn},
        {"Name": "Operation", "Value": "InvokeAgentRuntime"},
        {"Name": "Name", "Value": f"{_runtime_name_from_arn(arn)}::DEFAULT"},
    ]


def _fetch_health(days):
    """Invocations / errors / throttles / sessions from CloudWatch.

    Covers every configured runtime (text + voice + extras), not just text —
    the per-runtime dimensions are exact, so querying one ARN hid the voice and
    A2A runtimes' invocations and errors entirely. Per-runtime totals are also
    returned so the UI can break the fleet down.

    ActiveSessionCount only exists at ACCOUNT level (dimension
    Service=AgentCore.Runtime) — there is no per-runtime variant — so the UI
    labels that tile as account-wide.
    """
    if not RUNTIME_ARN:
        return {"available": False, "reason": "AGENT_RUNTIME_ARN not configured"}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # One datapoint per day for 7d/30d; hourly for 24h so the sparkline moves.
    period = 3600 if days <= 1 else 86400
    arns = _all_runtime_arns()

    per_runtime_metrics = (
        ("invocations", "Invocations", "Sum"),
        ("userErrors", "UserErrors", "Sum"),
        ("systemErrors", "SystemErrors", "Sum"),
        ("throttles", "Throttles", "Sum"),
        ("sessions", "Sessions", "Sum"),
        ("latencyP95", "Latency", "p95"),
    )

    mdq = []
    for ai, arn in enumerate(arns):
        dims = _runtime_dims(arn)
        for mi, (label, mn, stat) in enumerate(per_runtime_metrics):
            mdq.append({
                "Id": f"m{ai}_{mi}",
                # Label carries the runtime so results can be split apart.
                "Label": f"{label}|{_runtime_name_from_arn(arn)}",
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {"Namespace": "AWS/Bedrock-AgentCore",
                               "MetricName": mn, "Dimensions": dims},
                    "Period": period,
                    "Stat": stat,
                },
            })
    mdq.append({
        "Id": "acct_sessions",
        "Label": "activeSessions|__account__",
        "ReturnData": True,
        "MetricStat": {
            "Metric": {"Namespace": "AWS/Bedrock-AgentCore",
                       "MetricName": "ActiveSessionCount",
                       "Dimensions": [{"Name": "Service",
                                       "Value": "AgentCore.Runtime"}]},
            "Period": period,
            "Stat": "Maximum",
        },
    })

    try:
        res = _client("cloudwatch").get_metric_data(
            MetricDataQueries=mdq, StartTime=start, EndTime=end,
            ScanBy="TimestampAscending",
        )
    except ClientError as e:
        logger.warning("GetMetricData (health) failed: %s", e)
        return {"available": False, "reason": str(e)}

    # Fold per-runtime results into fleet-wide series (summed timestamp-wise,
    # max for the p95 envelope) while keeping a per-runtime breakdown.
    series = {}
    by_runtime = {}
    for r in res.get("MetricDataResults", []):
        label, _, runtime = r["Label"].partition("|")
        vals = [float(v) for v in r.get("Values", [])]
        stamps = [t.isoformat() for t in r.get("Timestamps", [])]
        if runtime != "__account__":
            slot = by_runtime.setdefault(runtime, {})
            slot[label] = sum(vals) if label != "latencyP95" else (
                max(vals) if vals else None)
        agg = series.setdefault(label, {})
        for ts, v in zip(stamps, vals):
            if label == "latencyP95":
                agg[ts] = max(agg.get(ts, v), v)
            else:
                agg[ts] = agg.get(ts, 0.0) + v
    series = {
        label: {
            "timestamps": [ts for ts, _ in sorted(points.items())],
            "values": [v for _, v in sorted(points.items())],
        }
        for label, points in series.items()
    }

    def total(name):
        return sum(series.get(name, {}).get("values") or [])

    invocations = total("invocations")
    errors = total("userErrors") + total("systemErrors")
    window_seconds = days * 86400
    latency_vals = series.get("latencyP95", {}).get("values") or []
    active_vals = series.get("activeSessions", {}).get("values") or []

    return {
        "available": True,
        "invocations": invocations,
        "sessions": total("sessions"),
        "userErrors": total("userErrors"),
        "systemErrors": total("systemErrors"),
        "throttles": total("throttles"),
        # Error rate as a share of invocations; None when there is no traffic
        # (0/0 would render a misleading 0%).
        "errorRate": (errors / invocations) if invocations else None,
        "qps": invocations / window_seconds if window_seconds else 0,
        "latencyP95Ms": max(latency_vals) if latency_vals else None,
        "activeSessionsAccount": active_vals[-1] if active_vals else None,
        "series": series,
        # Fleet breakdown: which runtimes these fleet-wide totals came from.
        "runtimes": [
            {"name": name, **totals}
            for name, totals in sorted(by_runtime.items())
        ],
    }


# ---------------------------------------------------------------------------
# Block 4 — evaluation scores (CloudWatch metrics)
# ---------------------------------------------------------------------------

def _fetch_evaluations(days):
    """Per-evaluator average + daily series from Bedrock-AgentCore/Evaluations.

    Queried once per (evaluator, service.name) pair: the dimension is an exact
    match, so a single value would hide every non-text runtime's scores. Series
    for the same evaluator are merged timestamp-wise below.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    period = 3600 if days <= 1 else 86400
    services = _service_names()

    # GetMetricData caps a request at 500 queries; 11 evaluators x N runtimes
    # stays far below that, but clamp so a long extras list can't 400 the page.
    max_services = max(1, 500 // len(EVALUATORS))
    if len(services) > max_services:
        logger.warning(
            "evaluations: %d service names exceeds the GetMetricData budget; "
            "querying the first %d only", len(services), max_services)
        services = services[:max_services]

    mdq = []
    for si, svc in enumerate(services):
        for ei, name in enumerate(EVALUATORS):
            mdq.append({
                "Id": f"e{si}_{ei}",
                "Label": f"{name}|{svc}",
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "Bedrock-AgentCore/Evaluations",
                        "MetricName": name,
                        "Dimensions": [{"Name": "service.name", "Value": svc}],
                    },
                    "Period": period,
                    "Stat": "Average",
                },
            })

    try:
        res = _client("cloudwatch").get_metric_data(
            MetricDataQueries=mdq, StartTime=start, EndTime=end,
            ScanBy="TimestampAscending",
        )
    except ClientError as e:
        logger.warning("GetMetricData (evaluations) failed: %s", e)
        return {"available": False, "reason": str(e), "evaluators": []}

    # Merge the per-service series back into one row per evaluator. Values at
    # the same timestamp are averaged, which matches the previous single-runtime
    # semantics when only the text runtime has data.
    merged = {}
    for r in res.get("MetricDataResults", []):
        label = r["Label"]
        name, _, svc = label.partition("|")
        vals = [float(v) for v in r.get("Values", [])]
        if not vals:
            continue
        slot = merged.setdefault(name, {})
        for ts, v in zip(r.get("Timestamps", []), vals):
            slot.setdefault(ts, []).append(v)

    evaluators = []
    for name, by_ts in merged.items():
        ordered = sorted(by_ts.items())
        r = {
            "Label": name,
            "Timestamps": [ts for ts, _ in ordered],
            "Values": [sum(vs) / len(vs) for _, vs in ordered],
        }
        vals = r["Values"]
        if not vals:
            continue
        # "Drift" = second-half mean minus first-half mean over the window.
        half = len(vals) // 2
        drift = None
        if half:
            first = sum(vals[:half]) / half
            second = sum(vals[half:]) / len(vals[half:])
            drift = second - first
        evaluators.append({
            "name": r["Label"],
            "average": sum(vals) / len(vals),
            "latest": vals[-1],
            "drift": drift,
            "isRatio": r["Label"] not in NON_RATIO_EVALUATORS,
            "timestamps": [t.isoformat() for t in r.get("Timestamps", [])],
            "values": vals,
        })
    evaluators.sort(key=lambda e: e["name"])
    return {"available": bool(evaluators), "evaluators": evaluators}


def _eval_config_id(arn_or_id):
    """`...:online-evaluation-config/smarthome_x-AbC123` → `smarthome_x-AbC123`."""
    if not arn_or_id:
        return ""
    return arn_or_id.rsplit("/", 1)[-1] if "/" in arn_or_id else arn_or_id


def _fetch_ab_comparison(days):
    """Per-variant eval scores for the A/B pair, when they have data.

    The control/treatment online-eval configs exist and are ACTIVE, but the
    A/B tests are STOPPED and these dimensions currently return zero
    datapoints. Returns available=False so the UI shows an empty state
    instead of a fabricated curve.
    """
    # setup-agentcore.py sets these as full ARNs; the CloudWatch dimension
    # wants the trailing config id.
    control = _eval_config_id(os.environ.get("CONTROL_ONLINE_EVAL_ARN", ""))
    treatment = _eval_config_id(os.environ.get("TREATMENT_ONLINE_EVAL_ARN", ""))
    if not control and not treatment:
        return {"available": False, "reason": "no per-variant eval configs set"}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    mdq = []
    for vi, (variant, cfg) in enumerate((("C", control), ("T1", treatment))):
        if not cfg:
            continue
        for ei, name in enumerate(EVALUATORS[:4]):
            mdq.append({
                "Id": f"ab{vi}_{ei}",
                "Label": f"{variant}|{name}",
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "Bedrock-AgentCore/Evaluations",
                        "MetricName": name,
                        "Dimensions": [
                            {"Name": "onlineEvaluationConfigId", "Value": cfg},
                            # The A/B evaluators are attached to the
                            # orchestrator's runtime and to no other, so this
                            # dimension is correctly orchestrator-specific rather
                            # than a missed parameterisation. Confirmed against
                            # Bedrock-AgentCore/Evaluations, where only this
                            # service.name carries evaluation metrics.
                            {"Name": "service.name",
                             "Value": ORCHESTRATOR_SERVICE_NAME},
                        ],
                    },
                    "Period": 86400,
                    "Stat": "Average",
                },
            })
    if not mdq:
        return {"available": False, "reason": "no per-variant eval configs set"}

    try:
        res = _client("cloudwatch").get_metric_data(
            MetricDataQueries=mdq, StartTime=start, EndTime=end)
    except ClientError as e:
        logger.warning("GetMetricData (ab) failed: %s", e)
        return {"available": False, "reason": str(e)}

    rows = []
    for r in res.get("MetricDataResults", []):
        vals = [float(v) for v in r.get("Values", [])]
        if not vals:
            continue
        variant, _, name = r["Label"].partition("|")
        rows.append({"variant": variant, "evaluator": name,
                     "average": sum(vals) / len(vals), "n": len(vals)})
    return {"available": bool(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Block 5 — versions, endpoints, rollout stage, rollback history
# ---------------------------------------------------------------------------

def _rollout_stage(ab_running, tenant_override_count):
    """Derive a rollout stage — AgentCore has no native field for this.

    This project's gradual rollout is implemented with Gateway A/B tests plus
    per-tenant routing in the `tenant_env` rows, so the stage is inferred
    from those two. Shadow has no implementation here and is never returned.
    The UI labels this as a project-specific derivation.
    """
    if not ab_running:
        return "Full"
    return "Canary" if tenant_override_count > 0 else "Percentage"


def _count_tenant_overrides():
    try:
        resp = _table().query(
            KeyConditionExpression=Key("userId").eq("__global__")
            & Key("skillName").begins_with("__tenant_env_")
        )
        return len(resp.get("Items", []))
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant_env count failed: %s", e)
        return 0


def _fetch_release():
    if not RUNTIME_ARN:
        return {"available": False, "reason": "AGENT_RUNTIME_ARN not configured"}
    runtime_id = _runtime_id_from_arn(RUNTIME_ARN)
    control = _client("bedrock-agentcore-control")

    endpoints, latest_version = [], None
    try:
        resp = control.list_agent_runtime_endpoints(agentRuntimeId=runtime_id)
        for ep in resp.get("runtimeEndpoints", []):
            endpoints.append({
                "name": ep.get("name", ""),
                "liveVersion": ep.get("liveVersion", ""),
                "status": ep.get("status", ""),
                "description": ep.get("description", ""),
                "lastUpdatedAt": str(ep.get("lastUpdatedAt", "")),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("list_agent_runtime_endpoints failed: %s", e)

    try:
        resp = control.list_agent_runtime_versions(
            agentRuntimeId=runtime_id, maxResults=1)
        versions = resp.get("agentRuntimes", [])
        if versions:
            latest_version = versions[0].get("agentRuntimeVersion")
    except Exception as e:  # noqa: BLE001
        logger.warning("list_agent_runtime_versions failed: %s", e)

    # A/B tests live on the DATA plane client, not control (see optimization.py).
    ab_tests, ab_running = [], False
    try:
        resp = _client("bedrock-agentcore").list_ab_tests()
        for t in resp.get("abTests", []):
            name = t.get("name", "")
            # Only this project's tests; the account hosts unrelated ones.
            if not (name.startswith("abtest_") or "smarthome" in name.lower()):
                continue
            exec_status = t.get("executionStatus", "")
            if exec_status == "RUNNING":
                ab_running = True
            ab_tests.append({
                "name": name,
                "status": t.get("status", ""),
                "executionStatus": exec_status,
                "updatedAt": str(t.get("updatedAt", "")),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("list_ab_tests failed: %s", e)

    tenant_overrides = _count_tenant_overrides()

    # Rollback / version-switch history. CloudTrail retains 90 days, so this
    # is a recent-history view, not a full audit log.
    history = []
    try:
        resp = _client("cloudtrail").lookup_events(
            LookupAttributes=[{
                "AttributeKey": "EventName",
                "AttributeValue": "UpdateAgentRuntimeEndpoint",
            }],
            StartTime=datetime.now(timezone.utc) - timedelta(days=90),
            EndTime=datetime.now(timezone.utc),
            MaxResults=20,
        )
        for ev in resp.get("Events", []):
            try:
                detail = json.loads(ev.get("CloudTrailEvent", "{}"))
                params = detail.get("requestParameters") or {}
            except (ValueError, TypeError):
                params = {}
            if params.get("agentRuntimeId") and params["agentRuntimeId"] != runtime_id:
                continue
            history.append({
                "eventTime": str(ev.get("EventTime", "")),
                "username": ev.get("Username", ""),
                "endpointName": params.get("endpointName", ""),
                "targetVersion": params.get("agentRuntimeVersion", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("CloudTrail lookup_events failed: %s", e)

    return {
        "available": bool(endpoints) or latest_version is not None,
        "runtimeId": runtime_id,
        "latestVersion": latest_version,
        "endpoints": endpoints,
        "abTests": ab_tests,
        "abRunning": ab_running,
        "tenantOverrides": tenant_overrides,
        "rolloutStage": _rollout_stage(ab_running, tenant_overrides),
        "rollbackHistory": history,
        "historyRetentionDays": 90,
    }


# ---------------------------------------------------------------------------
# Block 2 — TTFT + token split (Logs Insights over aws/spans)
# ---------------------------------------------------------------------------

def _run_logs_insights(query, days, deadline=None):
    """Start a Logs Insights query and poll to completion.

    Mirrors the polling shape of index.py's `_fetch_token_totals_7d`: bounded
    wait, stop_query on timeout, empty result on any failure so the caller can
    degrade instead of erroring.

    `deadline` is an absolute `time.time()` value SHARED across all queries in
    one request. API Gateway's integration timeout is a hard 29s, so the two
    queries in `_fetch_spans` must share one budget — giving each its own
    20s would risk a 40s worst case and a 504 with no usable response.
    """
    if deadline is None:
        deadline = time.time() + SPANS_QUERY_BUDGET_SECONDS
    logs = _client("logs")
    if time.time() >= deadline:
        logger.warning("Logs Insights budget exhausted before start_query")
        return None
    end_time = int(time.time())
    start_time = end_time - days * 86400
    groups = _spans_log_groups()
    if not groups:
        logger.info("no span log group exists; spans block unavailable")
        return None
    try:
        start = logs.start_query(
            # logGroupNames (plural): spans live in one group per runtime now,
            # plus the legacy account-wide group for history before the cutover.
            logGroupNames=groups,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = start["queryId"]
    except logs.exceptions.ResourceNotFoundException:
        # Should not happen — `_spans_log_groups` already filtered to existing
        # groups — but a group deleted between the check and the call lands here.
        logger.info("span log group vanished between check and query: %s", groups)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("start_query failed: %s", e)
        return None

    while time.time() < deadline:
        try:
            res = logs.get_query_results(queryId=query_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("get_query_results failed: %s", e)
            return None
        status = res.get("status", "")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            if status != "Complete":
                logger.warning("Logs Insights ended status=%s", status)
                return None
            return [
                {f["field"]: f["value"] for f in row}
                for row in res.get("results", [])
            ]
        time.sleep(0.5)

    logger.warning("Logs Insights query %s timed out client-side", query_id)
    try:
        logs.stop_query(queryId=query_id)
    except Exception:  # noqa: BLE001
        pass
    return None


def _num(row, key, default=0.0):
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _fetch_spans(days, dim):
    """Daily TTFT percentiles + input/output token split, plus attribution.

    Two queries because Logs Insights can't group by both a time bin and a
    session id in one `stats` and still give clean per-day rows. Both share one
    deadline so the pair can never blow the API Gateway 29s ceiling.
    """
    deadline = time.time() + SPANS_QUERY_BUDGET_SECONDS
    svc_filter = _spans_service_filter()
    daily = _run_logs_insights(
        'filter scope.name = "strands.telemetry.tracer"\n'
        '| filter ispresent(attributes.gen_ai.server.time_to_first_token)\n'
        + svc_filter +
        '| stats count(*) as n,'
        ' pct(attributes.gen_ai.server.time_to_first_token, 95) as ttftP95,'
        ' pct(attributes.gen_ai.server.time_to_first_token, 99) as ttftP99,'
        ' sum(attributes.gen_ai.usage.input_tokens) as inTok,'
        ' sum(attributes.gen_ai.usage.output_tokens) as outTok'
        ' by bin(1d) as day\n'
        '| sort day asc\n'
        '| limit 100',
        days,
        deadline,
    )
    if daily is None:
        return {"available": False, "reason": "spans query failed or unavailable"}

    trend = [{
        "day": r.get("day", ""),
        "n": int(_num(r, "n")),
        "ttftP95": _num(r, "ttftP95"),
        "ttftP99": _num(r, "ttftP99"),
        "inputTokens": int(_num(r, "inTok")),
        "outputTokens": int(_num(r, "outTok")),
    } for r in daily]

    overall = _run_logs_insights(
        'filter scope.name = "strands.telemetry.tracer"\n'
        '| filter ispresent(attributes.gen_ai.server.time_to_first_token)\n'
        + svc_filter +
        '| stats count(*) as n,'
        ' pct(attributes.gen_ai.server.time_to_first_token, 95) as ttftP95,'
        ' pct(attributes.gen_ai.server.time_to_first_token, 99) as ttftP99,'
        ' sum(attributes.gen_ai.usage.input_tokens) as inTok,'
        ' sum(attributes.gen_ai.usage.output_tokens) as outTok'
        ' by attributes.session.id as sessionId,'
        ' attributes.gen_ai.request.model as model,'
        ' resource.attributes.service.name as svc\n'
        '| limit 1000',
        days,
        deadline,
    ) or []

    attribution = _attribute(overall, dim)

    tot_in = sum(t["inputTokens"] for t in trend)
    tot_out = sum(t["outputTokens"] for t in trend)
    ttfts_p95 = [t["ttftP95"] for t in trend if t["ttftP95"]]
    ttfts_p99 = [t["ttftP99"] for t in trend if t["ttftP99"]]

    return {
        "available": bool(trend),
        "trend": trend,
        "attribution": attribution,
        "dim": dim,
        "totals": {
            "inputTokens": tot_in,
            "outputTokens": tot_out,
            "spans": sum(t["n"] for t in trend),
            # Max-of-daily-p95 is an upper envelope, not a true window
            # percentile — Logs Insights can't merge percentiles across bins.
            "ttftP95Ms": max(ttfts_p95) if ttfts_p95 else None,
            "ttftP99Ms": max(ttfts_p99) if ttfts_p99 else None,
        },
    }


def _session_to_email():
    """Map runtime sessionId → userId (email) from the sessions table."""
    mapping = {}
    try:
        t = _table(RUNTIME_SESSIONS_TABLE_NAME)
        kwargs = {}
        while True:
            resp = t.scan(**kwargs)
            for item in resp.get("Items", []):
                sid = item.get("sessionId")
                if sid:
                    mapping[sid] = item.get("userId", "")
            token = resp.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
    except Exception as e:  # noqa: BLE001
        logger.warning("runtime-sessions scan failed: %s", e)
    return mapping


def _tenant_modes():
    """Map email → entryEnvironment mode from the tenant_env rows."""
    modes = {}
    try:
        resp = _table().query(
            KeyConditionExpression=Key("userId").eq("__global__")
            & Key("skillName").begins_with("__tenant_env_")
        )
        for item in resp.get("Items", []):
            sk = item.get("skillName", "")
            email = sk[len("__tenant_env_"):-len("__")] if sk.endswith("__") else ""
            if email:
                modes[email] = item.get("mode", "default")
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant_env query failed: %s", e)
    return modes


def _attribute(rows, dim):
    """Roll per-session token sums up to the requested dimension.

    dim=user   → Cognito email (via the runtime-sessions table)
    dim=tenant → entryEnvironment mode from tenant_env, else "default"
    dim=agent  → the runtime that emitted the span (its service.name), with the
                 model id kept alongside for context

    `dim=agent` used to bucket by model id, which answered "which model burned
    tokens", not "which agent". With A2A specialist runtimes running their own
    models that distinction matters: the text agent and a delegate can share a
    model, and one runtime can change models between versions.

    There is no per-FEATURE attribution: the agent emits no feature-level
    telemetry, so that dimension is deliberately absent (see spec §2.3).
    """
    if dim == "agent":
        buckets = {}
        for r in rows:
            # Fall back to the model id for spans predating the svc field.
            key = r.get("svc") or r.get("model") or "unknown"
            b = buckets.setdefault(key, {"key": key, "inputTokens": 0,
                                         "outputTokens": 0, "sessions": 0,
                                         "models": []})
            b["inputTokens"] += int(_num(r, "inTok"))
            b["outputTokens"] += int(_num(r, "outTok"))
            b["sessions"] += 1
            model = r.get("model")
            if model and model not in b["models"]:
                b["models"].append(model)
        return sorted(buckets.values(),
                      key=lambda b: -(b["inputTokens"] + b["outputTokens"]))[:20]

    sess_map = _session_to_email()
    tenant_map = _tenant_modes() if dim == "tenant" else {}
    buckets = {}
    for r in rows:
        email = sess_map.get(r.get("sessionId", ""), "")
        if dim == "tenant":
            key = tenant_map.get(email, "default") if email else "default"
        else:
            key = email or "(unattributed)"
        b = buckets.setdefault(key, {"key": key, "inputTokens": 0,
                                     "outputTokens": 0, "sessions": 0})
        b["inputTokens"] += int(_num(r, "inTok"))
        b["outputTokens"] += int(_num(r, "outTok"))
        b["sessions"] += 1
    return sorted(buckets.values(),
                  key=lambda b: -(b["inputTokens"] + b["outputTokens"]))[:20]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def get_dashboard(event):
    """GET /dashboard?range=24h|7d|30d[&part=spans][&dim=user|tenant|agent]"""
    qs = event.get("queryStringParameters") or {}
    rng = qs.get("range", "7d")
    if rng not in RANGES:
        return _resp(400, {"error": f"range must be one of {list(RANGES)}"})
    part = qs.get("part", "fast")
    if part not in ("fast", "spans"):
        return _resp(400, {"error": "part must be 'fast' or 'spans'"})
    dim = qs.get("dim", "user")
    if dim not in DIMS:
        return _resp(400, {"error": f"dim must be one of {list(DIMS)}"})

    days = RANGES[rng]
    # dim only affects the spans part; keep it out of the fast cache key so
    # switching dimensions doesn't needlessly miss the fast cache.
    cache_key = f"{rng}#{part}" + (f"#{dim}" if part == "spans" else "")
    if qs.get("refresh") != "1":
        cached = _cache_get(cache_key)
        if cached:
            return _resp(200, cached)

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if part == "spans":
        payload = {
            "range": rng,
            "part": "spans",
            "generatedAt": now_iso,
            "cached": False,
            "spans": _fetch_spans(days, dim),
        }
    else:
        payload = {
            "range": rng,
            "part": "fast",
            "generatedAt": now_iso,
            "cached": False,
            "health": _fetch_health(days),
            "evaluations": _fetch_evaluations(days),
            "abComparison": _fetch_ab_comparison(days),
            "release": _fetch_release(),
        }

    _cache_put(cache_key, payload)
    return _resp(200, payload)
