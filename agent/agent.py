import os
import contextlib
import json
import logging
import re

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
from strands.models.bedrock import CacheConfig
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore import BedrockAgentCoreApp
from memory.session import (
    get_memory_session_manager,
    memory_session_id,
    _sanitize_actor_id,
)

# Which of the two Bedrock endpoints serves a given model, and the Strands
# provider that reaches it. The default model is Mantle-only, so this is not
# optional plumbing. See agent/model_provider.py.
import model_provider

# The Cognito group convention that carries A2A grants. Same file as
# shared/a2a_groups.py, copied because only `agent/` is packaged into this CodeZip;
# shared/tests/test_a2a_groups_parity.py holds them identical.
import a2a_groups
# Claim parsing lives in its own module so it can be imported and tested without
# pulling in strands/playwright/browser-use.
from a2a_grants import grants_from_user_token
# The generated half of the delegation prompt. Also import-light.
import a2a_prompt

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

# The web-search gateway, which is a SECOND gateway in a DIFFERENT region because
# the `web-search` connector is only offered in us-east-1. Its tools are reached
# with the same end-user idToken — the gateway's Cognito authorizer points at the
# us-west-2 user pool's discovery URL, which is region-independent.
#
# Read from an explicit variable rather than the AGENTCORE_GATEWAY_*_URL scan
# above: that scan takes the LAST match it happens to iterate over, so leaving
# this to it would intermittently swap the two gateways and drop every device
# tool. Empty means "no web search", which must stay a working configuration.
WEBSEARCH_GATEWAY_URL = os.environ.get("WEBSEARCH_GATEWAY_URL", "")

# Reached over Converse on `bedrock-runtime` via its cross-region inference profile
# (the bare `anthropic.claude-sonnet-4-6` id is not on-demand invocable). Bedrock
# Mantle is not integrated in this build — see agent/model_provider.py for what that
# would take. Keep in agreement with model_catalog.DEFAULT_MODEL_ID, the two writes
# in scripts/setup-agentcore.py and restore-text-runtime-config.py;
# agent/tests/test_default_model.py holds the four copies together.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
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
            # `modelEndpoint` rides along with the model it describes. It is a
            # HINT, not a requirement: an older row predating this field, or one a
            # deploy has rewritten, simply has no endpoint and model_provider
            # works it out instead.
            return {
                "modelId": item["modelId"],
                "modelEndpoint": item.get("modelEndpoint", ""),
            }
    return {}








def load_system_prompt(actor_id: str, agent_type: str,
                       headers: dict | None = None) -> str | None:
    """Resolve the active system prompt for this user/agent.

    Mode-aware. On the bundles runtime (ENABLE_BUNDLE_HOOK=1), this function
    returns None so the agent falls back to its hardcoded SYSTEM_PROMPT
    constant. A separate BeforeModelCallEvent hook will be registered by
    create_agent on that runtime (see bundle_config) to override
    system_prompt at model-call time from the request's W3C baggage header.
    Until that hook lands the bundles runtime simply uses the hardcoded
    constant. Keeping the two paths in different functions makes each
    runtime's prompt-resolution behavior single-purpose.

    On the default runtime, this is the §8.10 additive resolution:
      1. Read (__global__, __prompt_{type}__) → returns "" if missing.
      2. Read (actor_id,    __prompt_{type}__) → returns "" if missing.
      3. Concatenate non-empty parts with "\\n\\n". None when both empty.
    """
    if os.environ.get("ENABLE_BUNDLE_HOOK") == "1":
        return None

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
  1. Device control — turn devices on/off, set brightness, colour, mode, speed or temperature. Call discover_devices for the fleet and its valid parameters; never recite devices from memory.
  2. Device state & sensor readings — query_device_state returns what a device is doing RIGHT NOW (power, brightness, mode, sensor values, online/offline). query_sensor_history returns a metric over a time window with min / max / average / latest already computed. Use the first for "is it on" / "what is the temperature now", the second for trends and past values.
  3. Page navigation — navigate_to_page turns a request to open an app page ("打开群控页面", "open the automation page") into a link the client follows. Pass the user's own words; if nothing matches, the tool returns the available pages and you should offer those. Never write a link yourself.
  4. Specialist agents — tools named `a2a_<agent>_<skill>`, each a separate agent with its own domain: documentation and troubleshooting, lighting effects, automations and scenes, security, energy, appliance maintenance, multi-device orchestration. When one of them covers the request you MUST call it rather than answering yourself; see the routing rules at the end of this prompt, which override anything above. If no matching `a2a_*` tool is listed this turn, you do NOT have that domain's expertise and must refuse rather than improvise.
  5. Enterprise knowledge base — product manuals, troubleshooting guides, company documents, via query_knowledge_base. This is raw retrieval; when a knowledge-QA specialist is registered, prefer it.
  6. Image analysis — the user can attach photos or screenshots. Images are captioned upstream by a vision model; the caption is inserted into this conversation as a prior assistant message before your turn starts.

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
IMPORTANT: Always send the device control command when the user asks, even if query_device_state says the device is already in that state. A control request is an instruction, not a question.
IMPORTANT: Do NOT list or describe devices from your own knowledge. You MUST use the discover_devices tool to find available devices. If that tool is unavailable or fails, apply rule C.

DEVICE STATE AND SENSOR READINGS: Never state a device's current state, or a temperature / humidity / PM2.5 / CO2 / water level / filter life reading, from memory or from an earlier turn — call query_device_state (now) or query_sensor_history (over time) and report what came back. If the tool says a device has reported no state, tell the user the device simulator appears to be closed; do NOT report that as "off". When a reading has a unit, include it.

KNOWLEDGE BASE: Use query_knowledge_base for questions that may relate to company documents, product manuals, troubleshooting guides, or internal knowledge. Cite the source document when presenting information retrieved from the knowledge base.

IMAGES IN THIS CONVERSATION: When the user references an image they uploaded ("the image I just sent", "the photo", "上一张图片", "这张图"), rely on the image description that appears earlier in the conversation as a prior assistant message — that is the vision model's caption. Do NOT say "I cannot see images" or "I don't have image access"; the description is already in your context. If no image description is present, say so honestly and ask the user to re-upload. Never fabricate image contents; never invent colors, modes, or details that are not stated in a prior image description."""


# Structured output (spec 5 S6). Appended only when the caller asks for it.
#
# This product's end users are largely developers, and the natural-language reply
# they get is not scriptable: a device state arrives as prose about brightness
# rather than as a number they can assert on. Asking for JSON turns the agent into
# something a shell script can call.
#
# Prompt-level rather than a constrained-decoding feature, deliberately. The reply
# still comes from the same turn with the same tools, so a JSON request routes and
# delegates exactly as its prose equivalent would — one behaviour to reason about,
# not two. The cost is that compliance is not guaranteed, which is why the rules
# below are about what NOT to wrap it in: a fenced block or a "Here is the JSON:"
# preamble is the common failure and it breaks `JSON.parse` just as thoroughly as
# malformed JSON would.
JSON_OUTPUT_RULES = """OUTPUT FORMAT — JSON ONLY. This request came from a program,
not a person reading prose.

Reply with a single JSON value and NOTHING else:

  - No ``` fences. No "Here is the JSON:". No trailing commentary.
  - The first character of your reply must be `{` or `[`, and the last must be the
    matching close.
  - No comments, no trailing commas.

Shape it around the answer, and keep keys stable and machine-friendly
(lowerCamelCase, no spaces). Some useful conventions:

  - one device       -> {"deviceId": ..., "power": ..., "brightness": ...}
  - several devices  -> {"devices": [ ... ]}
  - an action taken  -> {"applied": [{"deviceId": ..., "action": ..., "ok": true}]}
  - a scene          -> the same shape the scenes export uses: name, description,
                        trigger, deviceActions
  - a failure        -> {"error": "<what went wrong>"} — an error is a JSON reply
                        too, never prose

Use real JSON types: numbers unquoted, booleans as true/false, absent values as
null rather than the string "null" or "unknown".

This changes only how you FORMAT the answer. Route, delegate and call tools exactly
as you otherwise would — including consulting a specialist when one covers the
request. If you must refuse, refuse in JSON: {"error": "..."}."""


# Appended to the system prompt only on turns where `a2a_*` tools are actually
# registered, so a user without grants is never told about specialists they
# cannot reach.
#
# The routing table is the point. A delegation costs at least two serial LLM
# calls — this agent deciding, then the sub-agent reasoning — plus the network
# round trips, which measures at 5-15s. Anything the main agent can do with one
# tool call must not be delegated, or every light switch pays for a conversation.
# Delegation is for work that genuinely needs a specialist: multi-device
# orchestration, capability reasoning, creative generation.
# The two hand-written halves of the delegation section. The TABLE between them is
# generated per turn from the granted AgentCards (a2a_prompt.build_routing_table),
# which is what makes granting a sub-agent sufficient — no prompt edit, no redeploy.
#
# What stays hand-written is what cannot be derived from a skill description: that a
# delegation costs at least two serial LLM calls plus network (measured 5-15s), so
# anything one tool call can do must not be delegated; that a match makes the call
# mandatory; routing on subject rather than phrasing; NOW versus LATER; the two-step
# choreography; and asking independent specialists in the same turn. Those came from
# measured behaviour, not from the cards.
A2A_DELEGATION_PREAMBLE = """SPECIALIST AGENTS (A2A) — ROUTING RULES. These OVERRIDE the
capability list above wherever the two disagree.

Some of your tools are named `a2a_<agent>_<skill>`. Each is a separate specialist
agent. When one of them covers the request, calling it is REQUIRED, not optional —
answering from your own knowledge instead is a failure even if your answer sounds
right, because the specialist is the part of this system that is governed,
auditable and kept up to date.

MATCH THE REQUEST TO A TOOL BY NAME. Read your tool list each turn and route:
"""

A2A_DELEGATION_EPILOGUE = """Route on the SUBJECT of the request, not on how it is phrased. "What animation
modes does the LED matrix support?" is a documentation question, so it goes to
knowledge-QA even though it names a device. "Turn the LED matrix off every night"
is an automation, so it goes to task-management even though turning something
off is normally yours.

NOW versus LATER separates the two scene specialists, and it is the only thing
that does. "Make the lights follow the music" is scene-sync: it happens while the
user is standing there. "Every night at 8, make the lights follow the music" is
task-management: it is a routine to store. Sending a live request to
task-management saves something and changes nothing, which reads as the feature
silently not working.

TWO-STEP REQUESTS. Some requests need one specialist's output as another's input,
and you are the one who carries it across — the specialists cannot call each other.

  - a feast the user then wants to keep: scene-sync applies it and names the
    actions it used; ASK whether to save it, and only if the user says yes pass
    those actions to task-management as a manual scene. Do not save it unasked —
    a user who wanted the lights on for an hour does not want a permanent button.
  - an automation that includes a lighting effect: get the effect parameters from
    light-effect first, then hand them to task-management to store.

Do the steps in that order and tell the user what each specialist did. One reply
covering both is fine; two round trips of tool calls is expected.

INDEPENDENT SPECIALISTS: ASK THEM TOGETHER. A two-step request is one where the
second specialist needs the first one's ANSWER. When the parts do not depend on
each other — "what's my security gap and how much could I save on energy" — call
both tools in the SAME turn rather than waiting for one before asking the next.
They run concurrently, so two independent questions cost about as long as one; done
one after another they cost double for no reason. Then report each specialist's
answer separately, and say which one said what.

Only serialise when the later question genuinely cannot be written without the
earlier answer.

DO IT YOURSELF — these are single, immediate, unambiguous actions on one device,
and delegating them only adds seconds:
  - Turn one device on or off, set its brightness, colour, mode, speed or temperature → control_device
  - What one device is doing right now, or a current sensor reading → query_device_state
  - A sensor's history, trend, min/max/average → query_sensor_history
  - Which devices exist → discover_devices
  - Open an app page → navigate_to_page

`query_knowledge_base` is the raw retrieval tool behind knowledge-QA. When
`a2a_knowledge_qa_agent_*` is in your tool list, PREFER IT — it retrieves and reads
the passages for you. Use `query_knowledge_base` directly only when no
knowledge-QA specialist is registered this turn.

IF NO TOOL MATCHES a domain the user asked about, say so plainly. Never answer a
security, energy, maintenance or documentation question from general knowledge —
that is exactly the case the refusal line exists for.

HOW TO DELEGATE: send the specialist a self-contained request in natural language.
It cannot see this conversation, so include the devices, rooms and parameters it
needs. Report back what it tells you and name the specialist you used. If it
returns an error or is unavailable, say so honestly — do not substitute your own
answer for the one it failed to give.

A specialist may hand back actions for you to perform — a saved scene returns
`pendingActions`. That extra hop is deliberate: it keeps every device command under
the same per-user authorisation as a command the user typed. The specialist has NOT
touched any device; it cannot.

So when a reply contains `pendingActions`, they are work assigned to you:

  1. Call `control_device` ONCE PER ENTRY, before you reply.
  2. Then report what the tool results actually said, device by device.

**Receiving `pendingActions` and describing them as done is a failure, not a
shortcut.** "Movie mode is running — strip at 20%, fan at speed 1" after zero
`control_device` calls is a false report: the lights never changed, and the user
finds out by looking at the room. If a `control_device` call fails, say which
device failed and why; a partial success reported as success is worse than a
failure.

The one exception is when the user asked only to SAVE a scene for later. Then say
it is saved and do not apply anything — but that is the user declining execution,
not you skipping it.
"""


def build_delegation_rules(grants: dict, cards: dict) -> str:
    """The delegation section for this turn, or "" when nothing is granted.

    A user with no specialists is told nothing about specialists — the same reason
    the section was always conditional, now applied per sub-agent rather than
    all-or-nothing.
    """
    table = a2a_prompt.build_routing_table(grants, cards)
    if not table:
        return ""
    return "\n".join([A2A_DELEGATION_PREAMBLE.rstrip(), "",
                       table, "", A2A_DELEGATION_EPILOGUE.lstrip()])



_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def unfence_json(text: str) -> str:
    """Strip a Markdown code fence from a JSON reply, if there is one.

    The prompt asks for bare JSON and forbids fences. It mostly works — a plain
    device-state question comes back unfenced — but measured on the live runtime, a
    DELEGATED turn came back as:

        ```json
        {"source": "a2a_home_security_agent_risk_assessment", ...}
        ```

    which is unsurprising: after summarising a specialist's prose the model is deep
    in chat-formatting mode, and the fence is what chat formatting does with JSON.
    Instructing harder is not the fix — the same lesson as the `⟦A2A:…⟧` marker
    (a2a-agent-registry/common/server.py): if a property must hold for every reply,
    the harness enforces it rather than the model.

    Conservative on purpose. Only an entire reply that is one fenced block is
    unwrapped, and only in JSON mode; the content is not parsed or re-serialised, so
    a reply this cannot fix passes through unchanged for the caller to reject rather
    than being silently mangled.
    """
    if not text:
        return text
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _strands_version() -> str:
    """The installed strands-agents version, or "?".

    Worth logging because `requirements.txt` pins only `>=1.25.0`, so the
    container's resolved version is whatever was current when the image was
    built — and a feature added after the floor is present locally and possibly
    not in the runtime.
    """
    try:
        from importlib.metadata import version

        return version("strands-agents")
    except Exception:  # noqa: BLE001
        return "?"


def create_agent(tools=None, session_manager=None, skills=None, model_id=None,
                 system_prompt=None, headers=None, model_endpoint=None):
    effective_model_id = model_id or MODEL_ID
    # Which Bedrock endpoint serves this model. `model_endpoint` is the hint the
    # admin console stored next to the model id; model_provider falls back to the
    # Mantle listing and then to Converse. See agent/model_provider.py.
    endpoint = model_provider.resolve_endpoint(
        effective_model_id, model_endpoint or "")
    model = model_provider.build_model(
        effective_model_id,
        endpoint,
        streaming=True,
        # Prompt caching (spec 5 S3). Measured on this deployment: the prefix in
        # front of every turn — system prompt (~1.6k tokens), the A2A routing
        # table (~1.7k), eleven governed skills (~4.3k) and ~20 tool schemas — is
        # ~10.5k input tokens, byte-identical on every call, and was being
        # reprocessed each time. With a cache point it is read from cache instead:
        # measured 15,839 billed input tokens down to 329, a 98% reduction.
        #
        # **This is a COST optimisation, not a latency one.** Measured
        # side-by-side, 12 alternating calls at ~15.6k prefix tokens: 2.11s
        # uncached against 2.06s cached, a 2% difference that is inside the noise.
        # AWS documents "up to 85% latency reduction" and that is presumably real
        # at much larger prefixes; at ours the prefill was never the bottleneck.
        # Recorded plainly because the spec asks for before/after numbers, and a
        # 98% token cut is worth having on its own — every turn of every user pays
        # this prefix.
        #
        # `strategy="auto"` asks Strands to detect whether the model supports
        # caching and place the cache points itself. That matters here because the
        # model is per-user configurable (Admin Console → Models): a hardcoded
        # cache point would fail on a model that does not support it, whereas auto
        # logs a warning and proceeds uncached. Cache hits need an EXACT prefix
        # match, which is why the static system prompt, skills and tools sit in
        # front and the user's message last — the order the prompt already used.
        #
        # Ignored on the Mantle path, which caches prefixes automatically and has
        # no cache point to place. The 98% figure above is therefore an
        # Anthropic-path measurement and does NOT describe the default model.
        cache_config=CacheConfig(strategy="auto"),
    )

    # Log once per agent build which model path was taken and whether caching
    # engaged, because when it does not the failure is silent in BOTH directions:
    # the answer is identical and the only trace is a CloudWatch metric that stays
    # at zero. Diagnosing it from the outside cost an hour — the spans carry no
    # cache fields, so `InputTokenCount` high + `CacheReadInputTokenCount` absent
    # was the only signal, and it is indistinguishable from "the code was never
    # deployed". The endpoint is in the line for the same reason: "why is this
    # model answering nothing" and "we routed it to the wrong endpoint" are
    # otherwise indistinguishable from outside.
    try:
        logger.info("%s", model_provider.describe(
            model, effective_model_id, endpoint, _strands_version()))
    except Exception as exc:  # noqa: BLE001 — diagnostics must never break a turn
        logger.info("model path: could not report state (%s)", exc)

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

    agent = Agent(**agent_kwargs)

    if os.environ.get("ENABLE_BUNDLE_HOOK") == "1":
        try:
            import bundle_config
            bundle_config.register_before_model_call_hook(agent, headers)
        except Exception as e:  # noqa: BLE001 — never break invocations
            logger.warning("failed to register bundle hook: %s", e)

    return agent


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


def invoke_agent(prompt, session_id="default", actor_id="default", auth_header=None,
                 headers=None, on_event=None, json_output=False):
    session_manager = get_memory_session_manager(session_id, actor_id)

    skills = None
    user_model_id = None
    user_model_endpoint = None
    user_system_prompt = None
    if SKILLS_TABLE_NAME:
        try:
            skills = load_skills_from_dynamodb(actor_id)
        except Exception as e:
            logger.warning(f"DynamoDB skill load failed, using filesystem fallback: {e}")
        try:
            settings = load_user_settings(actor_id)
            user_model_id = settings.get("modelId") or None
            user_model_endpoint = settings.get("modelEndpoint") or None
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
        # ExitStack rather than nested `with`: web search is a second gateway and a
        # second client, and it must be able to fail without taking the device
        # tools down with it. A nested `with` would put the whole tool-assembly
        # block one indent deeper for a capability that is optional.
        with contextlib.ExitStack() as _gw_stack:
            _gw_stack.enter_context(mcp_client)
            mcp_tools = get_mcp_tools(mcp_client)

            # Web search, from its own gateway. Appended to the same list so it
            # flows through the normal partition below: `WebSearch` is not in
            # `scoped_suffixes`, so it lands in `non_scoped_tools` and is handed to
            # the model unwrapped — correct, because it reads public pages and has
            # no user partition to inject.
            #
            # Per-user gating still happens: the gateway runs its own Cedar policy
            # engine in ENFORCE mode, so an ungranted user's `tools/list` does not
            # include it. That is why there is no skill-name check here.
            if WEBSEARCH_GATEWAY_URL:
                try:
                    ws_client = MCPClient(lambda: streamablehttp_client(
                        WEBSEARCH_GATEWAY_URL, headers=gw_headers or None))
                    _gw_stack.enter_context(ws_client)
                    ws_tools = get_mcp_tools(ws_client)
                    mcp_tools = mcp_tools + ws_tools
                    logger.info("web-search gateway: %d tool(s) available",
                                len(ws_tools))
                except Exception as e:  # noqa: BLE001
                    # Soft: the whole turn must not fail because an optional
                    # capability in another region is unreachable.
                    logger.warning("web-search gateway unavailable (skipped): %s", e)

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
            # Every tool that reads or writes data belonging to ONE user belongs
            # in this list. `navigate_to_page` deliberately does NOT: its result
            # is a static superapp:// link that is identical for every user, so
            # there is no identity to inject and no cross-user read to prevent.
            # Its absence here is a decision, not an omission — see the
            # nav-deeplink Lambda docstring.
            scoped_suffixes = (
                "query_knowledge_base", "control_device", "discover_devices",
                # Read half of the device link. Their Lambda partitions on the
                # caller's sub, so without a wrapper they would run unscoped and
                # be rejected for want of an identity.
                "query_device_state", "query_sensor_history",
            )
            mcp_name_for = {}  # suffix -> actual MCP tool_name, e.g. "SmartHome___control_device"
            for t in mcp_tools:
                for s in scoped_suffixes:
                    if t.tool_name == s or t.tool_name.endswith("___" + s):
                        mcp_name_for[s] = t.tool_name
            def _is_scoped(name: str) -> bool:
                return any(name == s or name.endswith("___" + s) for s in scoped_suffixes)
            non_scoped_tools = [t for t in mcp_tools if not _is_scoped(t.tool_name)]
            present_suffixes = set(mcp_name_for.keys())

            def _mcp_text(result) -> str:
                """Flatten an MCP tool result into the string a tool must return.

                `call_tool_sync` returns an MCPToolResult, which is a TypedDict —
                so the payload is `result["content"][i]["text"]`, not
                `result.content[i].text`. Only checking the attribute form meant
                every tool result reached the model as a stringified Python dict
                (`{'status': 'success', 'toolUseId': ..., 'content': [{'text':
                '<the actual JSON>'}]}`) with the answer buried inside it. The
                model was parsing through that wrapper, which is why this looked
                like it worked. Both shapes are handled since a future SDK
                version may return either.
                """
                content = None
                if isinstance(result, dict):
                    content = result.get("content")
                if content is None:
                    content = getattr(result, "content", None)
                if not content:
                    return str(result)
                texts = []
                for item in content:
                    if isinstance(item, dict):
                        if "text" in item:
                            texts.append(item["text"])
                    elif hasattr(item, "text"):
                        texts.append(item.text)
                if texts:
                    return "\n".join(texts)
                return json.dumps(content, default=str)

            def _call_scoped(suffix: str, args: dict) -> str:
                """Call a per-user MCP tool with the runtime-validated identity.

                `user_id` is set HERE rather than being a parameter, which is what
                keeps it out of the LLM-facing signature — the model cannot name
                another user's partition key because it never gets to supply one.
                """
                if user_sub_from_jwt:
                    args = {**args, "user_id": user_sub_from_jwt}
                return _mcp_text(mcp_client.call_tool_sync(
                    tool_use_id=str(_uuid.uuid4()),
                    name=mcp_name_for[suffix],
                    arguments=args,
                ))

            wrapped_tools = []
            if "query_knowledge_base" in present_suffixes:
                @strands_tool
                def query_knowledge_base(query: str) -> str:
                    """Query the enterprise knowledge base to retrieve relevant documents.
                    Use this when users ask about company documents, product manuals,
                    troubleshooting guides, or internal knowledge."""
                    # The KB scopes by EMAIL (actor_id), not by the Cognito sub the
                    # device tools use, so this one cannot go through
                    # _call_scoped — the two identifiers are not interchangeable.
                    args = {"query": query}
                    if kb_user_id:
                        args["user_id"] = kb_user_id
                    return _mcp_text(mcp_client.call_tool_sync(
                        tool_use_id=str(_uuid.uuid4()),
                        name=mcp_name_for["query_knowledge_base"],
                        arguments=args,
                    ))
                wrapped_tools.append(query_knowledge_base)
                logger.info(f"KB tool wrapped with user_id={kb_user_id} (LLM cannot override)")

            if "control_device" in present_suffixes:
                @strands_tool
                def control_device(device_id: str = "", device_type: str = "",
                                   command: dict | None = None) -> str:
                    """Send one command to one of the user's smart home devices.

                    Prefer `device_id` (an exact id from discover_devices, e.g.
                    bedroom-light-1) — it addresses a specific unit. `device_type`
                    is a fallback that resolves to the FIRST device of that type,
                    which is ambiguous now that several devices share a type.
                    `command` is an object with an `action` and that action's
                    parameters, e.g. {"action": "setBrightness", "brightness": 30}.
                    Out-of-range values are clamped and the reply says so — report
                    the value that was applied, not the one requested."""
                    args: dict = {"command": command or {}}
                    if device_id:
                        args["device_id"] = device_id
                    if device_type:
                        args["device_type"] = device_type
                    if not device_id and not device_type:
                        return ("control_device needs a device_id (preferred) or a "
                                "device_type; call discover_devices for the ids.")
                    return _call_scoped("control_device", args)
                wrapped_tools.append(control_device)

            if "discover_devices" in present_suffixes:
                @strands_tool
                def discover_devices() -> str:
                    """List the user's smart home devices and their supported actions."""
                    return _call_scoped("discover_devices", {})
                wrapped_tools.append(discover_devices)

            # Read half of the device link. The write half (control_device) has
            # existed since the beginning; without these the agent had no way to
            # answer "is that light on" or "what is the temperature" and would
            # either guess or refuse.
            if "query_device_state" in present_suffixes:
                @strands_tool
                def query_device_state(device_id: str = "", device_type: str = "") -> str:
                    """Read the CURRENT state of the user's devices — power, brightness,
                    mode, speed, sensor readings, and whether the device is online.
                    Pass `device_id` (from discover_devices) for one device, or neither
                    argument to read every device. Use this instead of guessing: a
                    device with no reported state comes back saying so, which means the
                    simulator is closed rather than that the device is off."""
                    args = {}
                    if device_id:
                        args["device_id"] = device_id
                    if device_type:
                        args["device_type"] = device_type
                    return _call_scoped("query_device_state", args)
                wrapped_tools.append(query_device_state)

            if "query_sensor_history" in present_suffixes:
                @strands_tool
                def query_sensor_history(device_id: str = "", metric: str = "",
                                         hours: int = 24) -> str:
                    """Read a sensor metric's readings over a time window, with min /
                    max / average / latest already computed. Use it for trends and past
                    values ("temperature over the last 24 hours"); use
                    query_device_state for the current value. `metric` is one of the
                    device's readable metrics (temperature, humidity, pm25, co2,
                    water_level, filter_life, bin_level) — omit it for all of them.
                    `hours` is 1 to 168."""
                    args = {"hours": hours}
                    if device_id:
                        args["device_id"] = device_id
                    if metric:
                        args["metric"] = metric
                    return _call_scoped("query_sensor_history", args)
                wrapped_tools.append(query_sensor_history)

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

            # Code Interpreter tool: registered only when the effective skill
            # set includes "code-interpreter". The closure pins user_id and
            # agent_session_id (neither appears in the LLM-facing schema), so
            # the model cannot forge identity — same guarantee as browse_web.
            if "code-interpreter" in skill_names:
                from tools.code_interpreter import run_execute_python as _run_exec
                _ci_user = actor_id
                _ci_session = session_id

                @strands_tool
                def execute_python(code: str, title: str = "") -> str:
                    """Run Python in a secure sandbox and return its output.
                    The user watches the code, streaming output, and any charts
                    live in the CodeInterpreter side panel. State persists across
                    calls in a turn, so build an analysis up over several blocks.
                    Save matplotlib figures to a file to have them rendered
                    inline. `title` is a short human label shown as the step
                    header."""
                    return _run_exec(
                        code=code,
                        title=title,
                        user_id=_ci_user,
                        agent_session_id=_ci_session,
                    )
                wrapped_tools.append(execute_python)
                logger.info(f"execute_python registered for actor={actor_id}")

            # A2A tools. Which sub-agents this user may reach comes from the
            # `cognito:groups` claim on their own token — the SAME claim each
            # sub-agent Runtime's authorizer checks (docs §9.5.1). Reading it here
            # rather than from DynamoDB is what makes what the model is offered and
            # what the platform will allow impossible to disagree about; the old DDB
            # read could show the model a tool that was then refused mid-turn.
            #
            # Any failure is soft: log and continue with no A2A tools so the main
            # agent path stays healthy.
            a2a_tools = []
            if os.environ.get("REGISTRY_ID"):
                try:
                    from tools.a2a import build_a2a_tools
                    grants = grants_from_user_token(auth_header)
                    if grants:
                        a2a_tools = build_a2a_tools(
                            grants=grants,
                            registry_id=os.environ["REGISTRY_ID"],
                            # The user's own token IS the credential now. The
                            # sub-agent's authorizer validates it and checks the
                            # grant claim, so there is no separate service token and
                            # no second header. Pinned in the tool closure, never a
                            # parameter the LLM can set.
                            user_token=auth_header,
                        )
                        logger.info(
                            f"A2A tools registered: {len(a2a_tools)} for actor={actor_id} "
                            f"across {len(grants)} sub-agent(s) from the token claim"
                        )
                    elif not auth_header:
                        logger.warning(
                            "No user token on this turn — sub-agent grants live in "
                            "its claims, so no a2a_* tool can be registered"
                        )
                except Exception as e:
                    logger.warning(f"A2A tool registration failed (skipped): {e}")

            all_tools = non_scoped_tools + wrapped_tools + builtin_tools + a2a_tools

            # When A2A tools are registered, append a short hint so the LLM knows
            # to prefer the specialist over general knowledge. Non-A2A turns see
            # the prompt untouched.
            #
            # The `and effective_system_prompt` guard used to mean this was only
            # appended when DynamoDB held a per-user or global override. A tenant
            # with no override — the default — fell back to the hardcoded
            # SYSTEM_PROMPT and never saw the hint, so its model was told nothing
            # about the specialists it had been granted. Fall back to SYSTEM_PROMPT
            # explicitly so both paths get it.
            effective_system_prompt = user_system_prompt or SYSTEM_PROMPT
            if a2a_tools:
                # The routing table is built from the cards this user was granted,
                # so a newly granted sub-agent appears in the prompt with no edit
                # here and no redeploy. `cards_by_name` is the same cached catalog
                # `build_a2a_tools` just used, so the two cannot disagree about
                # which agents exist.
                try:
                    from tools.a2a import cards_by_name

                    delegation = build_delegation_rules(
                        grants, cards_by_name(os.environ["REGISTRY_ID"]))
                except Exception as exc:  # noqa: BLE001
                    # A prompt without the table still has the tools and their
                    # descriptions, so this degrades rather than breaking the turn.
                    logger.warning(f"could not build the routing table: {exc}")
                    delegation = ""
                if delegation:
                    effective_system_prompt = (
                        effective_system_prompt + "\n\n" + delegation
                    )
            # LAST, so it wins on formatting. Everything before it — including an
            # admin's governed prompt — may ask for prose; the caller asking for
            # JSON is asking about the wire format, and the instruction nearest the
            # end is the one the model follows on a direct conflict. It also lands
            # after the cache point's stable prefix, so a JSON request does not
            # evict the cached prose prefix (S3).
            if json_output:
                effective_system_prompt += "\n\n" + JSON_OUTPUT_RULES

            agent = create_agent(tools=all_tools, session_manager=session_manager, skills=skills, model_id=user_model_id, model_endpoint=user_model_endpoint, system_prompt=effective_system_prompt, headers=headers)
            # `unfence_json` only when the caller asked for JSON: the model
            # sometimes wraps a delegated reply in a ```json fence despite the
            # prompt forbidding it, and a fence breaks JSON.parse exactly as
            # thoroughly as malformed JSON would.
            _post = unfence_json if json_output else (lambda t: t)
            if on_event is not None:
                return _post(_run_streamed(agent, prompt, on_event))
            return _post(str(agent(prompt)))
    else:
        # No Gateway configured, so no tools — but a caller can still ask for JSON,
        # and honouring it here keeps the flag's behaviour the same on both paths.
        no_gateway_prompt = user_system_prompt
        if json_output:
            no_gateway_prompt = (no_gateway_prompt or SYSTEM_PROMPT) + \
                "\n\n" + JSON_OUTPUT_RULES
        agent = create_agent(session_manager=session_manager, skills=skills, model_id=user_model_id, model_endpoint=user_model_endpoint, system_prompt=no_gateway_prompt, headers=headers)
        _post = unfence_json if json_output else (lambda t: t)
        if on_event is not None:
            return _post(_run_streamed(agent, prompt, on_event))
        return _post(str(agent(prompt)))


# ---------------------------------------------------------------------------
# Progress streaming (spec 5 S5)
# ---------------------------------------------------------------------------
#
# The spec proposed streaming the A2A hop through to the user, expecting TTFT to
# drop from ~30s to single digits. Measured against `Agent.stream_async`, that is
# not achievable and would not have helped:
#
#     +0.00s  init_event_loop
#     +1.88s  messageStart          <- first token of the turn
#     +1.88s  tool_use_stream       <- and it is a TOOL CALL, naming the specialist
#     +2.16s  message (toolUse complete)
#     +8.13s  first text delta      <- the first PROSE, after the tool returned
#
# The model cannot write its answer until the tool it just called comes back. So
# TTFT-to-prose is bounded below by the specialist's own latency no matter how the
# A2A hop is transported; streaming the sub-agent's tokens would deliver text the
# orchestrator has not finished reasoning about, into a UI with nowhere to put it.
#
# What IS available at 1.88s is the NAME of the specialist being consulted. That
# turns 31 seconds of a motionless "thinking…" into "asking the home security
# specialist…", which is the honest version of the same wait and arrives ~6s
# earlier than any text could. So this streams TOOL LIFECYCLE, not tokens.
#
# The final answer still arrives as one `answer` event rather than as deltas. Two
# reasons: the chatbot renders replies as markdown and a partially-streamed table
# or code fence renders as broken markup mid-flight; and the vision and
# image-to-effect paths return composed strings, so a token stream would be a
# second shape to handle for no gain.


def _run_streamed(agent, prompt: str, on_event) -> str:
    """Drive `agent` with stream_async, reporting progress, and return the text.

    `on_event(kind, detail)` is called for each notable step. It must never raise —
    a progress callback that breaks the turn would be worse than no progress at
    all — so every call is guarded.

    Falls back to a blocking call if streaming raises before producing a result.
    The reply is the product; progress is a nicety, and losing the turn to improve
    the waiting experience is the wrong trade.
    """
    import asyncio

    async def _drive() -> str:
        final = ""
        announced: set[str] = set()
        async for event in agent.stream_async(prompt):
            if not isinstance(event, dict):
                continue
            # A tool call, as soon as the model names it. `current_tool_use`
            # arrives repeatedly while the arguments stream in, so announce each
            # tool once.
            tool_use = event.get("current_tool_use") or {}
            name = tool_use.get("name") or ""
            if name and name not in announced:
                announced.add(name)
                _safe_emit(on_event, "tool", name)
            result = event.get("result")
            if result is not None:
                final = str(result)
        return final

    try:
        return asyncio.run(_drive())
    except Exception:
        logger.exception("streamed run failed; falling back to a blocking call")
        return str(agent(prompt))


def _safe_emit(on_event, kind: str, detail: str) -> None:
    try:
        on_event(kind, detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("progress callback raised (ignored): %s", exc)


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


# Words that turn "here is a photo" into "do something to my lights with it".
#
# A keyword list rather than asking the vision model to classify the intent: the
# classification would be one more thing to get wrong, and it would cost a token
# budget on every plain "what is this" to serve the minority of turns that want an
# effect. Cheap, inspectable, and wrong in the safe direction — a miss falls back
# to the caption, which is still a useful answer and which the user can act on with
# a second message.
#
# Both languages, because the deployment is used in both and a Chinese-only phrasing
# ("照这个做灯效") is the most likely way to ask.
LIGHT_EFFECT_KEYWORDS = (
    # English
    "light effect", "lighting effect", "light up", "lights up", "lighting",
    "ambience", "ambiance", "atmosphere", "mood light", "colour scheme",
    "color scheme", "palette", "match the lights", "match my lights",
    "set the lights", "make the lights", "light scene", "led", "strip",
    "backlight", "recreate", "like this photo", "like this image",
    # Chinese
    "灯效", "灯光", "氛围", "灯带", "背光", "配色", "色调", "调色",
    "打光", "照这个", "按这张", "按照这张", "仿照", "还原", "同款灯",
    "点亮", "灯光效果", "情景灯", "变成这个颜色", "这个颜色",
)


def wants_light_effect(prompt: str) -> bool:
    """Whether an image turn is asking for lighting rather than a description.

    Matched case-insensitively on substrings. Chinese needs substring matching
    anyway (no word boundaries), and using it for both keeps one rule instead of
    two.
    """
    text = (prompt or "").strip().lower()
    if not text:
        # An image with no words is "tell me what this is". Nobody attaches a photo
        # in silence and expects the lights to change.
        return False
    return any(kw in text for kw in LIGHT_EFFECT_KEYWORDS)


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
            # The STABLE memory session, not the runtime session this request
            # arrived on. Writing vision turns under the per-login id would put
            # them in a different session from the text turns, and the transcript
            # would have holes exactly where the user attached an image.
            session_id=memory_session_id(actor_id),
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

        # "Make a light effect from this photo" is a request the fast path cannot
        # answer. Captioning and returning describes the picture and changes
        # nothing, and the user has to ask a second time — which worked, because
        # the caption was by then in the conversation, but only if they knew to.
        #
        # So when the prompt asks for lighting, the caption becomes CONTEXT and the
        # turn continues into the agent loop, where it can reach the light-effect
        # specialist. Everything else keeps the fast path: "what is this" pays for
        # one model call, not two.
        if wants_light_effect(prompt):
            logger.info("image prompt asks for a lighting effect — continuing "
                        "into the agent loop with the caption as context")
            request_headers = getattr(context, "request_headers", None) or {}
            effect_prompt = (
                f"{prompt}\n\n"
                f"[A vision model has already described the image the user "
                f"attached. You cannot see the image itself; work from this "
                f"description, and say which parts of it you drew on.]\n"
                f"Image description: {caption_text}"
            )
            try:
                effect_response = invoke_agent(
                    effect_prompt, session_id=session_id, actor_id=actor_id,
                    auth_header=auth_header, headers=request_headers)
            except Exception:
                # The caption is a real answer on its own, so a failure here
                # degrades to it rather than losing the turn.
                logger.exception("image-to-effect continuation raised")
                return {"response": response_text, "status": "success"}
            return {"response": effect_response, "status": "success"}

        return {"response": response_text, "status": "success"}

    logger.info(f"Invocation: actor_id={actor_id}, session_id={session_id}")
    _record_session(actor_id, session_id)

    request_headers = getattr(context, "request_headers", None) or {}

    # Progress streaming, opt-in per request (spec 5 S5). A caller that asks for
    # it gets SSE — one `progress` event per tool the model calls, then one
    # `answer`. Everything else, including the voice path, the eval harness and
    # any existing client, keeps the plain JSON body it has always had.
    #
    # Opt-in rather than always-on because the response TYPE changes: returning a
    # generator makes BedrockAgentCoreApp emit text/event-stream, and a caller
    # doing `response.json()` on that gets a parse error rather than a reply. The
    # chatbot is updated in the same change; nothing else has to be.
    # Structured output (spec 5 S6): `responseFormat: "json"` appends the JSON
    # rules to the prompt. Accepted alongside `stream`, since a script may well
    # want both — progress on stderr, JSON on stdout.
    json_output = str(payload.get("responseFormat", "")).lower() == "json"

    if payload.get("stream"):
        return _stream_invocation(prompt, session_id, actor_id, auth_header,
                                  request_headers, json_output=json_output)

    response = invoke_agent(prompt, session_id=session_id, actor_id=actor_id,
                            auth_header=auth_header, headers=request_headers,
                            json_output=json_output)
    return {"response": response, "status": "success",
            # Echoed so a caller can tell a JSON-mode reply from a prose one
            # without re-reading its own request — a script that fed a prose reply
            # to `JSON.parse` would fail on the parse, not on the mode.
            **({"responseFormat": "json"} if json_output else {})}


def _stream_invocation(prompt, session_id, actor_id, auth_header, request_headers,
                       json_output=False):
    """Yield SSE frames: `progress` per tool call, then one `answer`.

    A generator, so BedrockAgentCoreApp wraps it in a StreamingResponse. The agent
    runs on a worker thread and pushes events into a queue this generator drains —
    `invoke_agent` is synchronous and Strands calls tools from its own threads, so
    a callback cannot yield directly from here.

    The `answer` frame always goes out, including on failure, where it carries the
    error text. A stream that just stops leaves the UI showing progress for a turn
    that will never finish, which is worse than an error the user can read.
    """
    import json as _json
    import queue
    import threading

    events: queue.Queue = queue.Queue()
    _DONE = object()

    def _run() -> None:
        try:
            answer = invoke_agent(
                prompt, session_id=session_id, actor_id=actor_id,
                auth_header=auth_header, headers=request_headers,
                on_event=lambda kind, detail: events.put((kind, detail)),
                json_output=json_output,
            )
            events.put(("answer", answer))
        except Exception as exc:  # noqa: BLE001
            logger.exception("streamed invocation failed")
            events.put(("error", str(exc)))
        finally:
            events.put(_DONE)

    threading.Thread(target=_run, name="stream-invoke", daemon=True).start()

    # Yield BARE JSON, not `data: ...\n\n`. BedrockAgentCoreApp adds the SSE
    # framing itself when a handler returns a generator, so a hand-framed string
    # arrives double-wrapped:
    #
    #     data: data: {"type": "progress", ...}\n\n\n\n
    #
    # which parses as the STRING 'data: {...}' rather than an object. Measured
    # against the live runtime; the client saw a str where it expected a dict and
    # would have silently dropped every frame including the answer.
    while True:
        item = events.get()
        if item is _DONE:
            break
        kind, detail = item
        if kind == "tool":
            yield _json.dumps({"type": "progress", "tool": detail})
        elif kind == "answer":
            yield _json.dumps({"type": "answer", "response": detail})
        elif kind == "error":
            yield _json.dumps({"type": "answer", "error": detail})


if __name__ == "__main__":
    # Force the 'websockets' ASGI WS implementation. On the managed runtime we
    # observed uvicorn's default auto-detection picking a no-op WS handler,
    # causing WebSocket upgrade requests to be rejected with 400 Bad Request
    # even though the `websockets` library was installed.
    app.run(log_level="info", ws="websockets")
