"""WebSocket /ws voice session handler: bridges the browser to Nova Sonic
bi-directional streaming via Strands BidiAgent.

Protocol (between browser and this server):

  Client -> Server
    {"type": "config", "voice": "matthew", "input_sample_rate": 16000,
     "output_sample_rate": 16000, "model_id": "amazon.nova-2-sonic-v1:0"}
    {"type": "audio_input", "audio": "<base64 pcm>"}
    {"type": "text_input", "text": "..."}       (optional)

  Server -> Client
    {"type": "welcome_audio", "audio": "<base64 mp3>", "format": "mp3"}
    {"type": "audio_output", "audio": "<base64 pcm>", "sample_rate": 16000}
    {"type": "transcript", "role": "user"|"assistant", "text": "..."}
    {"type": "system", "message": "..."}

The welcome audio is a static clip pre-rendered by Polly at deploy time and
stored in S3 (key set via WELCOME_AUDIO_S3 env var as "<bucket>/<key>"). On
connection we fetch the object once per session and push it to the client
before the Nova Sonic stream starts.
"""
from __future__ import annotations

import os
import json
import base64
import logging
import asyncio
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

# Shared helpers from the text-runtime module. Safe to import at module level
# now that voice runs under its own entrypoint (voice_agent.py), so `agent`
# won't cause recursive or duplicate runtime wiring. agent.py's ADOT block is
# DISABLE_ADOT-gated (voice_agent.py sets that env var before this module
# loads).
from agent import (  # noqa: E402
    SKILLS_TABLE_NAME,
    _get_dynamodb,
    load_skills_from_dynamodb,
    load_system_prompt,
)

# Module-level imports for Strands' BidiAgent stack. Safe because voice_session
# is only loaded in the voice runtime container, which eager-imports these in
# voice_agent.py already — importing them here again is free and lets us
# define a model subclass at module load time.
from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client


class _TranscriptIdTaggingModel(BidiNovaSonicModel):
    """Tag `BidiTranscriptStreamEvent` with Nova Sonic's `completionId` and
    `generationStage` so the browser can deduplicate SPECULATIVE + FINAL.

    Nova Sonic emits each assistant utterance twice: first a SPECULATIVE
    content block, then a FINAL content block. Each block has its *own*
    `contentId`, so contentId alone can't merge them — but both blocks share
    the surrounding `completionId` (set on `completionStart`, cleared on
    `completionEnd`).

    The browser reducer keys on `(role, completionId, generationStage)`:
      - SPECULATIVE and FINAL of the same utterance share completionId and
        role, so the FINAL replaces the SPECULATIVE bubble.
      - A distinct utterance starts a new completion and gets its own
        completionId, so it renders as a separate bubble.

    Strands' `BidiNovaSonicModel` already tracks `_current_completion_id` and
    `_generation_stage` as instance state — we just surface them on the
    transcript event that Strands emits.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_text_content_id: str | None = None
        logger.info("Voice WS: using _TranscriptIdTaggingModel (completionId/generationStage stamping active)")

    def _convert_nova_event(self, nova_event):
        # Cache the current TEXT contentId for completeness/debug. The browser
        # dedup key is completionId (see class docstring) — contentId differs
        # between SPECULATIVE and FINAL blocks so it cannot be the dedup key.
        if "contentStart" in nova_event:
            content_data = nova_event["contentStart"]
            if content_data.get("type") == "TEXT":
                self._current_text_content_id = content_data.get("contentId")

        result = super()._convert_nova_event(nova_event)

        # BidiTranscriptStreamEvent is a dict subclass; stamp completionId and
        # generationStage on it so the browser can correlate SPEC/FINAL pairs.
        try:
            if result is not None and hasattr(result, "get") and result.get("type") == "bidi_transcript_stream":
                if self._current_completion_id:
                    result["completionId"] = self._current_completion_id
                # _generation_stage is "SPECULATIVE" or "FINAL" (set on
                # contentStart). It's the string Nova Sonic sends verbatim.
                if getattr(self, "_generation_stage", None):
                    result["generationStage"] = self._generation_stage
                if self._current_text_content_id:
                    result["contentId"] = self._current_text_content_id
        except Exception:
            pass

        if "contentEnd" in nova_event:
            self._current_text_content_id = None

        return result

logger = logging.getLogger(__name__)

NOVA_SONIC_MODEL_ID = os.environ.get("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
if not GATEWAY_URL:
    for _k, _v in os.environ.items():
        if _k.startswith("AGENTCORE_GATEWAY_") and _k.endswith("_URL"):
            GATEWAY_URL = _v
            break


# Map unprefixed tool names (as used in skill markdown) to the MCP-gateway-
# prefixed form Nova Sonic needs to emit in toolUse events. Skills reference
# `discover_devices` / `control_device` / `device_control`; the gateway
# exposes them as `SmartHomeDeviceDiscovery___discover_devices` etc.
_TOOL_ALIASES = {
    "discover_devices": "SmartHomeDeviceDiscovery___discover_devices",
    "control_device": "SmartHomeDeviceControl___control_device",
    "device_control": "SmartHomeDeviceControl___control_device",
    "query_knowledge_base": "SmartHomeKnowledgeBase___query_knowledge_base",
}


def _rewrite_tool_names(text: str) -> str:
    """Replace bare tool names in skill text with the MCP-prefixed versions.

    Word-boundary substitution so we don't mangle adjacent identifiers.
    """
    import re
    for short, full in _TOOL_ALIASES.items():
        text = re.sub(rf"\b{re.escape(short)}\b", full, text)
    return text

# Voice prompt mirrors the text-mode guardrails (must use discover_devices,
# no fabrication, no narrating tool calls) plus voice-specific tone rules.
# Without these, Nova Sonic happily invents device names from its training
# data — we observed it listing "客厅空调, 厨房灯, 门锁, 安全摄像头" which
# don't exist in this stack.
#
# The tool names are the MCP gateway's prefixed names (target___toolName).
# Nova Sonic needs the exact names so it can emit a `toolUse` event with
# matching `toolName` — a bare `discover_devices` would cause the model to
# hallucinate rather than invoke our tool.
VOICE_SYSTEM_PROMPT = (
    "You are a smart home voice assistant. You control: LED Matrix, Rice Cooker, Fan, Oven.\n"
    "Reply in one short spoken sentence. No Markdown, no lists, no numbered steps.\n"
    "\n"
    "To LIST devices: call SmartHomeDeviceDiscovery___discover_devices.\n"
    "To CONTROL one device: call SmartHomeDeviceControl___control_device with "
    "device_type in {led_matrix, rice_cooker, fan, oven} and a command object.\n"
    "To TURN ON EVERY DEVICE AT ONCE (\"turn on all\", \"打开所有设备\", etc.): call "
    "turn_on_all_devices — it performs the full discover + power-on loop in one call and "
    "returns a short summary you should speak back to the user.\n"
    "Never list devices from memory — always call the discovery tool first.\n"
    "Never fabricate tool results. If a tool fails, say so honestly.\n"
    "Never narrate 'let me check' or 'I'll call the tool' — just invoke it."
)


# Welcome clip is env-gated so we can measure real BidiAgent initialization
# latency (the welcome audio otherwise masks it by giving the user something
# to hear during startup). Set VOICE_WELCOME_ENABLED=1 to re-enable; the
# chatbot's connection-banner status already covers the "is it alive?"
# affordance, making audio greeting non-essential.
_WELCOME_ENABLED = os.environ.get("VOICE_WELCOME_ENABLED", "0") == "1"
_WELCOME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome-zh.mp3")
_WELCOME_BYTES: Optional[bytes] = None
if _WELCOME_ENABLED:
    # Read the bundled welcome clip at module import, not per-request. The MP3
    # is baked into the CodeZip alongside agent.py (see scripts/setup-agentcore.py
    # step 2 — Polly renders it into the agent directory before the CLI
    # packages). Loading here means the first WebSocket connection has the
    # bytes ready in RAM from the moment the process is warm.
    try:
        with open(_WELCOME_PATH, "rb") as _f:
            _WELCOME_BYTES = _f.read()
        logger.info(f"Preloaded welcome audio: {len(_WELCOME_BYTES)} bytes from {_WELCOME_PATH}")
    except FileNotFoundError:
        logger.warning(f"Welcome audio not found at {_WELCOME_PATH}; voice mode will skip the greeting")
else:
    logger.info("Welcome clip disabled (VOICE_WELCOME_ENABLED != '1')")


def _record_voice_session(actor_id: str, session_id: str) -> None:
    """Mirror agent._record_session for voice sessions, writing under a
    distinct sort key so setup-agentcore.py can invalidate them against the
    voice-runtime ARN (different from the text runtime's ARN)."""
    if not SKILLS_TABLE_NAME:
        return
    try:
        _get_dynamodb().Table(SKILLS_TABLE_NAME).put_item(Item={
            "userId": actor_id,
            "skillName": "__session_voice__",
            "sessionId": session_id,
            "lastActiveAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning(f"Failed to record voice session: {e}")


def _extract_devices(mcp_response):
    """Pull the devices JSON out of whatever shape the MCP call returns.

    call_tool_sync can return a dict (`{"status": "success", "content": [...]}`),
    an object with `.content`, or a ToolResult from Strands. Handle all three.
    """
    import json as _json
    content = None
    if hasattr(mcp_response, "content"):
        content = mcp_response.content
    elif isinstance(mcp_response, dict):
        content = mcp_response.get("content")
    if not content:
        return None
    for c in content:
        text = None
        if hasattr(c, "text"):
            text = c.text
        elif isinstance(c, dict):
            text = c.get("text")
        if not text:
            continue
        try:
            data = _json.loads(text)
        except Exception:
            continue
        if isinstance(data, dict) and "devices" in data:
            return data
    return None


def _build_turn_on_all_tool(mcp_client):
    """Build a single Strands @tool that runs the full discover + power-on
    loop server-side. Nova Sonic's voice model only makes one tool call per
    turn before ending, so multi-step orchestration has to be collapsed into
    one callable. MCP gateway calls are made directly through the existing
    live MCPClient session, so per-user Cedar policies still apply.
    """
    import json as _json
    import uuid as _uuid
    from strands import tool as _strands_tool

    @_strands_tool
    def turn_on_all_devices() -> str:
        """Turn on every smart home device the user has. Call this tool whenever
        the user asks to power on everything / all devices / the whole house.
        It handles the full discover-then-power-on loop internally and returns
        a short summary the assistant should speak back verbatim."""
        try:
            disc = mcp_client.call_tool_sync(
                tool_use_id=str(_uuid.uuid4()),
                name="SmartHomeDeviceDiscovery___discover_devices",
                arguments={},
            )
            logger.info(f"turn_on_all: discover raw={str(disc)[:500]}")
            devices_payload = _extract_devices(disc)
            if not devices_payload or "devices" not in devices_payload:
                return f"Discovery failed — couldn't parse response: {str(disc)[:200]}"

            names_on = []
            for dev in devices_payload["devices"]:
                device_type = dev.get("deviceType")
                cmd = dev.get("powerOn")
                if not device_type or not cmd:
                    continue
                try:
                    mcp_client.call_tool_sync(
                        tool_use_id=str(_uuid.uuid4()),
                        name="SmartHomeDeviceControl___control_device",
                        arguments={"device_type": device_type, "command": cmd},
                    )
                    names_on.append(dev.get("displayName") or device_type)
                except Exception as e:
                    logger.warning(f"turn_on_all_devices: failed on {device_type}: {e}")
            if not names_on:
                return "I couldn't turn on any devices."
            return f"Turned on {len(names_on)} devices: {', '.join(names_on)}."
        except Exception as e:
            logger.exception("turn_on_all_devices failed")
            return f"Failed to turn on all devices: {e}"

    return turn_on_all_devices


async def handle_voice_session(
    websocket: WebSocket,
    context,
    gateway_arn: str,
    region: str,
) -> None:
    """Accept the WS, send welcome audio, then run a Nova Sonic BidiAgent session."""
    await websocket.accept()
    session_id = getattr(context, "session_id", None) or "default"
    logger.info(f"Voice WS: connection accepted (session={session_id})")
    # Diagnostic: dump what the runtime exposes on `context` so we can trace
    # header-availability regressions (gateway 401s).
    try:
        ctx_attrs = [a for a in dir(context) if not a.startswith("_")]
        logger.info(f"Voice WS: context attrs={ctx_attrs}")
        rh_dbg = getattr(context, "request_headers", None)
        if rh_dbg is None:
            logger.warning("Voice WS: context.request_headers is None")
        else:
            try:
                keys = list(rh_dbg.keys()) if hasattr(rh_dbg, "keys") else list(rh_dbg)
                logger.info(f"Voice WS: header keys={keys}")
            except Exception as e:
                logger.warning(f"Voice WS: header-enum failed: {e}")
    except Exception as e:
        logger.warning(f"Voice WS: context inspect failed: {e}")

    # Latency probe sentinel: send a `ready` frame immediately so probes can
    # separate WS-handshake time from server-side init time. The frontend
    # ignores unknown message types, so this is safe for regular clients.
    try:
        await websocket.send_json({"type": "ready", "ts_ms": int(time.time() * 1000)})
    except Exception:
        pass

    # Wait for the client's initial config event BEFORE pushing welcome audio.
    # Empirically the runtime's WS proxy buffers/drops server-sent frames if
    # they hit the socket before the client has completed its first client→
    # server exchange on the stream. Receiving the config first kicks the
    # receive loop into life and unblocks subsequent server→client sends.
    try:
        config = await _wait_for_config(websocket)
    except WebSocketDisconnect:
        return
    if config is None:
        return

    # BidiAgent / BidiNovaSonicModel / MCPClient are module-level imports now
    # (voice_agent.py eager-imports them for warmup; importing here too is
    # free after that).

    # Load skills (same DynamoDB-backed loader the text path uses). BidiAgent
    # doesn't support the AgentSkills plugin API, so we inline the skill
    # instructions into the system prompt after rewriting tool names to their
    # MCP-prefixed form.
    actor_id = payload_user_id = "default"
    rh_for_id = getattr(context, "request_headers", None)
    if rh_for_id:
        lowered_headers = {str(k).lower(): v for k, v in rh_for_id.items()} if hasattr(rh_for_id, "items") else {}
        id_token = lowered_headers.get("x-amzn-bedrock-agentcore-runtime-custom-authtoken")
        if id_token:
            try:
                import base64 as _b64, json as _json
                parts = id_token.split(".")
                if len(parts) >= 2:
                    payload_raw = parts[1] + "=" * (-len(parts[1]) % 4)
                    claims = _json.loads(_b64.urlsafe_b64decode(payload_raw))
                    actor_id = claims.get("email") or claims.get("cognito:username") or claims.get("sub") or "default"
            except Exception as e:
                logger.warning(f"Could not parse idToken claims for skill loading: {e}")

    # Record this voice session under __session_voice__ so redeploy-time session
    # invalidation can target the voice-runtime ARN (different from text).
    _record_voice_session(actor_id, session_id)

    # Extract the forwarded idToken from the request context so we can pass it
    # as Bearer to the CUSTOM_JWT gateway (same flow as the text path). We do
    # this first because it's cheap and the MCPClient below needs gw_headers.
    gw_headers = {}
    rh = getattr(context, "request_headers", None)
    if rh:
        lowered = {str(k).lower(): v for k, v in rh.items()} if hasattr(rh, "items") else {}
        token = lowered.get("x-amzn-bedrock-agentcore-runtime-custom-authtoken") or lowered.get("authorization")
        if token:
            gw_headers["Authorization"] = token if str(token).lower().startswith("bearer ") else f"Bearer {token}"
            logger.info("Voice WS: forwarding user JWT to gateway for per-user Cedar policy")
        else:
            logger.warning("Voice WS: no user token — gateway per-user policies won't apply")

    # Parallelize the three IO-bound startup tasks that have no mutual
    # dependency:
    #   (1) DynamoDB skills query (per-user + global)
    #   (2) DynamoDB voice-prompt override (per-user + global, 2 GetItem calls)
    #   (3) MCP list_tools_sync (network round-trip to the gateway, paginated)
    # Sequential baseline was ~1s; running them concurrent cuts it to the
    # slowest single call (usually MCP at ~400-600ms).
    #
    # For (3) we also need the MCPClient open — but we can't close/reopen
    # across the session (MCPClient holds the gateway SSE stream), so the
    # list_tools work happens inside a spun-up MCPClient that stays open for
    # the rest of the session. Enter the context synchronously and keep it
    # alive via a nested coroutine below.
    skill_blocks: list[str] = []
    base_voice_prompt = VOICE_SYSTEM_PROMPT

    def _load_skill_blocks() -> list[str]:
        """Voice context: load only the 'all-devices-on' skill (the multi-step
        orchestration case). Inlining all 5 skill bodies made the prompt big
        enough that Nova Sonic started hallucinating. Single-device commands
        are already covered by the base prompt's tool names.
        """
        blocks: list[str] = []
        try:
            if SKILLS_TABLE_NAME:
                for skill in load_skills_from_dynamodb(actor_id):
                    if skill.name != "all-devices-on":
                        continue
                    body = _rewrite_tool_names(skill.instructions or "")
                    if body.strip():
                        blocks.append(f"\n\n### When the user asks to 'turn on all devices':\n{body.strip()}")
                    break
        except Exception as e:
            logger.warning(f"Voice WS: skill loading failed: {e}")
        return blocks

    def _load_prompt_override() -> Optional[str]:
        try:
            return load_system_prompt(actor_id, "voice")
        except Exception as e:
            logger.warning(f"Voice WS: voice prompt override load failed, using default: {e}")
            return None

    # Kick off DDB reads in worker threads so they run in parallel with MCP.
    skill_task = asyncio.create_task(asyncio.to_thread(_load_skill_blocks))
    prompt_task = asyncio.create_task(asyncio.to_thread(_load_prompt_override))

    # Open MCP gateway and list tools. The tools list is what lets the BidiAgent
    # actually invoke `discover_devices`, `control_device`, etc — without this
    # Nova Sonic has no idea the tools exist and will hallucinate device lists
    # from its training data.
    #
    # Do NOT pass `mcp_gateway_arn=[...]` here. Older Strands silently ignored
    # it, but current builds honor it and register the gateway using the
    # runtime's execution-role IAM credentials. That parallel path conflicts
    # with our explicit `BidiAgent(tools=...)` pipeline: Nova Sonic invokes via
    # the model's internal path, and the result never reaches Strands' tool
    # dispatcher — so the WS never emits `bidi_tool_result` and Nova Sonic
    # hangs waiting for a toolResult that will never arrive. We keep a single
    # source of truth: the MCPClient below (with the user JWT as Bearer),
    # feeding tools via BidiAgent(tools=).
    model = _TranscriptIdTaggingModel(
        region=region,
        model_id=config["model_id"],
        provider_config={
            "audio": {
                "input_sample_rate": config["input_sample_rate"],
                "output_sample_rate": config["output_sample_rate"],
                "voice": config["voice"],
            }
        },
    )

    mcp_client = None
    tools_list = []
    if GATEWAY_URL:
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=gw_headers or None))

    def _load_tools_sync() -> list:
        """Enter MCPClient and paginate list_tools. Sync helper so we can call
        it via asyncio.to_thread for concurrent execution with the DDB reads.
        Caller must re-enter the context later (MCPClient's background thread
        persists across with-blocks; re-entering is cheap)."""
        # The MCPClient context has already been entered below; this runs
        # within that live session.
        tools = []
        pagination_token = None
        while True:
            result = mcp_client.list_tools_sync(pagination_token=pagination_token)
            tools.extend(result)
            if result.pagination_token is None:
                break
            pagination_token = result.pagination_token
        return list(tools)

    async def _run_with_tools():
        nonlocal tools_list, skill_blocks, base_voice_prompt
        if mcp_client is not None:
            # MCPClient is a sync context manager; enter it once for the full
            # voice session so the background pumps stay alive. list_tools is
            # dispatched to a worker thread so it runs concurrently with the
            # DDB reads we kicked off above.
            with mcp_client:
                tools_task = asyncio.create_task(asyncio.to_thread(_load_tools_sync))
                skills_result, prompt_result, tools = await asyncio.gather(
                    skill_task, prompt_task, tools_task,
                )
                skill_blocks = skills_result
                if prompt_result:
                    base_voice_prompt = prompt_result
                    logger.info(f"Voice WS: using per-user/global voice prompt override for actor={actor_id}")
                logger.info(f"Voice WS: inlined {len(skill_blocks)} skill(s) for actor={actor_id}")
                tools_list = list(tools)

                # Composite tool: Nova Sonic's voice model doesn't auto-chain
                # tool calls the way text LLMs do — it typically makes one
                # tool call, reads the result, then ends the turn. So
                # multi-step flows like "turn on all devices" stall after the
                # first discover_devices call. We wrap the whole loop as a
                # single tool; from Nova Sonic's POV it's one call, one reply.
                if any(getattr(t, "tool_name", "") == "SmartHomeDeviceDiscovery___discover_devices" for t in tools):
                    tools_list.append(_build_turn_on_all_tool(mcp_client))
                tool_names = [getattr(t, "tool_name", "?") for t in tools_list]
                logger.info(f"Voice WS: loaded {len(tools_list)} tools for BidiAgent: {tool_names}")
                effective_prompt = base_voice_prompt + "".join(skill_blocks)
                agent = BidiAgent(
                    model=model,
                    tools=tools_list,
                    system_prompt=effective_prompt,
                )
                # Confirm what BidiAgent actually registered — tool_registry is what
                # gets forwarded to Nova Sonic's `promptStart` event.
                try:
                    specs = agent.tool_registry.get_all_tool_specs()
                    logger.info(f"Voice WS: BidiAgent tool_registry has {len(specs)} specs: {[s.get('name') for s in specs]}")
                except Exception as e:
                    logger.warning(f"Could not introspect tool_registry: {e}")
                await _drive_agent(agent)
        else:
            logger.warning("Voice WS: no GATEWAY_URL — running without MCP tools")
            # Still need to await the DDB tasks so their exceptions surface.
            skills_result, prompt_result = await asyncio.gather(skill_task, prompt_task)
            skill_blocks = skills_result
            if prompt_result:
                base_voice_prompt = prompt_result
            effective_prompt = base_voice_prompt + "".join(skill_blocks)
            agent = BidiAgent(
                model=model,
                tools=[],
                system_prompt=effective_prompt,
            )
            await _drive_agent(agent)

    async def _drive_agent(agent):
        await websocket.send_json({"type": "system", "message": "Agent ready."})

        async def handle_input():
            while True:
                try:
                    message = await websocket.receive_json()
                except WebSocketDisconnect:
                    raise
                mtype = message.get("type")
                if mtype == "config":
                    logger.info("Ignoring duplicate config event")
                    continue
                # Legacy: tolerate older client-side type names.
                if mtype == "audio_input":
                    message = {**message, "type": "bidi_audio_input", "channels": message.get("channels", 1)}
                elif mtype == "text_input":
                    message = {**message, "type": "bidi_text_input"}
                return message

        async def send_output(event):
            """Forward BidiAgent output events to the browser.

            Strands can emit dicts, TypedEvents (dict-like), or raw exception
            objects (e.g. BidiModelTimeoutError). `websocket.send_json` rejects
            anything that isn't JSON-serializable, so coerce defensively: try
            the event as-is, fall back to serialising via `default=str`, and
            surface unknown shapes as a generic error frame instead of dropping
            them silently (which was masking Nova Sonic timeouts in the UI).
            """
            # One-line debug for tool activity so we can confirm Nova Sonic
            # actually emits toolUse rather than only text.
            try:
                et = event.get("type") if hasattr(event, "get") else None
                if et in ("tool_use_stream", "bidi_tool_use", "tool_use", "bidi_tool_result"):
                    logger.info(f"Voice WS: tool event {et}: {str(event)[:300]}")
                elif et == "bidi_transcript_stream":
                    logger.info(
                        "Voice WS: transcript role=%s stage=%s is_final=%s completionId=%s contentId=%s text=%s",
                        event.get("role"), event.get("generationStage"),
                        event.get("is_final"), event.get("completionId"),
                        event.get("contentId"), (event.get("text") or "")[:80],
                    )
            except Exception:
                pass
            try:
                await websocket.send_json(event)
                return
            except TypeError:
                pass
            except Exception as e:
                logger.warning(f"Failed to forward model event: {e}")
                return
            # Coerce non-standard event objects to a JSON frame.
            try:
                import json as _json
                payload_text = _json.dumps(event, default=str)
                await websocket.send_text(payload_text)
                return
            except Exception:
                pass
            # Last resort: emit an error frame so the UI knows something went wrong.
            try:
                await websocket.send_json({
                    "type": "bidi_error",
                    "message": f"unserialisable {type(event).__name__}: {event!s}",
                })
            except Exception as e:
                logger.warning(f"send_output fallback failed: {e}")

        welcome_task = (
            asyncio.create_task(_welcome_stream(websocket))
            if _WELCOME_ENABLED and _WELCOME_BYTES
            else None
        )
        try:
            await agent.run(inputs=[handle_input], outputs=[send_output])
        finally:
            if welcome_task is not None:
                welcome_task.cancel()

    async def _welcome_stream(ws: WebSocket):
        """Emit the preloaded welcome clip once the BidiAgent pipeline is live.

        Direct `websocket.send_json(...)` calls before `agent.run(...)` starts
        appear to be dropped by the runtime's WS proxy. But frames sent while
        the BidiAgent receive loop is running — the same path Nova Sonic's
        own `bidi_audio_stream` traverses — do arrive. We sleep a moment after
        agent.run starts so the pipeline is established, then chunk the MP3
        as a tagged `bidi_audio_stream` event (reusing the known-good type so
        the proxy's frame filter lets it through).
        """
        audio = _WELCOME_BYTES
        if not audio:
            return
        # Small delay lets BidiAgent spin up its receive/send pumps.
        await asyncio.sleep(0.2)
        b64 = base64.b64encode(audio).decode("ascii")
        chunk_chars = 3000 - (3000 % 4)
        total = (len(b64) + chunk_chars - 1) // chunk_chars
        for idx in range(total):
            part = b64[idx * chunk_chars : (idx + 1) * chunk_chars]
            try:
                await ws.send_json({
                    "type": "bidi_audio_stream",
                    "is_welcome": True,
                    "format": "mp3",
                    "seq": idx,
                    "total": total,
                    "audio": part,
                })
            except Exception as e:
                logger.warning(f"Welcome send failed at seq {idx}: {e}")
                return
            await asyncio.sleep(0.02)  # pace between frames
        logger.info(f"Welcome streamed in {total} chunks ({len(audio)} bytes)")

    try:
        await _run_with_tools()
    except WebSocketDisconnect:
        logger.info("Voice WS: client disconnected")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Voice session error: {e}")
        traceback.print_exc()
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        logger.info("Voice WS: session closed")


async def _wait_for_config(websocket: WebSocket) -> Optional[dict]:
    """Wait for the client's first 'config' event with safe defaults."""
    while True:
        message = await websocket.receive_json()
        if message.get("type") != "config":
            await websocket.send_json({
                "type": "system",
                "message": "Please send a config event before streaming audio.",
            })
            continue
        return {
            "voice": message.get("voice", "matthew"),
            "input_sample_rate": int(message.get("input_sample_rate", 16000)),
            "output_sample_rate": int(message.get("output_sample_rate", 16000)),
            "model_id": message.get("model_id", NOVA_SONIC_MODEL_ID),
        }
