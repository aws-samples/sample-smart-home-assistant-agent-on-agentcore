#!/usr/bin/env python3
"""Measure the orchestrator's latency, token cost and routing, per prompt.

This is the measuring instrument the latency work is judged against. Spec 5 puts
it first for one reason: S2 (context trimming), S3 (routing cost) and S4
(parallel delegation) all move the same number, so without a fixed method the
three cannot be attributed afterwards — only claimed.

What it records, per prompt per repeat:

  wall        the client's own round trip, end to end
  server      the container's own `POST /invocations` span
  platform    wall - server: time the request spent outside our code
  ttftFirst   time to first token of the FIRST model call in the turn
  llmTime     summed duration of every `chat` span
  toolTime    summed duration of every `execute_tool` span
  inTok/outTok  summed over the turn's `chat` spans
  cycles      how many event-loop cycles the turn took
  tools       which tools actually ran, from `gen_ai.tool.name`
  delegated   whether any of those was an `a2a_*` tool

`server - (llmTime + toolTime)` is the harness's own overhead inside the
container: prompt assembly, skill loading, the Gateway/MCP handshake, Memory
retrieval, the per-record Registry reads that build the A2A tools. It is a
first-class number because S2-S4 are meant to shrink it and it is invisible in
any single span.

`platform` is a first-class number for a different reason: it is NOT ours, and
it is large. Measured on this runtime, a request whose session id has never been
seen spends ~7-8s in AgentCore before our container is entered, and a request
reusing a live session spends ~0.2s. So a 16s "fast path" is about 8s of agent
work behind 8s of session creation. Any optimisation report that quotes only the
client wall on fresh sessions is measuring the platform's cold start and
crediting it to the harness — in both directions. Keeping the two apart is the
whole reason this column exists.

Both are reported because both are true: a user who opens the chatbot and sends
one message really does wait for the cold start, and their second message really
does not.

Why spans and not the reply text
--------------------------------
The `⟦A2A:…⟧` marker is on the SUB-AGENT's reply, and the orchestrator
summarises rather than pasting it through, so a correctly delegated turn usually
carries no marker at all. Scanning text reported six false misses twice over
(see docs/architecture-and-design.md §9.13). `gen_ai.tool.name` is the only
direct record of which tool ran.

Where the spans live
--------------------
NOT `aws/spans`. That account-wide group stopped receiving this project's spans
on 2026-08-05 (the sub-agents on 2026-08-09) when AgentCore Runtime moved trace
export into each runtime's OWN log group, `spans` stream. The handover is clean —
the runtime-local stream starts 02:28 the same day `aws/spans` stops at 02:12 —
so this is a platform change, not a broken pipe, and nothing failed loudly.
Querying the per-runtime group also scopes the query to this project by
construction, where `aws/spans` needed a service.name allowlist to keep a
stranger's traffic out of the numbers.

Traps this script exists to avoid re-learning
---------------------------------------------
  - `userId` must be present and must be the EMAIL. It becomes `actor_id`, which
    scopes skills, prompts and memory. A2A grants no longer come from it — they are
    read from the caller's `cognito:groups` claim — so a run whose token carries no
    grant registers NOT ONE `a2a_*` tool and every delegation "fails" for a reason
    unrelated to the prompt.
  - One fresh session id per invocation. A shared session lets an earlier answer
    bias the next routing decision, and it also makes the spans of two prompts
    indistinguishable — the session id is the only join key. It also costs the
    ~8s session creation above, which is why `platform` is reported separately
    rather than being allowed to swamp the number under test.
  - Spans take ~30s to arrive. Every prompt is invoked first and queried after,
    rather than polling per prompt, so a slow export does not become a timeout.

Usage:
  ./venv/bin/python scripts/measure-baseline.py                # 3 repeats
  ./venv/bin/python scripts/measure-baseline.py --repeats 1    # a quick look
  ./venv/bin/python scripts/measure-baseline.py --warm         # reuse one session
  ./venv/bin/python scripts/measure-baseline.py --label after-s2
  ./venv/bin/python scripts/measure-baseline.py --compare docs/measurements/baseline-....json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "us-west-2")
REPO = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO / "docs" / "measurements"

# Ten prompts, five each side of the routing decision. Held fixed on purpose:
# a comparison is only meaningful against the same inputs, so changing this list
# invalidates every archived baseline. The five delegating prompts cover five
# DIFFERENT sub-agents, because a single specialist's own latency would
# otherwise dominate the delegated mean.
#
# All ten are READ-ONLY. A measurement run happens repeatedly and unattended; a
# prompt that turned on a light would leave the user's house in a state they did
# not ask for, three times per run.
PROMPTS: list[tuple[str, str]] = [
    ("fast:state", "Is the bedroom light on right now?"),
    ("fast:sensor", "What is the living room temperature right now?"),
    ("fast:history", "What was the average humidity over the last 6 hours?"),
    ("fast:list", "Which devices do I have in the living room?"),
    ("fast:nav", "Open the automation page"),
    ("deleg:kb", "What animation modes does the LED matrix support?"),
    ("deleg:security", "What is my biggest smart-home security gap?"),
    ("deleg:energy", "How much could I save by dimming the LEDs at night?"),
    ("deleg:maint", "When should I replace the air purifier filter?"),
    ("deleg:task", "What automations do I have saved?"),
]

SPAN_SCOPE = "strands.telemetry.tracer"
# The request-level span lives under a different instrumentation scope than the
# agent-level ones, and it is the only place the container's own view of the
# request duration appears.
HTTP_SCOPE = "opentelemetry.instrumentation.starlette"
# How long to wait for the OTel batch exporter to flush before querying. Measured
# at ~30s for the last turn of a run; 60 leaves margin without being a long wait.
SPAN_SETTLE_SECONDS = 60


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def _load_state() -> tuple[dict, dict]:
    st = json.loads((REPO / "agentcore-state.json").read_text())
    out = json.loads((REPO / "cdk-outputs.json").read_text())
    return st, out[next(iter(out))]


def _id_token(outputs: dict) -> tuple[str, str]:
    """Sign in as the admin and return (idToken, actor id).

    The actor id is the EMAIL claim. A2A grants are keyed on it; sending the sub
    reads a row that does not exist, registers no `a2a_*` tool, and makes every
    delegating prompt look like a routing bug.
    """
    cog = boto3.client("cognito-idp", region_name=REGION)
    tok = cog.initiate_auth(
        ClientId=outputs["UserPoolClientId"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": outputs["AdminUsername"],
            "PASSWORD": outputs["AdminPassword"],
        },
    )["AuthenticationResult"]["IdToken"]
    payload = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return tok, claims.get("email") or claims["sub"]


def _invoke(runtime_arn: str, token: str, actor: str, prompt: str,
            session_id: str) -> tuple[float, str]:
    """One turn over HTTPS with SigV4. Returns (wall seconds, reply text).

    SigV4 over HTTPS rather than boto3 because the user's idToken travels in
    X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken and InvokeAgentRuntime
    models no parameter for it. Without that header the agent has no identity to
    open the Gateway with, every tool call 401s and the runtime returns a bare
    500.
    """
    url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
           f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")
    body = json.dumps({"prompt": prompt, "userId": actor}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken": token,
    }
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    t0 = time.time()
    with urllib.request.urlopen(
        urllib.request.Request(url, data=body, headers=dict(req.headers)),
        timeout=300,
    ) as resp:
        raw = resp.read().decode("utf-8", "replace")
    wall = time.time() - t0
    try:
        reply = json.loads(raw).get("response", raw)
    except json.JSONDecodeError:
        reply = raw
    return wall, reply


def _session_id() -> str:
    """A session id for one measured turn.

    Runtime requires at least 33 characters, so two hex UUIDs are concatenated
    and truncated. The prompt label is deliberately NOT encoded here: the
    archive already maps session id to label, and putting the prompt set into
    log data would make it grep-able by anyone with log access.
    """
    return f"mb-{uuid.uuid4().hex}{uuid.uuid4().hex}"[:64]


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------

def _span_log_group(runtime_arn: str) -> str:
    """The runtime's own log group, which is where spans go now.

    See the module docstring: `aws/spans` stopped receiving this project's spans
    when the platform moved trace export per-runtime.
    """
    runtime_id = runtime_arn.split("/")[-1]
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def _query(log_group: str, query: str, start: int, end: int,
           budget: float = 90.0) -> list[dict]:
    logs = boto3.client("logs", region_name=REGION)
    qid = logs.start_query(
        logGroupNames=[log_group], startTime=start, endTime=end,
        queryString=query,
    )["queryId"]
    deadline = time.time() + budget
    while time.time() < deadline:
        res = logs.get_query_results(queryId=qid)
        status = res.get("status")
        if status == "Complete":
            return [{f["field"]: f["value"] for f in row} for row in res["results"]]
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Logs Insights query ended {status}")
        time.sleep(1.5)
    logs.stop_query(queryId=qid)
    raise RuntimeError("Logs Insights query exceeded its budget")


def _fetch_spans(log_group: str, start: int, end: int) -> list[dict]:
    """Every span this measurement reads, one row each.

    Fetched as rows rather than pre-aggregated by `stats`: the per-turn shape
    (how many cycles, which tool in which order) is the interesting part, and a
    `stats` query would flatten exactly that away.

    Two scopes, not one. The agent-level spans (`chat`, `execute_tool`,
    `execute_event_loop_cycle`) come from Strands' own tracer; the request-level
    `POST /invocations` span comes from the FastAPI/starlette instrumentation the
    runtime installs. Filtering on the Strands scope alone silently drops it, and
    with it the only measurement of how much of the wall clock was ours.
    """
    scopes = f'"{SPAN_SCOPE}", "{HTTP_SCOPE}"'
    return _query(
        log_group,
        'fields attributes.session.id as sid, name,'
        ' attributes.gen_ai.tool.name as tool,'
        ' attributes.gen_ai.server.time_to_first_token as ttft,'
        ' attributes.gen_ai.usage.input_tokens as inTok,'
        ' attributes.gen_ai.usage.output_tokens as outTok,'
        ' attributes.gen_ai.request.model as model,'
        ' durationNano, startTimeUnixNano as st\n'
        f'| filter scope.name in [{scopes}]\n'
        '| filter ispresent(sid)\n'
        '| sort st asc\n'
        '| limit 10000',
        start, end,
    )


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _summarise_turn(spans: list[dict], near_nano: int = 0) -> dict:
    """Collapse one turn's spans into the measured row.

    ``near_nano`` is roughly when the client sent the request, and is only needed
    in --warm mode, where one session id covers every turn and so cannot separate
    them on its own.

    The turn is then bounded by its own `POST /invocations` span rather than by the
    client's clock. That span is the request, measured by the runtime, so it needs
    no slack — and slack is what broke the first two attempts. Turns run
    back-to-back with no idle gap between them (turn N ends and turn N+1 starts
    inside the same second), so even 2s of tolerance pulled in a neighbour. The
    result was not an error but a plausible-looking table: `server` exceeded the
    client's own `wall`, which is impossible, and `_impossible()` now refuses it.
    """
    posts = sorted((s for s in spans if s.get("name") == "POST /invocations"),
                   key=lambda s: _num(s, "st"))
    if near_nano and posts:
        post = min(posts, key=lambda s: abs(_num(s, "st") - near_nano))
        lo = _num(post, "st")
        hi = lo + _num(post, "durationNano")
        posts = [post]
        spans = [s for s in spans if lo <= _num(s, "st") <= hi]
    # Sorted by start time, not by the order the query returned them: `ttftFirst`
    # below means the first model call of the turn, and Logs Insights row order is
    # not a guarantee to rely on for that.
    chats = sorted((s for s in spans if s.get("name") == "chat"),
                   key=lambda s: _num(s, "st"))
    tool_spans = sorted((s for s in spans
                         if (s.get("name") or "").startswith("execute_tool")),
                        key=lambda s: _num(s, "st"))
    cycles = [s for s in spans if s.get("name") == "execute_event_loop_cycle"]
    tools = [s.get("tool") or (s.get("name") or "").removeprefix("execute_tool ").strip()
             for s in tool_spans]
    tools = [t for t in tools if t]
    return {
        # The container's own view of the request. Subtracted from the client's
        # wall it gives the platform's share, which on a fresh session is the
        # single largest term in the whole measurement.
        "server": round(max((_num(s, "durationNano") for s in posts), default=0.0) / 1e9, 2),
        # The FIRST chat's TTFT, not the mean: this is the number a streaming UI
        # would show, and averaging it across a multi-cycle turn describes
        # nothing a user experiences.
        "ttftFirst": (_num(chats[0], "ttft") if chats else 0.0),
        "llmTime": round(sum(_num(s, "durationNano") for s in chats) / 1e9, 2),
        "toolTime": round(sum(_num(s, "durationNano") for s in tool_spans) / 1e9, 2),
        "inTok": int(sum(_num(s, "inTok") for s in chats)),
        "outTok": int(sum(_num(s, "outTok") for s in chats)),
        "chats": len(chats),
        "cycles": len(cycles),
        "tools": tools,
        "delegated": [t for t in tools if t.startswith("a2a_")],
        "model": next((s.get("model") for s in chats if s.get("model")), ""),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _agg(rows: list[dict], key: str) -> dict:
    """Mean / median / spread for one measured field.

    The spread is reported, not smoothed away: it decides how large a later
    improvement has to be before it counts as one rather than as noise. A run
    whose baseline varies 20% cannot certify a 10% win.
    """
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0}
    mean = statistics.fmean(vals)
    return {
        "n": len(vals),
        "mean": round(mean, 2),
        "median": round(statistics.median(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        # Coefficient of variation, in percent — comparable across fields with
        # very different magnitudes (a 2s spread means something different for
        # TTFT than for a 30s wall).
        "cvPct": round((statistics.stdev(vals) / mean * 100) if len(vals) > 1 and mean else 0.0, 1),
    }


def _derive(rows: list[dict]) -> None:
    """Fill the columns computed from the raw measurements. Idempotent."""
    for r in rows:
        r["platform"] = round(r["wall"] - r.get("server", 0.0), 2)
        # Harness overhead INSIDE the container. Measured against `server`, not
        # `wall`: charging the platform's session creation to our own prompt
        # assembly would make every optimisation here look futile.
        r["harness"] = round(r.get("server", 0.0) - r["llmTime"] - r["toolTime"], 2)
        r["ttftSec"] = round(r.get("ttftFirst", 0.0) / 1000, 2)


def _impossible(rows: list[dict]) -> list[str]:
    """Rows whose arithmetic proves the span-to-turn join is wrong.

    A turn cannot spend longer inside the container than the client spent waiting
    for it, and its model and tool time cannot exceed the request that contains
    them. When the warm-mode window was open-ended, every turn absorbed its
    successors' spans and produced exactly this — but the numbers still looked
    like numbers, ordered and plausible, so the table read as a result rather
    than as a bug. Checked rather than trusted for that reason.
    """
    bad = []
    for r in rows:
        if r["platform"] < -1.0:
            bad.append(f"{r['label']} r{r['repeat']}: server {r['server']}s > "
                       f"wall {r['wall']}s (platform {r['platform']}s)")
        elif r["harness"] < -1.0:
            bad.append(f"{r['label']} r{r['repeat']}: llm+tool "
                       f"{r['llmTime'] + r['toolTime']:.1f}s > server "
                       f"{r['server']}s (harness {r['harness']}s)")
    return bad


def _print_table(rows: list[dict]) -> None:
    _derive(rows)
    print()
    print(f"{'prompt':16} {'rep':>3} {'wall':>7} {'platf':>7} {'server':>7} "
          f"{'ttft':>7} {'llm':>7} {'tool':>7} {'harn':>7} {'in':>7} {'out':>6} "
          f"{'cyc':>4}  tools")
    print("-" * 124)
    for r in rows:
        tools = ",".join(t.replace("a2a_", "→") for t in r["tools"]) or "-"
        print(f"{r['label']:16} {r['repeat']:>3} {r['wall']:>6.1f}s "
              f"{r['platform']:>6.1f}s {r['server']:>6.1f}s "
              f"{r['ttftSec']:>6.2f}s {r['llmTime']:>6.1f}s {r['toolTime']:>6.1f}s "
              f"{r['harness']:>6.1f}s {r['inTok']:>7} {r['outTok']:>6} "
              f"{r['cycles']:>4}  {tools[:34]}")


def _print_summary(rows: list[dict]) -> None:
    _derive(rows)
    fast = [r for r in rows if r["label"].startswith("fast:")]
    deleg = [r for r in rows if r["label"].startswith("deleg:")]
    print()
    print(f"{'group':10} {'n':>3} {'wall':>8} {'cv%':>6} {'platform':>9} "
          f"{'server':>8} {'ttft':>7} {'llm':>7} {'tool':>7} {'harness':>8} "
          f"{'in tok':>8} {'out tok':>8}")
    print("-" * 106)
    for name, group in (("fast", fast), ("delegated", deleg), ("all", rows)):
        if not group:
            continue
        w = _agg(group, "wall")
        print(f"{name:10} {len(group):>3} {w['mean']:>7.1f}s {w['cvPct']:>5.1f}% "
              f"{_agg(group,'platform')['mean']:>8.1f}s "
              f"{_agg(group,'server')['mean']:>7.1f}s "
              f"{_agg(group,'ttftSec')['mean']:>6.2f}s "
              f"{_agg(group,'llmTime')['mean']:>6.1f}s "
              f"{_agg(group,'toolTime')['mean']:>6.1f}s "
              f"{_agg(group,'harness')['mean']:>7.1f}s "
              f"{_agg(group,'inTok')['mean']:>8.0f} {_agg(group,'outTok')['mean']:>8.0f}")

    routed = sum(1 for r in deleg if r["delegated"])
    print(f"\nrouting: {routed}/{len(deleg)} delegating prompts reached an a2a_* tool")
    missed = sorted({r["label"] for r in deleg if not r["delegated"]})
    if missed:
        # Not a warning in passing: a delegating prompt the orchestrator answered
        # itself is a routing regression, and it is invisible in the reply text.
        print(f"  NOT DELEGATED: {missed}")
    stray = sorted({t for r in fast for t in r["delegated"]})
    if stray:
        print(f"  fast-path prompts that delegated anyway: {stray}")

    # Stated rather than left for the reader to divide: on a cold session the
    # platform's share is usually the largest single term, and a report that
    # quotes wall alone reads as though the harness were responsible for it.
    plat = _agg(rows, "platform").get("mean", 0.0)
    wall = _agg(rows, "wall").get("mean", 0.0)
    if wall:
        print(f"\nof the {wall:.1f}s mean wall, {plat:.1f}s ({plat / wall * 100:.0f}%) "
              f"was outside the container "
              f"(session creation + AgentCore edge, not ours to optimise)")


def _compare(current: dict, previous: dict) -> None:
    print(f"\n=== vs {previous.get('label','?')} "
          f"({previous.get('startedAt','?')[:16]}) ===")
    prev_rows = previous.get("rows", [])
    _derive(prev_rows)
    _derive(current["rows"])
    if previous.get("warm") != current.get("warm"):
        # A warm run against a cold baseline would show an ~8s "improvement" that
        # no code change produced. Refusing to print the table is the point.
        print("  REFUSING to compare: one run is warm and the other cold. "
              "The session-creation difference alone is ~8s, which would swamp "
              "any real change. Re-run with matching --warm.")
        return
    print(f"{'group':10} {'metric':10} {'before':>10} {'after':>10} {'delta':>10}")
    print("-" * 54)
    for group in ("fast", "deleg"):
        for metric, unit in (("wall", "s"), ("server", "s"), ("harness", "s"),
                             ("ttftSec", "s"), ("toolTime", "s"),
                             ("inTok", ""), ("outTok", "")):
            a = [r for r in current["rows"] if r["label"].startswith(group)]
            b = [r for r in prev_rows if r["label"].startswith(group)]
            before, after = _agg(b, metric).get("mean"), _agg(a, metric).get("mean")
            if before is None or after is None:
                continue
            pct = ((after - before) / before * 100) if before else 0.0
            print(f"{group:10} {metric:10} {before:>9.2f}{unit} {after:>9.2f}{unit} "
                  f"{pct:>+9.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3,
                    help="invocations per prompt (default 3)")
    ap.add_argument("--label", default="baseline",
                    help="archive label, e.g. 'after-s2'")
    ap.add_argument("--compare", default="",
                    help="path to a previous archive to diff against")
    ap.add_argument("--warm", action="store_true",
                    help="reuse ONE session for every prompt, so the ~8s "
                         "AgentCore session creation is paid once instead of "
                         "per prompt. Measures the returning user, not the "
                         "first message.")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()

    st, outputs = _load_state()
    runtime_arn = st["runtimeArn"]
    token, actor = _id_token(outputs)
    log_group = _span_log_group(runtime_arn)
    print(f"runtime : {runtime_arn.split('/')[-1]}")
    print(f"actor   : {actor}")
    print(f"spans   : {log_group}")
    print(f"prompts : {len(PROMPTS)} x {args.repeats} repeats "
          f"({'warm — one shared session' if args.warm else 'cold — fresh session each'})")

    # In warm mode the shared session stops being a unique join key, so each turn
    # is bounded by the clock instead. Recorded per invocation rather than derived
    # afterwards, because the gap between two turns is not observable in the spans.
    shared_session = _session_id() if args.warm else ""
    started = time.time()
    invocations: list[dict] = []
    for repeat in range(1, args.repeats + 1):
        for label, prompt in PROMPTS:
            sid = shared_session or _session_id()
            turn_start = time.time()
            try:
                wall, reply = _invoke(runtime_arn, token, actor, prompt, sid)
            except Exception as exc:  # noqa: BLE001
                detail = (exc.read().decode()[:160] if hasattr(exc, "read")
                          else str(exc)[:160])
                print(f"  {label:16} r{repeat} ERROR {type(exc).__name__} {detail}",
                      flush=True)
                continue
            print(f"  {label:16} r{repeat} {wall:6.1f}s  {reply[:70]!r}", flush=True)
            invocations.append({
                "label": label, "prompt": prompt, "repeat": repeat,
                "sessionId": sid, "wall": round(wall, 2),
                "replyChars": len(reply),
                # When the client sent this request. In warm mode it picks which
                # `POST /invocations` span is this turn's; the span's own duration
                # then bounds it, so no client-side end time is needed.
                "turnStartNano": int(turn_start * 1e9),
            })

    if not invocations:
        print("no successful invocations; nothing to measure")
        return 1

    print(f"\nwaiting {SPAN_SETTLE_SECONDS}s for spans to export...", flush=True)
    time.sleep(SPAN_SETTLE_SECONDS)

    # Widen the window either side: the exporter stamps spans with their own
    # start time, which precedes the first invocation for a turn already in
    # flight, and clock skew is not worth arguing with.
    spans = _fetch_spans(log_group, int(started) - 120, int(time.time()) + 60)
    by_session: dict[str, list[dict]] = {}
    for row in spans:
        by_session.setdefault(row.get("sid", ""), []).append(row)

    rows: list[dict] = []
    for inv in invocations:
        turn = by_session.get(inv["sessionId"], [])
        if not turn:
            # Loud, because a silently missing turn would quietly bias every
            # aggregate towards whichever prompts did export.
            print(f"  WARNING: no spans for {inv['label']} r{inv['repeat']} "
                  f"(session {inv['sessionId'][:20]}...)")
            continue
        # Only warm mode needs a hint: there one session id covers every turn, so
        # the turn is picked out by the `POST /invocations` span closest to when
        # the client sent this request.
        summary = _summarise_turn(
            turn, near_nano=inv["turnStartNano"] if args.warm else 0)
        if args.warm and not summary["chats"]:
            print(f"  WARNING: no spans inside the window for {inv['label']} "
                  f"r{inv['repeat']}; dropped")
            continue
        rows.append({**inv, **summary})

    _print_table(rows)
    _print_summary(rows)

    _derive(rows)
    bad = _impossible(rows)
    if bad:
        print(f"\nIMPOSSIBLE MEASUREMENTS ({len(bad)}) — the span-to-turn join is "
              f"wrong, so nothing above is usable:")
        for line in bad[:10]:
            print(f"  {line}")
        # Not archived. A file that looks like a baseline is worse than no file:
        # a later --compare against it would report an improvement no code change
        # produced.
        print("\nnot archiving; fix the join first")
        return 1

    payload = {
        "label": args.label,
        "startedAt": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "runtimeId": runtime_arn.split("/")[-1],
        "logGroup": log_group,
        "repeats": args.repeats,
        # Recorded so `--compare` can refuse a warm-against-cold diff, where the
        # session-creation difference alone would read as an ~8s improvement.
        "warm": bool(args.warm),
        "promptSet": [p[0] for p in PROMPTS],
        "rows": rows,
    }

    if args.compare:
        _compare(payload, json.loads(Path(args.compare).read_text()))

    if not args.no_archive:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(started, timezone.utc).strftime("%Y%m%d-%H%M")
        suffix = "-warm" if args.warm else ""
        path = ARCHIVE_DIR / f"{args.label}{suffix}-{stamp}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\narchived: {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
