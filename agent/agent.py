import os
import json
import logging

# OpenTelemetry instrumentation is handled entirely by the AgentCore Runtime
# container: `agentcore deploy` auto-instruments the process, emits spans
# to aws/spans, and propagates session.id from the runtimeSessionId header.
# Do NOT call StrandsTelemetry, set OTEL_SEMCONV_STABILITY_OPT_IN, or attach
# baggage here — those override the runtime's providers and break span
# export. Mirrors the AWS-official Strands sample at
# amazon-bedrock-agentcore-samples/.../07-AgentCore-evaluations/00-prereqs/
# eval_agent_strands.py. See docs/architecture-and-design.md §9.13.

from strands import Agent, AgentSkills
from strands.vended_plugins.skills import Skill
from strands.models.bedrock import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore import BedrockAgentCoreApp
from memory.session import get_memory_session_manager, _sanitize_actor_id

import boto3
from boto3.dynamodb.conditions import Key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# agentcore CLI sets env vars as AGENTCORE_GATEWAY_{GATEWAYNAME}_URL / _ARN
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
GATEWAY_ARN = os.environ.get("AGENTCORE_GATEWAY_ARN", "")
if not GATEWAY_URL:
    for key, val in os.environ.items():
        if key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_URL"):
            GATEWAY_URL = val
        elif key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_ARN"):
            GATEWAY_ARN = val

MODEL_ID = os.environ.get("MODEL_ID", "moonshotai.kimi-k2.5")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "")
# Dedicated table for per-login AgentCore Runtime sessions (text + voice).
# Writing here (sort key includes sessionId) preserves session history per
# user so the admin Sessions tab can list all of them.
RUNTIME_SESSIONS_TABLE_NAME = os.environ.get("RUNTIME_SESSIONS_TABLE_NAME", "")

app = BedrockAgentCoreApp()

_dynamodb_resource = None


def _get_dynamodb():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb_resource


def load_skills_from_dynamodb(actor_id: str) -> list:
    """Load global + user-specific skills from DynamoDB, return list of Skill instances."""
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    skills_by_name = {}

    resp = table.query(KeyConditionExpression=Key("userId").eq("__global__"))
    for item in resp.get("Items", []):
        allowed_tools = item.get("allowedTools")
        if isinstance(allowed_tools, set):
            allowed_tools = list(allowed_tools)
        skills_by_name[item["skillName"]] = Skill(
            name=item["skillName"],
            description=item.get("description", ""),
            instructions=item.get("instructions", ""),
            allowed_tools=allowed_tools,
            license=item.get("license"),
            compatibility=item.get("compatibility"),
            metadata=item.get("metadata") or {},
        )

    if actor_id and actor_id not in ("default", "__global__"):
        resp = table.query(KeyConditionExpression=Key("userId").eq(actor_id))
        for item in resp.get("Items", []):
            allowed_tools = item.get("allowedTools")
            if isinstance(allowed_tools, set):
                allowed_tools = list(allowed_tools)
            skills_by_name[item["skillName"]] = Skill(
                name=item["skillName"],
                description=item.get("description", ""),
                instructions=item.get("instructions", ""),
                allowed_tools=allowed_tools,
                license=item.get("license"),
                compatibility=item.get("compatibility"),
                metadata=item.get("metadata") or {},
            )

    return list(skills_by_name.values())


def load_user_settings(actor_id: str) -> dict:
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    for uid in [actor_id, "__global__"]:
        if uid in ("default", ""):
            continue
        resp = table.get_item(Key={"userId": uid, "skillName": "__settings__"})
        item = resp.get("Item")
        if item and item.get("modelId"):
            return {"modelId": item["modelId"]}
    return {}


def load_user_a2a_permissions(actor_id: str) -> dict[str, list[str]]:
    """Return {registryRecordId: [allowed_skill_id, ...]} for this user.

    Global grants (``__global__``) are merged with per-user grants; per-user
    wins on the same recordId. Same merge semantics as ``load_skills_from_dynamodb``.
    A missing row means "no grants"; the agent simply registers no A2A tools.
    """
    if not SKILLS_TABLE_NAME:
        return {}
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    merged: dict[str, list[str]] = {}

    def _read(uid: str) -> dict:
        if not uid or uid in ("default",):
            return {}
        try:
            resp = table.get_item(Key={"userId": uid, "skillName": "__a2a_permissions__"})
        except Exception as e:
            logger.warning(f"A2A perms read failed for {uid}: {e}")
            return {}
        item = resp.get("Item") or {}
        grants = item.get("a2aGrants") or {}
        # DynamoDB can hand us sets; normalise to lists of str.
        out: dict[str, list[str]] = {}
        for rid, skills in grants.items():
            if isinstance(skills, (set, list, tuple)):
                out[rid] = sorted(str(s) for s in skills)
        return out

    merged.update(_read("__global__"))
    if actor_id and actor_id != "__global__":
        merged.update(_read(actor_id))
    return merged


def load_system_prompt(actor_id: str, agent_type: str,
                       headers: dict | None = None) -> str | None:
    """Resolve the active system prompt for this user/agent.

    Order of resolution:
      1. If the request carries a W3C `baggage` header with a bundle
         reference (set by AgentCore Gateway during an A/B test), load the
         prompt from that bundle version. Falls through to step 2 on any
         failure.
      2. Existing DDB additive resolution: __global__ + per-user, joined
         with "\\n\\n".
      3. None when both DDB rows are empty (caller uses the hardcoded
         constant in agent.py / voice_session.py).

    `headers` is optional — when None, behaves exactly like the pre-bundle
    path (so callers without a baggage source need no changes).
    """
    if headers:
        try:
            import bundle_config
            bundle_prompt = bundle_config.load_from_baggage(headers, agent_type)
            if bundle_prompt is not None:
                logger.info("Using prompt from bundle (A/B variant) for agent_type=%s", agent_type)
                return bundle_prompt
        except Exception as e:  # noqa: BLE001 — fail-open; never break invocations
            logger.warning("bundle hook failed, falling back to DDB: %s", e)

    if not SKILLS_TABLE_NAME:
        return None
    sk = f"__prompt_{agent_type}__"
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)

    def _read(uid: str) -> str:
        if uid in ("default", ""):
            return ""
        resp = table.get_item(Key={"userId": uid, "skillName": sk})
        item = resp.get("Item")
        body = item.get("promptBody") if item else None
        return body.strip() if isinstance(body, str) else ""

    global_body = _read("__global__")
    user_body = _read(actor_id) if actor_id != "__global__" else ""

    parts = [p for p in (global_body, user_body) if p]
    return "\n\n".join(parts) if parts else None


_static_skills_plugin = AgentSkills(skills="./skills/")


SYSTEM_PROMPT = """You are a smart home assistant.

CAPABILITIES (only those registered as tools/skills/A2A agents in THIS turn are truly available; items below describe what *may* be registered):
  1. Device control & querying — turn devices on/off, change modes, query current settings. Devices in scope: LED Matrix, Rice Cooker, Fan, Oven.
  2. Enterprise knowledge base — product manuals, troubleshooting guides, company documents. Query it with query_knowledge_base when the user asks about information rather than control.
  3. Image analysis — the user can attach photos or screenshots. Images are captioned upstream by a vision model; the caption is inserted into this conversation as a prior assistant message before your turn starts.
  4. Specialist A2A agents — registered only when granted to this user, each exposed as an `a2a_*` tool for a specific domain (e.g. home security, energy optimization, appliance maintenance). If no matching `a2a_*` tool is listed in your tools this turn, you do NOT have that domain's expertise.

Be helpful and concise. Confirm actions you take. Use what you remember about the user's preferences to personalize responses. You may also suggest creative lighting scenes, cooking presets, and comfort settings within the device scope above.

SCOPE RULES — classify every user message before replying:

A. SOCIAL / META turns are always allowed without a tool call:
   - Greetings, thanks, goodbyes ("hi", "你好", "thank you").
   - Identity and capability questions ("who are you", "你是谁", "你能做什么", "what can you do") — answer by summarizing CAPABILITIES above, but only mention items whose tool/skill/agent is actually registered this turn.
   - Clarifying questions back to the user when their intent is ambiguous.

B. For any OTHER request, classify first:
   1. Does the request map to a tool, skill, or A2A agent that is ACTUALLY registered in this turn?
      - Yes → you MUST call it. Do not answer from general knowledge.
      - No → refuse with exactly this line and nothing else: "抱歉,这超出我当前的工具、技能与代理能力范围。/ Sorry, that's outside my current tool / skill / agent capabilities." Do not give partial analysis, tips, workarounds, or "general advice"; do not describe what you would have said.
   2. Topics that MUST map to a registered capability or be refused (never answered from general knowledge): home safety / security / risk assessment, health, medical, legal, financial advice, recommendations about appliances or practices outside the devices listed above, any factual claim that depends on knowledge you did not retrieve via a tool this turn.

C. When you DO call a tool / skill / A2A agent:
   - If it succeeds, report the result honestly and mention which tool/skill/agent you used.
   - If it FAILS, is rejected, returns an error, or is unavailable, reply: "抱歉,这超出我的知识范围。/ Sorry, that's beyond my knowledge." You may add one short line suggesting the user retry later or contact an administrator. Do NOT substitute general-knowledge content for the missing tool output, and do NOT pretend the action succeeded.
   - Never fabricate or assume tool results.

D. TRANSPARENCY: Whenever you used a tool, skill, or A2A agent to answer, name it in your reply so the user knows which capability handled the request.

CRITICAL RULE — TOOL CALLING: When the user asks you to perform ANY action on devices (turn on, turn off, set mode, change settings, etc.), you MUST immediately call the appropriate tool in your VERY FIRST response. Do NOT describe what you plan to do, do NOT explain your steps, do NOT narrate your intentions — just call the tool directly. Action requests require tool calls, not text descriptions of tool calls.
IMPORTANT: Always send the device control command when the user asks, even if you believe the device is already in the requested state. You do not have real-time device state — always execute the command.
IMPORTANT: Do NOT list or describe devices from your own knowledge. You MUST use the discover_devices tool to find available devices. If that tool is unavailable or fails, apply rule C.

KNOWLEDGE BASE: Use query_knowledge_base for questions that may relate to company documents, product manuals, troubleshooting guides, or internal knowledge. Cite the source document when presenting information retrieved from the knowledge base.

IMAGES IN THIS CONVERSATION: When the user references an image they uploaded ("the image I just sent", "the photo", "上一张图片", "这张图"), rely on the image description that appears earlier in the conversation as a prior assistant message — that is the vision model's caption. Do NOT say "I cannot see images" or "I don't have image access"; the description is already in your context. If no image description is present, say so honestly and ask the user to re-upload. Never fabricate image contents; never invent colors, modes, or details that are not stated in a prior image description."""


def create_agent(tools=None, session_manager=None, skills=None, model_id=None, system_prompt=None):
    model = BedrockModel(
        model_id=model_id or MODEL_ID,
        region_name=AWS_REGION,
        streaming=True,
    )

    if skills:
        skills_plugin = AgentSkills(skills=skills)
    else:
        skills_plugin = _static_skills_plugin

    agent_kwargs = dict(
        model=model,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        plugins=[skills_plugin],
    )

    if tools:
        agent_kwargs["tools"] = tools
    if session_manager:
        agent_kwargs["session_manager"] = session_manager

    return Agent(**agent_kwargs)


def get_mcp_tools(mcp_client):
    tools = []
    pagination_token = None
    while True:
        result = mcp_client.list_tools_sync(pagination_token=pagination_token)
        tools.extend(result)
        if result.pagination_token is None:
            break
        pagination_token = result.pagination_token
    return tools


def _extract_sub_from_auth(auth_header: str | None) -> str | None:
    """Decode the Cognito `sub` from the forwarded idToken. Used to scope
    MCP tool calls (device control, device discovery) so the LLM cannot
    forge another user's identity — the sub comes from a token the runtime
    has already validated."""
    if not auth_header:
        return None
    try:
        import base64 as _b64
        token = auth_header
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1]
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(_b64.urlsafe_b64decode(payload))
        sub = claims.get("sub")
        return sub if sub else None
    except Exception:
        return None


def invoke_agent(prompt, session_id="default", actor_id="default", auth_header=None, headers=None):
    session_manager = get_memory_session_manager(session_id, actor_id)

    skills = None
    user_model_id = None
    user_system_prompt = None
    if SKILLS_TABLE_NAME:
        try:
            skills = load_skills_from_dynamodb(actor_id)
        except Exception as e:
            logger.warning(f"DynamoDB skill load failed, using filesystem fallback: {e}")
        try:
            settings = load_user_settings(actor_id)
            user_model_id = settings.get("modelId") or None
            if user_model_id:
                logger.info(f"Using per-user model: {user_model_id} for actor {actor_id}")
        except Exception as e:
            logger.warning(f"Failed to load user settings: {e}")
        try:
            user_system_prompt = load_system_prompt(actor_id, "text", headers=headers)
            if user_system_prompt:
                logger.info(f"Using per-user/global text system prompt override for actor {actor_id}")
        except Exception as e:
            logger.warning(f"Failed to load system prompt override, using default: {e}")

    if GATEWAY_URL:
        gw_headers = {}
        if auth_header:
            gw_headers["Authorization"] = auth_header
            logger.info("Forwarding user JWT to gateway for policy evaluation")
        else:
            logger.warning("No Authorization header available — gateway per-user policies won't apply")
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=gw_headers or None))
        with mcp_client:
            mcp_tools = get_mcp_tools(mcp_client)

            # Every MCP tool that reaches a per-user backend (KB, IoT, ...)
            # is wrapped so the agent's runtime-validated identity is injected
            # as an argument. The LLM cannot override these — its tool-call
            # arguments pass through our wrapper, which replaces the
            # user-scoping fields before hitting the Lambda.
            user_sub_from_jwt = _extract_sub_from_auth(auth_header)
            kb_user_id = actor_id if actor_id not in ("default", "__global__", "") else None

            from strands import tool as strands_tool
            import uuid as _uuid

            # MCP tool names come through with the Gateway target prefix
            # (e.g. SmartHomeDeviceControl___control_device). Match on the
            # trailing suffix so we don't accidentally leave the raw MCP tool
            # in-list alongside our wrapper, and remember the exact MCP name
            # so the wrapper can call the original tool by its real name.
            scoped_suffixes = ("query_knowledge_base", "control_device", "discover_devices")
            mcp_name_for = {}  # suffix -> actual MCP tool_name, e.g. "SmartHome___control_device"
            for t in mcp_tools:
                for s in scoped_suffixes:
                    if t.tool_name == s or t.tool_name.endswith("___" + s):
                        mcp_name_for[s] = t.tool_name
            def _is_scoped(name: str) -> bool:
                return any(name == s or name.endswith("___" + s) for s in scoped_suffixes)
            non_scoped_tools = [t for t in mcp_tools if not _is_scoped(t.tool_name)]
            present_suffixes = set(mcp_name_for.keys())

            wrapped_tools = []
            if "query_knowledge_base" in present_suffixes:
                @strands_tool
                def query_knowledge_base(query: str) -> str:
                    """Query the enterprise knowledge base to retrieve relevant documents.
                    Use this when users ask about company documents, product manuals,
                    troubleshooting guides, or internal knowledge."""
                    args = {"query": query}
                    if kb_user_id:
                        args["user_id"] = kb_user_id
                    result = mcp_client.call_tool_sync(
                        tool_use_id=str(_uuid.uuid4()),
                        name=mcp_name_for["query_knowledge_base"],
                        arguments=args,
                    )
                    if hasattr(result, 'content') and result.content:
                        texts = [c.text for c in result.content if hasattr(c, 'text')]
                        return "\n".join(texts) if texts else json.dumps(result.content, default=str)
                    return str(result)
                wrapped_tools.append(query_knowledge_base)
                logger.info(f"KB tool wrapped with user_id={kb_user_id} (LLM cannot override)")

            if "control_device" in present_suffixes:
                @strands_tool
                def control_device(device_type: str, command: dict) -> str:
                    """Send a command to one of the user's smart home devices. Returns
                    a short confirmation or error message. `device_type` is one of
                    led_matrix, rice_cooker, fan, oven. `command` is a JSON object with
                    an `action` field and action-specific parameters."""
                    args = {"device_type": device_type, "command": command}
                    if user_sub_from_jwt:
                        args["user_id"] = user_sub_from_jwt
                    result = mcp_client.call_tool_sync(
                        tool_use_id=str(_uuid.uuid4()),
                        name=mcp_name_for["control_device"],
                        arguments=args,
                    )
                    if hasattr(result, 'content') and result.content:
                        texts = [c.text for c in result.content if hasattr(c, 'text')]
                        return "\n".join(texts) if texts else json.dumps(result.content, default=str)
                    return str(result)
                wrapped_tools.append(control_device)

            if "discover_devices" in present_suffixes:
                @strands_tool
                def discover_devices() -> str:
                    """List the user's smart home devices and their supported actions."""
                    args = {}
                    if user_sub_from_jwt:
                        args["user_id"] = user_sub_from_jwt
                    result = mcp_client.call_tool_sync(
                        tool_use_id=str(_uuid.uuid4()),
                        name=mcp_name_for["discover_devices"],
                        arguments=args,
                    )
                    if hasattr(result, 'content') and result.content:
                        texts = [c.text for c in result.content if hasattr(c, 'text')]
                        return "\n".join(texts) if texts else json.dumps(result.content, default=str)
                    return str(result)
                wrapped_tools.append(discover_devices)

            if user_sub_from_jwt:
                logger.info(f"Device tools wrapped with user_sub={user_sub_from_jwt[:8]}... (LLM cannot override)")
            else:
                logger.warning("No user sub from JWT — device tools will hit Lambda without user scoping (likely to fail)")

            # Surface a small set of Strands built-in tools so skills like
            # weather-lookup (needs http_request) actually have the tool
            # they reference. Imported lazily so the runtime still boots if
            # strands_tools is absent. We intentionally do NOT register
            # strands_tools.agent_core_memory — it's a provider-style tool
            # (AgentCoreMemoryToolProvider) that needs per-session
            # instantiation and does not load as a plain module. The
            # session_manager already persists turns to Memory, so the
            # user-feedback skill records its marker as conversation text.
            builtin_tools = []
            try:
                from strands_tools import http_request as _sst_http_request
                builtin_tools.append(_sst_http_request)
            except Exception as e:
                logger.warning(f"http_request built-in not available: {e}")
            try:
                from strands_tools import file_write as _sst_file_write
                builtin_tools.append(_sst_file_write)
            except Exception as e:
                logger.warning(f"file_write built-in not available: {e}")

            # Browser-use tool: only register when the effective skill set
            # for this user includes "browser-use". The closure pins user_id
            # and agent_session_id — the LLM cannot forge either because
            # neither field appears in the tool's input schema.
            skill_names = {getattr(s, "name", "") for s in (skills or [])}
            if "browser-use" in skill_names:
                from tools.browser_use import run_browse_web as _run_browse_web
                _bound_user = actor_id
                _bound_session = session_id

                @strands_tool
                def browse_web(goal: str) -> str:
                    """Open a live browser and drive it to accomplish the user's goal.
                    The user can watch the browser in a side panel while it runs.
                    Returns a short text summary."""
                    return _run_browse_web(
                        goal=goal,
                        user_id=_bound_user,
                        agent_session_id=_bound_session,
                    )
                wrapped_tools.append(browse_web)
                logger.info(f"browse_web registered for actor={actor_id}")

            # A2A tools — only when the deploy step patched the A2A envs into
            # this runtime. Any failure here is soft: log and continue with no
            # A2A tools so the main agent path stays healthy.
            a2a_tools = []
            if (
                os.environ.get("A2A_M2M_SECRET_ARN")
                and os.environ.get("A2A_COGNITO_TOKEN_URL")
                and os.environ.get("REGISTRY_ID")
            ):
                try:
                    from tools.a2a import build_a2a_tools
                    from tools.a2a_auth import build_token_provider
                    grants = load_user_a2a_permissions(actor_id)
                    if grants:
                        a2a_tools = build_a2a_tools(
                            grants=grants,
                            registry_id=os.environ["REGISTRY_ID"],
                            token_provider=build_token_provider(),
                        )
                        logger.info(
                            f"A2A tools registered: {len(a2a_tools)} for actor={actor_id}"
                        )
                except Exception as e:
                    logger.warning(f"A2A tool registration failed (skipped): {e}")

            all_tools = non_scoped_tools + wrapped_tools + builtin_tools + a2a_tools

            # When A2A tools are registered, append a short hint so the LLM
            # knows to prefer the specialist over general knowledge. Non-A2A
            # turns see the original system prompt untouched.
            effective_system_prompt = user_system_prompt
            if a2a_tools and effective_system_prompt:
                effective_system_prompt = (
                    effective_system_prompt
                    + "\n\nYou also have access to specialist A2A agents via tools named `a2a_*`. "
                    + "When the user's question maps to one of those specialists, prefer the corresponding "
                    + "tool over general knowledge — each routes to a purpose-built agent with domain expertise."
                )

            agent = create_agent(tools=all_tools, session_manager=session_manager, skills=skills, model_id=user_model_id, system_prompt=effective_system_prompt)
            return str(agent(prompt))
    else:
        agent = create_agent(session_manager=session_manager, skills=skills, model_id=user_model_id, system_prompt=user_system_prompt)
        return str(agent(prompt))


def _extract_user_auth(context) -> str | None:
    """Read the chatbot-supplied idToken from the custom allowlisted header and
    format it as a Bearer header for downstream gateway MCP calls.

    Header name: X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken.
    We also keep the legacy Authorization-header path for local-dev invocations
    where the header is passed directly.
    """
    rh = getattr(context, "request_headers", None)
    if not rh:
        return None
    headers = rh

    # Custom header (current prod path under AWS_IAM auth). Search
    # case-insensitively across whatever case the runtime forwards.
    lowered = {str(k).lower(): v for k, v in headers.items()} if hasattr(headers, "items") else {}
    token = lowered.get("x-amzn-bedrock-agentcore-runtime-custom-authtoken")
    if token:
        return f"Bearer {token}" if not token.lower().startswith("bearer ") else token

    # Legacy: raw Authorization header (still used in some dev flows).
    return lowered.get("authorization")


_memory_client_singleton = None


def _memory_client():
    """Lazily build an AgentCore Memory client (short-term event writes)."""
    global _memory_client_singleton
    if _memory_client_singleton is None:
        try:
            from bedrock_agentcore.memory.client import MemoryClient
            _memory_client_singleton = MemoryClient(region_name=AWS_REGION)
        except Exception as e:
            logger.warning(f"Memory client init failed: {e}")
            _memory_client_singleton = False  # sentinel so we don't retry every turn
    return _memory_client_singleton or None


def _persist_vision_turn(session_id, actor_id, user_prompt, description, images, storage_entries=None):
    """Write the vision exchange to AgentCore Memory short-term events.

    messages: the user's prompt (or placeholder) and Haiku's description.
    metadata: a small fingerprint of the images (count, MIME types, sizes,
    sha256 prefix) — never the raw base64, which would blow up metadata limits
    and pollute future Kimi context. Failure is non-fatal: logs and returns.
    """
    memory_id = os.environ.get("MEMORY_SMARTHOMEMEMORY_ID", "")
    if not memory_id:
        return
    client = _memory_client()
    if not client:
        return
    try:
        from bedrock_agentcore.memory.models.filters import StringValue  # noqa: F401
    except Exception:
        StringValue = None  # type: ignore

    import base64 as _b64, hashlib as _hash
    fingerprints = []
    for idx, img in enumerate(images or [], start=1):
        if not isinstance(img, dict):
            continue
        mt = img.get("mediaType", "")
        data = img.get("data", "")
        try:
            raw = _b64.b64decode(data, validate=False) if isinstance(data, str) else b""
        except Exception:
            raw = b""
        sha = _hash.sha256(raw).hexdigest()[:16] if raw else ""
        fingerprints.append(f"{idx}:{mt}:{len(raw)}:{sha}")

    metadata = {}
    if fingerprints and StringValue is not None:
        # One metadata key per image (max 3 images, well within the 15-kv cap).
        for i, fp in enumerate(fingerprints, start=1):
            metadata[f"image_{i}"] = StringValue(stringValue=fp)
        metadata["image_count"] = StringValue(stringValue=str(len(fingerprints)))
        # If session-storage persisted the bytes, attach the relative path so
        # future agent features (a "re-examine image" tool, a UI viewer, etc.)
        # can locate the raw file.
        for i, entry in enumerate(storage_entries or [], start=1):
            if isinstance(entry, dict) and entry.get("path"):
                metadata[f"image_{i}_path"] = StringValue(stringValue=entry["path"])

    # Strands' AgentCoreMemoryConverter stores each message as a JSON-serialized
    # SessionMessage envelope, and on read calls json.loads on every event. If
    # we write plain strings here, list_messages() raises JSONDecodeError and
    # the entire short-term history is dropped — including this image turn.
    # Round-trip through the same converter so Kimi's next text turn sees it.
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    now_iso = _dt.now(_tz.utc).isoformat()

    def _envelope(text: str, role: str, msg_id: int) -> str:
        return _json.dumps({
            "message": {"role": role, "content": [{"text": text}]},
            "message_id": msg_id,
            "redact_message": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        })

    user_text = user_prompt or "[The user sent images without text.]"
    messages = [
        (_envelope(user_text, "user", 0), "USER"),
        (_envelope(description, "assistant", 1), "ASSISTANT"),
    ]
    try:
        client.create_event(
            memory_id=memory_id,
            actor_id=_sanitize_actor_id(actor_id),
            session_id=session_id,
            messages=messages,
            metadata=metadata or None,
        )
    except Exception as e:
        logger.warning(f"Memory create_event failed (non-fatal): {e}")


def _record_session(actor_id: str, session_id: str) -> None:
    if not RUNTIME_SESSIONS_TABLE_NAME:
        return
    try:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        # `kind` is a DynamoDB reserved word — alias via ExpressionAttributeNames.
        _get_dynamodb().Table(RUNTIME_SESSIONS_TABLE_NAME).update_item(
            Key={"userId": actor_id, "sessionKey": f"text#{session_id}"},
            UpdateExpression=(
                "SET #k = :k, sessionId = :s, lastActiveAt = :la, "
                "createdAt = if_not_exists(createdAt, :ca)"
            ),
            ExpressionAttributeNames={"#k": "kind"},
            ExpressionAttributeValues={
                ":k": "text",
                ":s": session_id,
                ":la": now_iso,
                ":ca": now_iso,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to record session: {e}")


@app.entrypoint
def handle_invocation(payload, context):
    """POST /invocations handler — text path (synchronous request/response)."""
    prompt = payload.get("prompt", payload.get("inputText", ""))
    images = payload.get("images") or []

    # Allow image-only turns: if prompt is empty but images are present, we
    # substitute a placeholder downstream so Kimi has something to reply to.
    if not prompt and not images:
        return {"error": "No prompt provided"}

    session_id = "default"
    if hasattr(context, "session_id") and context.session_id:
        session_id = context.session_id

    actor_id = payload.get("userId", "default")

    # Under AWS_IAM auth on the runtime, the `Authorization` header can't be
    # passthrough-allowlisted, so the chatbot sends the user's idToken in a
    # custom allowlisted header instead. We forward it to the CUSTOM_JWT gateway
    # MCP client as `Bearer <token>` for per-user Cedar policy evaluation.
    auth_header = _extract_user_auth(context)

    # Warm-up ping sent right after login — proves the runtime + JWT are healthy
    # without burning an LLM turn. Warmups never carry images.
    if prompt == "__warmup__":
        logger.info(f"Warmup invocation: actor_id={actor_id}, session_id={session_id}")
        return {"status": "warmup_ok"}

    # Image branch — respond directly from Claude Haiku (bypass Kimi) for
    # latency. The raw bytes are saved to the runtime's per-session
    # filesystem first (see agent/session_storage.py), then captioned, then
    # persisted to AgentCore Memory so follow-up text turns see the context.
    if images:
        if not isinstance(images, list) or len(images) > 3:
            return {"error": "Invalid images payload (max 3)."}
        logger.info(f"Vision invocation: actor_id={actor_id}, session_id={session_id}, images={len(images)}")
        _record_session(actor_id, session_id)

        # Persist first: save raw bytes so later features (or retries) can
        # recover the originals even if Haiku fails. Failures are non-fatal.
        storage_entries = []
        try:
            import session_storage
            import base64 as _b64
            for img in images:
                if not isinstance(img, dict):
                    storage_entries.append(None)
                    continue
                try:
                    raw = _b64.b64decode(img.get("data", "") or "", validate=False)
                    entry = session_storage.save_image(
                        session_id=session_id,
                        mime=img.get("mediaType", "application/octet-stream"),
                        raw=raw,
                        user_prompt=prompt or None,
                    )
                    storage_entries.append(entry)
                except Exception as e:
                    logger.warning(f"save_image failed for one image (non-fatal): {e}")
                    storage_entries.append(None)
        except Exception as e:
            logger.warning(f"session_storage import failed (non-fatal): {e}")
            storage_entries = [None] * len(images)

        import vision
        # Per-user vision model override (falls back to env VISION_MODEL_ID
        # inside caption_images if None). Read from the same __settings__ row
        # used for the text agent's modelId.
        user_vision_model = None
        try:
            settings = load_user_settings(actor_id)
            user_vision_model = settings.get("visionModelId") or None
            if user_vision_model:
                logger.info(f"Using per-user vision model: {user_vision_model} for actor {actor_id}")
        except Exception as e:
            logger.warning(f"Failed to load per-user vision model (using default): {e}")
        try:
            caption_text, warnings = vision.caption_images(images, prompt, model_id=user_vision_model)
        except Exception:
            logger.exception("Vision captioning raised")
            caption_text = "[Image(s) could not be analyzed at this time.]"
            warnings = (
                "Note: vision service was unavailable; please try again."
            )

        response_text = caption_text
        if warnings:
            response_text = f"{response_text}\n\n{warnings}"

        _persist_vision_turn(session_id, actor_id, prompt, response_text, images,
                             storage_entries=storage_entries)
        return {"response": response_text, "status": "success"}

    logger.info(f"Invocation: actor_id={actor_id}, session_id={session_id}")
    _record_session(actor_id, session_id)

    request_headers = getattr(context, "request_headers", None) or {}
    response = invoke_agent(prompt, session_id=session_id, actor_id=actor_id,
                            auth_header=auth_header, headers=request_headers)
    return {"response": response, "status": "success"}


if __name__ == "__main__":
    # Force the 'websockets' ASGI WS implementation. On the managed runtime we
    # observed uvicorn's default auto-detection picking a no-op WS handler,
    # causing WebSocket upgrade requests to be rejected with 400 Bad Request
    # even though the `websockets` library was installed.
    app.run(log_level="info", ws="websockets")
