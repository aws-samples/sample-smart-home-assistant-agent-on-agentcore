#!/usr/bin/env python3
"""
Force a cold start on an AgentCore Runtime.

Two-step protocol, order matters:
  1. Stop every known active runtime session on this runtime.
  2. Bump an env-var nonce via UpdateAgentRuntime.

Why both steps?

UpdateAgentRuntime alone is *not* enough. Stopping sessions first guarantees
the control-plane sees zero live sessions when it cycles containers, so the
next /ws invoke can't be routed to a lingering warm container that was still
serving an idle session. Bumping the nonce alone is *not* enough either:
sessions pinned to old containers keep them warm until idle-timeout (minutes),
and the next invoke from the same client can be sticky-routed to that warm
container, masking the cold-start we're trying to measure.

Usage:
  force-cold.py --region <r> --runtime-id <rt-id> [--skills-table smarthome-skills]

Session IDs to stop are discovered from the DynamoDB skills table (same
source that setup-agentcore.py uses during redeploy). Rows where
skillName="__session_text__" map to the text runtime; "__session_voice__"
rows map to the voice runtime. We stop both kinds — this script doesn't
try to tell text-from-voice. If the runtime doesn't know a session, the
API returns ResourceNotFoundException which we swallow.
"""

import argparse
import sys
import time
import boto3


def stop_all_sessions(
    dataplane,
    ddb,
    skills_table: str,
    runtime_arn: str,
) -> int:
    """Scan DDB for session rows and call stop_runtime_session on each.

    Returns: number of sessions successfully stopped (ResourceNotFound and
    already-expired are treated as success).
    """
    table = ddb.Table(skills_table)
    stopped = 0
    for kind in ("__session_text__", "__session_voice__"):
        try:
            items = table.scan(
                FilterExpression="skillName = :s",
                ExpressionAttributeValues={":s": kind},
                ProjectionExpression="sessionId",
            ).get("Items", [])
        except Exception as e:
            print(f"  (skipping {kind} scan: {e})", flush=True)
            continue
        for item in items:
            sid = item.get("sessionId")
            if not sid:
                continue
            try:
                dataplane.stop_runtime_session(
                    agentRuntimeArn=runtime_arn,
                    runtimeSessionId=sid,
                )
                stopped += 1
            except dataplane.exceptions.ResourceNotFoundException:
                # Session already terminated/idle-expired on the runtime —
                # we still wanted it gone, so count it as success.
                stopped += 1
            except Exception as e:
                # Don't abort the cold run for one bad session; log and move on.
                print(f"  warn: could not stop session {sid}: {e}", flush=True)
    return stopped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--runtime-id", required=True, dest="runtime_id")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--skills-table", default="smarthome-skills")
    args = p.parse_args()

    ac = boto3.client("bedrock-agentcore-control", region_name=args.region)
    dataplane = boto3.client("bedrock-agentcore", region_name=args.region)
    ddb = boto3.resource("dynamodb", region_name=args.region)

    rt = ac.get_agent_runtime(agentRuntimeId=args.runtime_id)
    runtime_arn = rt["agentRuntimeArn"]

    # Step 1: stop live sessions so no warm container can serve the next invoke.
    print(f"Stopping sessions on {args.runtime_id}...", flush=True)
    stopped = stop_all_sessions(dataplane, ddb, args.skills_table, runtime_arn)
    print(f"  Stopped {stopped} session(s)", flush=True)

    # Step 2: bump nonce. Rolls every container version forward; combined
    # with the stop above, the very next invoke is guaranteed to hit a newly
    # provisioned container.
    env = dict(rt.get("environmentVariables") or {})
    env["LATENCY_PROBE_COLD_NONCE"] = str(int(time.time() * 1000))

    update_kwargs = {
        "agentRuntimeId": args.runtime_id,
        "environmentVariables": env,
    }
    # These fields must round-trip unchanged on update, or the API complains
    # about missing required config — OR silently drops them (e.g.
    # requestHeaderAllowlist, which strips MCP auth headers from subsequent
    # invocations and causes a 401 storm).
    for k in (
        "agentRuntimeArtifact",
        "roleArn",
        "networkConfiguration",
        "protocolConfiguration",
        "authorizerConfiguration",
    ):
        if k in rt and rt[k] is not None:
            update_kwargs[k] = rt[k]

    # requestHeaderConfiguration round-trip. boto3 sometimes returns
    # requestHeaderAllowlist at top level and sometimes nested under
    # requestHeaderConfiguration — depends on the AgentCore API version.
    # Check both. Drop the nested form into the update call's expected
    # shape. If we skip this and pass nothing, UpdateAgentRuntime silently
    # drops the allowlist and custom auth headers stop reaching the agent
    # → MCP gateway 401 storm.
    rhc = rt.get("requestHeaderConfiguration") or {}
    allowlist = rhc.get("requestHeaderAllowlist") or rt.get("requestHeaderAllowlist")
    if allowlist:
        update_kwargs["requestHeaderConfiguration"] = {
            "requestHeaderAllowlist": allowlist,
        }

    print(f"Bumping nonce on {args.runtime_id}...", flush=True)
    ac.update_agent_runtime(**update_kwargs)

    # Poll until READY. UpdateAgentRuntime transitions to UPDATING then READY
    # once the new deployment is live (~60-90s empirically; usually ~15s).
    t0 = time.time()
    while time.time() - t0 < args.timeout:
        status = ac.get_agent_runtime(agentRuntimeId=args.runtime_id)["status"]
        if status == "READY":
            elapsed = time.time() - t0
            print(f"READY in {elapsed:.1f}s", flush=True)
            return 0
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            print(f"ERROR: runtime status = {status}", file=sys.stderr)
            return 2
        time.sleep(5)

    print(f"TIMEOUT after {args.timeout}s", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
