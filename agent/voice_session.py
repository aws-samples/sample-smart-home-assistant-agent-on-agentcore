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
import traceback
from typing import Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

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


# Read the bundled welcome clip at module import, not per-request. The MP3 is
# baked into the CodeZip alongside agent.py (see scripts/setup-agentcore.py,
# step 2 — Polly renders it into the agent directory before the CLI packages).
# Loading here means the first WebSocket connection has the bytes ready in
# RAM from the moment the process is warm.
_WELCOME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome-zh.mp3")
try:
    with open(_WELCOME_PATH, "rb") as _f:
        _WELCOME_BYTES: Optional[bytes] = _f.read()
    logger.info(f"Preloaded welcome audio: {len(_WELCOME_BYTES)} bytes from {_WELCOME_PATH}")
except FileNotFoundError:
    _WELCOME_BYTES = None
    logger.warning(f"Welcome audio not found at {_WELCOME_PATH}; voice mode will skip the greeting")


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

    # Lazy-import so the HTTP path doesn't pay for BidiAgent deps at startup.
    try:
        from strands.experimental.bidi.agent import BidiAgent
        from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
        from strands.tools.mcp.mcp_client import MCPClient
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as e:
        logger.error(f"strands bidi extras not installed: {e}")
        await websocket.send_json({
            "type": "error",
            "message": "Voice mode not available on this server.",
        })
        return

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

    # Voice context: load only the "all-devices-on" skill (the multi-step
    # orchestration case that actually needs explicit instructions). Inlining
    # all 5 skill bodies was making the system prompt big enough that Nova
    # Sonic started ignoring the tool schema and hallucinating. Single-step
    # device control is already covered by the base prompt's tool names.
    skill_blocks: list[str] = []
    try:
        from agent import load_skills_from_dynamodb, SKILLS_TABLE_NAME
        if SKILLS_TABLE_NAME:
            for skill in load_skills_from_dynamodb(actor_id):
                if skill.name != "all-devices-on":
                    continue
                body = _rewrite_tool_names(skill.instructions or "")
                if body.strip():
                    skill_blocks.append(f"\n\n### When the user asks to 'turn on all devices':\n{body.strip()}")
                break
            logger.info(f"Voice WS: inlined {len(skill_blocks)} skill(s) for actor={actor_id}")
    except Exception as e:
        logger.warning(f"Voice WS: skill loading failed: {e}")

    # Resolve voice system prompt override (per-user → global → hardcoded default).
    # Same DynamoDB resolution as the text path, stored under skillName="__prompt_voice__".
    base_voice_prompt = VOICE_SYSTEM_PROMPT
    try:
        from agent import load_system_prompt
        override = load_system_prompt(actor_id, "voice")
        if override:
            base_voice_prompt = override
            logger.info(f"Voice WS: using per-user/global voice prompt override for actor={actor_id}")
    except Exception as e:
        logger.warning(f"Voice WS: voice prompt override load failed, using default: {e}")

    # Extract the forwarded idToken from the request context so we can pass it
    # as Bearer to the CUSTOM_JWT gateway (same flow as the text path).
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

    # Open MCP gateway and list tools. The tools list is what lets the BidiAgent
    # actually invoke `discover_devices`, `control_device`, etc — without this
    # Nova Sonic has no idea the tools exist and will hallucinate device lists
    # from its training data. `mcp_gateway_arn` on the model alone does NOT
    # wire the tools into Strands' execution path.
    model = BidiNovaSonicModel(
        region=region,
        model_id=config["model_id"],
        provider_config={
            "audio": {
                "input_sample_rate": config["input_sample_rate"],
                "output_sample_rate": config["output_sample_rate"],
                "voice": config["voice"],
            }
        },
        mcp_gateway_arn=[gateway_arn] if gateway_arn else None,
    )

    mcp_client = None
    tools_list = []
    if GATEWAY_URL:
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=gw_headers or None))

    async def _run_with_tools():
        nonlocal tools_list
        if mcp_client is not None:
            # MCPClient is a sync context manager; enter it once for the full
            # voice session so the background pumps stay alive.
            with mcp_client:
                tools = []
                pagination_token = None
                while True:
                    result = mcp_client.list_tools_sync(pagination_token=pagination_token)
                    tools.extend(result)
                    if result.pagination_token is None:
                        break
                    pagination_token = result.pagination_token
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

        welcome_task = asyncio.create_task(_welcome_stream(websocket))
        try:
            await agent.run(inputs=[handle_input], outputs=[send_output])
        finally:
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
