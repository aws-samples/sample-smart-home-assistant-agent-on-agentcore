"""Strands tool `browse_web` — drive a live Chrome via AgentCore Browser Tool.

The LLM calls browse_web(goal=...). We:
  1. Start an AgentCore browser session (enableWebBotAuth=true).
  2. Record a running row in smarthome-browser-sessions so the chatbot's
     polling endpoint can surface liveViewUrl.
  3. Run browser_use.Agent with the goal string against the CDP endpoint.
  4. Stop the session; update the DDB row with completed/failed status.
  5. Return the browser-use summary (or a user-visible error).

User identity and agent session id are injected at agent.py registration
time (closure over actor_id / session_id) — the LLM cannot override them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

GOAL_MAX_LEN = 4000
WALL_CLOCK_CAP_S = 120
# AgentCore browser session idle timeout. The tool does NOT proactively stop
# the session on success — the user can keep driving the browser via the
# DCV live view until this timeout elapses (AgentCore then reaps it).
# Only the failure paths call stop_browser_session to avoid leaking a
# half-broken session. 15 min balances exploration time against cost.
SESSION_TIMEOUT_S = 900
TABLE_ENV = "BROWSER_SESSIONS_TABLE_NAME"
REGION_ENV = "AWS_REGION"
# The built-in AWS-managed browser that ships with web-bot-auth enabled.
# Overridable via env so a future follow-up can point at a custom-provisioned
# browser (e.g. one with proxy configuration) without a code change.
BROWSER_IDENTIFIER_ENV = "AGENTCORE_BROWSER_IDENTIFIER"
DEFAULT_BROWSER_IDENTIFIER = "aws.browser.v1"

_data_client = None
_ddb_resource = None


def _agentcore():
    """Data-plane client — browser-session APIs live here, not on control."""
    global _data_client
    if _data_client is None:
        _data_client = boto3.client(
            "bedrock-agentcore",
            region_name=os.environ.get(REGION_ENV, "us-west-2"),
        )
    return _data_client


def _ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource(
            "dynamodb",
            region_name=os.environ.get(REGION_ENV, "us-west-2"),
        )
    return _ddb_resource


def _table():
    name = os.environ.get(TABLE_ENV, "smarthome-browser-sessions")
    return _ddb().Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ddb_put(user_id: str, agent_session_id: str, session_id: str, *,
             status: str, goal: str, live_view_url: str = "",
             last_error: str = "", started_at: str | None = None,
             browser_id: str = "") -> None:
    item = {
        "userId": user_id,
        "sessionId": session_id,
        "agentSessionId": agent_session_id,
        "status": status,
        "goal": goal[:200],
        "liveViewUrl": live_view_url,
        "startedAt": started_at or _now_iso(),
        "ttl": int(time.time()) + 3600,
    }
    if browser_id:
        # Needed by the frontend's Take/Release control toggle, which calls
        # UpdateBrowserStream on (browserIdentifier, sessionId).
        item["browserIdentifier"] = browser_id
    if status != "running":
        item["endedAt"] = _now_iso()
    if last_error:
        item["lastError"] = last_error[:500]
    try:
        _table().put_item(Item=item)
    except Exception as e:
        logger.warning("browser-session DDB write failed (non-fatal): %s", e)


def _build_browser_client():
    """Factory for the AgentCore BrowserClient helper. Isolated so tests can
    monkeypatch the full client without touching AWS SDK internals."""
    from bedrock_agentcore.tools.browser_client import BrowserClient
    return BrowserClient(region=os.environ.get(REGION_ENV, "us-west-2"))


def _build_browser_use_agent(goal: str, ws_url: str, headers: dict, model_id: str,
                             screenshot_dir: str | None = None):
    """Construct a browser_use.Agent wired to the AgentCore CDP endpoint.

    Uses the explicit Browser + BrowserProfile form from the official
    agentcore-samples notebook — passing headers via BrowserProfile is the
    path that actually routes SigV4-signed WebSocket upgrade headers through
    to cdp_use.CDPClient(additional_headers=...). The shortcut
    BrowserSession(headers=...) is forwarded through pydantic field
    merging, which older browser-use builds drop silently.

    When `screenshot_dir` is provided, we attach a step callback that
    writes one PNG per browse step so the chatbot's Files tab surfaces a
    visual audit trail for the run.

    Imported lazily so test mocks can replace this without pulling the
    entire browser-use / playwright stack into the import graph.
    """
    from browser_use import Agent as BrowserUseAgent, Browser, BrowserProfile
    from browser_use.llm.aws.chat_bedrock import ChatAWSBedrock
    import boto3

    profile = BrowserProfile(headers=headers or {}, timeout=1_500_000)
    browser = Browser(cdp_url=ws_url, browser_profile=profile, keep_alive=False)
    # Pass an explicit boto3 Session so ChatAWSBedrock resolves the
    # runtime's IAM-role credentials via the instance metadata service;
    # the library otherwise looks for AWS_ACCESS_KEY_ID/SECRET env vars
    # and fails on a managed runtime where none are set.
    llm = ChatAWSBedrock(
        model=model_id,
        aws_region=os.environ.get(REGION_ENV, "us-west-2"),
        session=boto3.Session(region_name=os.environ.get(REGION_ENV, "us-west-2")),
    )

    step_cb = None
    if screenshot_dir:
        os.makedirs(screenshot_dir, exist_ok=True)

        async def _on_step(_state, _output, step_num: int):
            try:
                ts = datetime.now(timezone.utc).strftime("%H-%M-%S")
                path = os.path.join(screenshot_dir, f"step-{step_num:03d}-{ts}.png")
                await browser.take_screenshot(path=path, full_page=False, format="png")
                logger.info("browse_web step %d screenshot: %s", step_num, path)
            except Exception as e:
                logger.warning("screenshot step %d failed: %s", step_num, e)

        step_cb = _on_step

    agent_kwargs = dict(task=goal, llm=llm, browser_session=browser)
    if step_cb is not None:
        agent_kwargs["register_new_step_callback"] = step_cb
    return BrowserUseAgent(**agent_kwargs)


async def _run_browser_use(agent_obj) -> str:
    return await agent_obj.run()


def run_browse_web(goal: str, user_id: str = "default", agent_session_id: str = "default") -> str:
    """Open a live Chrome browser and drive it to accomplish the user's goal.

    This is the raw implementation. `agent.py` wraps it in a @strands_tool
    closure that pins `user_id` and `agent_session_id` so the LLM cannot
    forge either — keep those parameters out of the LLM-facing signature.
    """
    if not isinstance(goal, str) or not goal.strip():
        return "browse_web failed: goal is empty."
    if len(goal) > GOAL_MAX_LEN:
        return f"browse_web failed: goal too long (max {GOAL_MAX_LEN} chars)."

    user_id = user_id or "default"
    agent_session_id = agent_session_id or "default"
    started_at = _now_iso()

    # Use the AgentCore BrowserClient helper rather than raw boto3: it knows
    # how to construct the SigV4-signed WebSocket headers Playwright needs
    # to pass on the CDP connection. Raw `start_browser_session` hands back
    # a wss URL but the automation endpoint only accepts SigV4-authenticated
    # upgrades — a bare connect is rejected with HTTP 403.
    try:
        browser_id = os.environ.get(BROWSER_IDENTIFIER_ENV, DEFAULT_BROWSER_IDENTIFIER)
        bc = _build_browser_client()
        session_id = bc.start(
            identifier=browser_id,
            name=f"agent-{agent_session_id[:20]}",
            viewport={"width": 1280, "height": 800},
            session_timeout_seconds=SESSION_TIMEOUT_S,
        )
        ws_url, headers = bc.generate_ws_headers()
        live_view_url = bc.generate_live_view_url()
    except Exception as e:
        logger.exception("start_browser_session failed")
        _ddb_put(user_id, agent_session_id, f"failed-{int(time.time())}",
                 status="failed", goal=goal, last_error=str(e),
                 started_at=started_at)
        return f"Browser unavailable: {type(e).__name__}"

    _ddb_put(user_id, agent_session_id, session_id,
             status="running", goal=goal, live_view_url=live_view_url,
             started_at=started_at, browser_id=browser_id)

    # Per-step screenshots land in the text agent's session workspace so
    # they show up in the chatbot's Files tab alongside whatever else the
    # agent saved. Path must match session_storage._session_dir() layout.
    screenshot_dir = os.path.join(
        os.environ.get("AGENT_SESSION_ROOT", "/mnt/workspace"),
        agent_session_id,
        "browser",
    )

    try:
        model_id = os.environ.get("MODEL_ID", "moonshotai.kimi-k2.5")
        browser_agent = _build_browser_use_agent(
            goal, ws_url, headers, model_id, screenshot_dir=screenshot_dir,
        )
        summary = asyncio.run(
            asyncio.wait_for(_run_browser_use(browser_agent), timeout=WALL_CLOCK_CAP_S)
        )
        summary = str(summary).strip() or "(browser returned no summary)"
        # Append a breadcrumb so Kimi knows the Files tab now has content.
        try:
            shots = sorted(os.listdir(screenshot_dir)) if os.path.isdir(screenshot_dir) else []
        except Exception:
            shots = []
        if shots:
            summary = (
                summary
                + f"\n\n(Per-step screenshots saved: {len(shots)} PNG(s) in the Files tab under browser/.)"
            )
    except asyncio.TimeoutError:
        logger.warning("browse_web hit %ds cap for session %s", WALL_CLOCK_CAP_S, session_id)
        _ddb_put(user_id, agent_session_id, session_id,
                 status="failed", goal=goal, live_view_url=live_view_url,
                 last_error="wall-clock timeout", started_at=started_at,
                 browser_id=browser_id)
        _safe_stop(bc)
        return f"Browsing failed: exceeded {WALL_CLOCK_CAP_S}s."
    except Exception as e:
        logger.exception("browser-use run failed")
        _ddb_put(user_id, agent_session_id, session_id,
                 status="failed", goal=goal, live_view_url=live_view_url,
                 last_error=str(e), started_at=started_at,
                 browser_id=browser_id)
        _safe_stop(bc)
        return f"Browsing failed: {type(e).__name__}"

    # Success path: do NOT stop the AgentCore browser session. The user
    # may want to keep exploring the page manually (Take Control toggle
    # in the chatbot flips UpdateBrowserStream to DISABLED, letting them
    # drive the browser directly). AgentCore will reap the session at
    # its sessionTimeoutSeconds. We write status="idle" so the frontend
    # still renders the live view but knows the automation stream is no
    # longer being driven by the tool.
    _ddb_put(user_id, agent_session_id, session_id,
             status="idle", goal=goal, live_view_url=live_view_url,
             started_at=started_at, browser_id=browser_id)
    return summary


def _safe_stop(bc) -> None:
    try:
        bc.stop()
    except Exception as e:
        logger.warning("stop_browser_session failed: %s", e)
