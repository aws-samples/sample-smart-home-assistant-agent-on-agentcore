"""Strands tool `execute_python` — run Python in an AgentCore Code Interpreter.

The LLM calls execute_python(code=..., title=...). We:
  1. Reuse (or lazily start) a per-agent-session Code Interpreter sandbox so
     variables/imports persist across blocks within a turn (clearContext=False).
  2. Append a step (status=running) to smarthome-code-sessions so the chatbot's
     polling endpoint can render the block live.
  3. invoke("executeCode", ...) and iterate response["stream"], accumulating
     stdout/stderr from each event's structuredContent and updating the DDB row
     as deltas arrive.
  4. Detect any chart files the code saved (matplotlib savefig), copy them into
     the text agent's session workspace (/mnt/workspace/<sid>/code/) so they
     surface inline in the CodeInterpreter tab and in the Files tab.
  5. Mark the step idle/failed and return a short text summary to the model.

User identity and agent session id are injected at agent.py registration time
(closure over actor_id / session_id) — the LLM cannot override them.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

CODE_MAX_LEN = 16000
STDOUT_CAP = 10000          # per-step stored stdout/stderr cap (bytes)
SESSION_TIMEOUT_S = 900     # AgentCore CI idle timeout (15 min)
# A new execute_python call more than this many seconds after the previous one
# is treated as a fresh "run" — new row id, cleared steps, bumped startedAt —
# so the chatbot's per-turn polling filter surfaces it. Within a turn the
# model's blocks fire seconds apart and accumulate into one row.
NEW_RUN_GAP_S = 90
TABLE_ENV = "CODE_SESSIONS_TABLE_NAME"
DEFAULT_TABLE = "smarthome-code-sessions"
REGION_ENV = "AWS_REGION"
IDENTIFIER_ENV = "AGENTCORE_CODE_INTERPRETER_IDENTIFIER"
DEFAULT_IDENTIFIER = "aws.codeinterpreter.v1"

# Per-process cache: agent_session_id -> _Run. The runtime keeps the process
# warm across turns of the same session, so the sandbox (and its variable
# state) is reused until the AgentCore idle timeout reaps it.
_RUNS: dict[str, "_Run"] = {}
_ddb_resource = None


class _Run:
    """One logical demo run: a Code Interpreter client + its DDB row + steps."""

    def __init__(self, client, ddb_session_id: str):
        self.client = client
        self.ddb_session_id = ddb_session_id
        self.started_at = _now_iso()
        self.steps: list[dict] = []
        self.seen_charts: set[str] = set()
        self.last_ts = time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource(
            "dynamodb", region_name=os.environ.get(REGION_ENV, "us-west-2")
        )
    return _ddb_resource


def _table():
    return _ddb().Table(os.environ.get(TABLE_ENV, DEFAULT_TABLE))


def _build_code_client():
    """Factory for the AgentCore CodeInterpreter helper. Isolated so tests can
    monkeypatch the whole client without touching AWS SDK internals."""
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
    return CodeInterpreter(region=os.environ.get(REGION_ENV, "us-west-2"))


def _put_row(user_id: str, agent_session_id: str, run: "_Run", status: str,
             title: str, last_error: str = "") -> None:
    """Persist the current run (status + all steps) to DynamoDB. startedAt is
    bumped to now on every write so the chatbot's per-turn poll always surfaces
    an active run (the filter drops rows older than the turn's send time)."""
    run.started_at = _now_iso()
    item = {
        "userId": user_id,
        "sessionId": run.ddb_session_id,
        "agentSessionId": agent_session_id,
        "status": status,
        "title": title[:200],
        "startedAt": run.started_at,
        "steps": run.steps,
        "ttl": int(time.time()) + 3600,
    }
    if status != "running":
        item["endedAt"] = _now_iso()
    if last_error:
        item["lastError"] = last_error[:500]
    try:
        _table().put_item(Item=item)
    except Exception as e:
        logger.warning("code-session DDB write failed (non-fatal): %s", e)


def _get_run(user_id: str, agent_session_id: str) -> "_Run":
    """Return the active run for this session, starting a fresh one (new row /
    cleared steps) when none exists or the previous activity is stale."""
    run = _RUNS.get(agent_session_id)
    now = time.time()
    if run is None or (now - run.last_ts) > NEW_RUN_GAP_S:
        client = run.client if run is not None else None
        if client is None:
            client = _build_code_client()
            client.start(
                identifier=os.environ.get(IDENTIFIER_ENV, DEFAULT_IDENTIFIER),
                name=f"code-{agent_session_id[:20]}",
                session_timeout_seconds=SESSION_TIMEOUT_S,
            )
        run = _Run(client, ddb_session_id=f"code-{int(now)}-{os.urandom(3).hex()}")
        _RUNS[agent_session_id] = run
    run.last_ts = now
    return run


def _consume_stream(response: dict) -> tuple[str, str, int | None, str]:
    """Drain response['stream'], returning (stdout, stderr, exitCode, execTime).

    Each event carries result.structuredContent (stdout/stderr/exitCode/
    executionTime) and/or a content[] array. We prefer structuredContent and
    fall back to text content items so we still capture output if the shape
    varies between runtimes."""
    stdout, stderr = "", ""
    exit_code: int | None = None
    exec_time = ""
    for event in response.get("stream", []):
        result = event.get("result", {}) if isinstance(event, dict) else {}
        sc = result.get("structuredContent") or {}
        if "stdout" in sc:
            stdout += sc.get("stdout") or ""
        if "stderr" in sc:
            stderr += sc.get("stderr") or ""
        if sc.get("exitCode") is not None:
            try:
                exit_code = int(sc["exitCode"])
            except (TypeError, ValueError):
                exit_code = sc["exitCode"]
        if sc.get("executionTime") is not None:
            exec_time = str(sc["executionTime"])
        if not sc:
            for item in result.get("content", []) or []:
                if item.get("type") == "text" and item.get("text"):
                    stdout += item["text"]
        if result.get("isError"):
            exit_code = exit_code if exit_code not in (None, 0) else 1
    return stdout[-STDOUT_CAP:], stderr[-STDOUT_CAP:], exit_code, exec_time


def _collect_charts(run: "_Run", agent_session_id: str, step_index: int) -> list[str]:
    """Find chart files the just-run code saved, copy each new one into the
    agent session workspace, and return their paths relative to the session dir
    (e.g. "code/step-001-0.png") for the chatbot to load."""
    # Ask the sandbox (same context) for image files, newest first. We do NOT
    # record this as a user-visible step.
    scan = (
        "import os,glob,json as _j\n"
        "_imgs=sorted(set(glob.glob('**/*.png',recursive=True)"
        "+glob.glob('**/*.jpg',recursive=True)+glob.glob('**/*.jpeg',recursive=True)),"
        "key=lambda p:os.path.getmtime(p))\n"
        "print('__CHARTS__'+_j.dumps(_imgs))"
    )
    try:
        out, _, _, _ = _consume_stream(
            run.client.invoke("executeCode",
                              {"code": scan, "language": "python", "clearContext": False})
        )
    except Exception as e:
        logger.warning("chart scan failed: %s", e)
        return []

    paths: list[str] = []
    for line in out.splitlines():
        if line.startswith("__CHARTS__"):
            try:
                paths = json.loads(line[len("__CHARTS__"):])
            except json.JSONDecodeError:
                paths = []
            break

    new_paths = [p for p in paths if p not in run.seen_charts]
    if not new_paths:
        return []

    dest_dir = os.path.join(
        os.environ.get("AGENT_SESSION_ROOT", "/mnt/workspace"),
        agent_session_id, "code",
    )
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        logger.warning("could not create chart dir: %s", e)
        return []

    rel_paths: list[str] = []
    for n, p in enumerate(new_paths):
        try:
            data = run.client.download_file(p)
            if isinstance(data, str):
                data = data.encode("utf-8")
            ext = os.path.splitext(p)[1] or ".png"
            fname = f"step-{step_index:03d}-{n}{ext}"
            with open(os.path.join(dest_dir, fname), "wb") as f:
                f.write(data)
            run.seen_charts.add(p)
            rel_paths.append(f"code/{fname}")
        except Exception as e:
            logger.warning("chart copy failed for %s: %s", p, e)
    return rel_paths


def run_execute_python(code: str, title: str = "",
                       user_id: str = "default",
                       agent_session_id: str = "default") -> str:
    """Execute Python in a secure sandbox and stream the result to the panel.

    Raw implementation. `agent.py` wraps it in a @strands_tool closure that
    pins `user_id` and `agent_session_id` — keep those out of the LLM-facing
    signature so they cannot be forged.
    """
    if not isinstance(code, str) or not code.strip():
        return "execute_python failed: code is empty."
    if len(code) > CODE_MAX_LEN:
        return f"execute_python failed: code too long (max {CODE_MAX_LEN} chars)."

    user_id = user_id or "default"
    agent_session_id = agent_session_id or "default"
    title = (title or "Run code").strip()

    try:
        run = _get_run(user_id, agent_session_id)
    except Exception as e:
        logger.exception("code interpreter session start failed")
        return f"Code interpreter unavailable: {type(e).__name__}"

    step_index = len(run.steps)
    step = {
        "index": step_index,
        "title": title[:200],
        "code": code,
        "stdout": "",
        "stderr": "",
        "exitCode": None,
        "executionTime": "",
        "charts": [],
        "status": "running",
    }
    run.steps.append(step)
    _put_row(user_id, agent_session_id, run, status="running", title=title)

    try:
        response = run.client.invoke(
            "executeCode",
            {"code": code, "language": "python", "clearContext": False},
        )
        stdout, stderr, exit_code, exec_time = _consume_stream(response)
        step["stdout"] = stdout
        step["stderr"] = stderr
        step["exitCode"] = exit_code
        step["executionTime"] = exec_time
        # Persist text output before the (slower) chart scan so the panel shows
        # stdout promptly.
        _put_row(user_id, agent_session_id, run, status="running", title=title)

        charts = _collect_charts(run, agent_session_id, step_index)
        step["charts"] = charts
        failed = exit_code not in (None, 0)
        step["status"] = "failed" if failed else "done"
        _put_row(user_id, agent_session_id, run,
                 status="failed" if failed else "idle", title=title)
    except Exception as e:
        logger.exception("execute_python run failed")
        step["status"] = "failed"
        step["stderr"] = (step["stderr"] + f"\n{type(e).__name__}: {e}")[-STDOUT_CAP:]
        _put_row(user_id, agent_session_id, run,
                 status="failed", title=title, last_error=str(e))
        return f"Code execution failed: {type(e).__name__}: {e}"

    # Build a concise summary for the model — real output only, no fabrication.
    parts = []
    tail = step["stdout"].strip()
    if tail:
        parts.append("Output:\n" + tail[-1500:])
    if step["stderr"].strip():
        parts.append("Stderr:\n" + step["stderr"].strip()[-500:])
    if step["charts"]:
        parts.append(f"{len(step['charts'])} chart(s) rendered live in the "
                     f"CodeInterpreter tab.")
    if step["exitCode"] not in (None, 0):
        parts.append(f"(exit code {step['exitCode']})")
    return ("\n\n".join(parts)
            or "Code ran with no output (rendered in the CodeInterpreter tab).")
