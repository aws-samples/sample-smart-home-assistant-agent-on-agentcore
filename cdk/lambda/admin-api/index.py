"""Admin API Lambda — CRUD operations for agent skills stored in DynamoDB,
plus per-user tool permission management via AgentCore Policy Engine."""

import json
import os
import re
import logging
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from concurrent import futures

import boto3
from boto3.dynamodb.conditions import Key

from agent_prompt_defaults import DEFAULTS as PROMPT_DEFAULTS
from a2a_prompt_defaults import A2A_DEFAULTS

import optimization  # AgentCore Optimization handlers; see optimization.py
import dashboard  # Overview ops-dashboard aggregation; see dashboard.py
# Copied in beside this file at build time by scripts/01-install-deps.sh — CDK
# packages each Lambda with Code.fromAsset(<dir>), so shared/ is not deployed.
import agent_registry as registry_ns
import model_catalog  # live Bedrock model catalog; see model_catalog.py
import chat_history  # Memory events -> a renderable transcript; see chat_history.py
import gateway_catalog  # which gateway exposes which tool; see gateway_catalog.py
# Copied from shared/ by scripts/01-install-deps.sh. The actor id is a Memory
# namespace component, so reading a transcript back requires naming the actor
# exactly as the agent named it when writing — a second sanitizer that differs by
# one character returns an empty transcript, not an error.
import memory_actor
import subagent_policy  # A2A grant intent -> Cognito groups; see subagent_policy.py
import a2a_conformance  # does a sub-agent's authorizer match its card? see that module
import a2a_manifest  # the contract we publish to third-party agent teams
import a2a_runtimes  # a card's url -> the AgentCore Runtime behind it

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
RUNTIME_SESSIONS_TABLE_NAME = os.environ.get(
    "RUNTIME_SESSIONS_TABLE_NAME", "smarthome-runtime-sessions"
)
SKILL_FILES_BUCKET = os.environ.get("SKILL_FILES_BUCKET", "")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
VOICE_RUNTIME_ARN = os.environ.get("VOICE_AGENT_RUNTIME_ARN", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
# The EPISODIC strategy's id. Episodes live under `/strategy/{id}/actor/{actor}/`
# rather than a user-scoped path, so listing them needs the id, which is minted with
# the strategy and patched in post-deploy alongside MEMORY_ID.
EPISODIC_STRATEGY_ID = os.environ.get("MEMORY_STRATEGY_EPISODIC_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
# The app client whose id a sub-agent's authorizer must carry in `allowedAudience`.
# Needed to tell an author what right looks like, and to spot a runtime pointed at a
# different client — which refuses a fully granted user with a message about
# `client_id`. See shared/a2a_conformance.py.
COGNITO_APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "")
REGISTRY_ID = os.environ.get("REGISTRY_ID", "")
KB_DOCS_BUCKET = os.environ.get("KB_DOCS_BUCKET", "")
# AOSS_ENDPOINT / AOSS_COLLECTION_ARN were dropped here: the knowledge base moved
# from OpenSearch Serverless to S3 Vectors, and nothing set or read them
# afterwards. Left in place they read as configuration this Lambda needs.
KB_SERVICE_ROLE_ARN = os.environ.get("KB_SERVICE_ROLE_ARN", "")
KB_ID = os.environ.get("KB_ID", "")
KB_DATA_SOURCE_ID = os.environ.get("KB_DATA_SOURCE_ID", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
runtime_sessions_table = dynamodb.Table(RUNTIME_SESSIONS_TABLE_NAME)
s3_client = boto3.client("s3", region_name=REGION)
agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)
# Gateway, Policy Engine, Runtime, Memory and Optimization. These did NOT move
# namespace at Agent Registry GA and must stay here — repointing this client
# wholesale would break all 15 of those calls.
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
# AWS Agent Registry only. Six call sites in this file use it; the rest keep the
# client above. The old namespace stops serving Registry on 2026-09-17.
registry_control = boto3.client(registry_ns.REGISTRY_CLIENT, region_name=REGION)
cognito_client = boto3.client("cognito-idp", region_name=REGION)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)
logs_client = boto3.client("logs", region_name=REGION)

# AgentCore emits GenAI spans to this log group with session.id +
# gen_ai.usage.total_tokens attributes on each LLM call span.
SPANS_LOG_GROUP = "aws/spans"

ALLOWED_FILE_DIRS = {"scripts", "references", "assets"}

# Strands SDK skill name pattern: lowercase alphanumeric + hyphens, 1-64 chars
SKILL_NAME_RE = re.compile(r"^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$")


def _json_default(o):
    # DynamoDB returns integer/float numbers as Decimal, which json.dumps
    # can't serialize out of the box. Convert to int when exact, else float.
    from decimal import Decimal
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def response(status_code, body):
    """Return an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=_json_default),
    }


def check_admin(event):
    """Return True if the caller belongs to the 'admin' Cognito group."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    groups = claims.get("cognito:groups", "")
    return "admin" in groups


def _caller_id(event) -> str:
    """Return the caller's stable identifier used as DDB userId.

    The agent and the chatbot both key DDB rows by email (falling back to the
    Cognito username / sub when email is absent). This helper mirrors that
    resolution so self-access checks on user-facing routes agree with how
    rows were written.
    """
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    return claims.get("email") or claims.get("cognito:username") or claims.get("sub") or ""


def _require_self_or_admin(event, target_user_id: str):
    """Return None if caller may act as `target_user_id`, else an error response."""
    if check_admin(event):
        return None
    if _caller_id(event) == target_user_id:
        return None
    return response(403, {"error": "Forbidden: caller is not target user or admin"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def list_skills(event):
    """GET /skills?userId=__global__

    When called with `?promptBundle=1`, returns the agent-prompt bundle
    `{userId, text, voice}` instead of the skill list — lets the Agent Prompt
    tab fetch both prompts in one call without adding a new API Gateway route
    (the admin Lambda's resource-policy is at the 20 KB cap).
    """
    params = event.get("queryStringParameters") or {}
    user_id = params.get("userId", "__global__")

    if params.get("promptBundle") == "1":
        # The bundle is the two built-in runtimes only. A sub-agent's prompt is
        # read one at a time from the agent detail page, because bundling all
        # eight would make the Prompt tab pay for six reads it does not render.
        out = {agent_type: _read_prompt_record(user_id, agent_type)
               for agent_type in BUILTIN_AGENT_TYPES}
        out["userId"] = user_id
        return response(200, out)

    resp = table.query(KeyConditionExpression=Key("userId").eq(user_id))
    items = resp.get("Items", [])
    # Filter out internal records (settings, sessions, permissions, etc.)
    items = [i for i in items if not i.get("skillName", "").startswith("__")]
    # Convert sets to lists for JSON serialisation
    for item in items:
        if "allowedTools" in item and isinstance(item["allowedTools"], set):
            item["allowedTools"] = list(item["allowedTools"])
    return response(200, {"skills": items})


def create_skill(event):
    """POST /skills  body: {userId, skillName, description, instructions, allowedTools, license, compatibility, metadata}"""
    body = json.loads(event.get("body") or "{}")
    user_id = body.get("userId", "__global__")
    skill_name = body.get("skillName", "")
    description = body.get("description", "")
    instructions = body.get("instructions", "")
    allowed_tools = body.get("allowedTools", [])
    skill_license = body.get("license", "")
    compatibility = body.get("compatibility", "")
    metadata = body.get("metadata", {})

    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return response(400, {
            "error": f"Invalid skillName '{skill_name}'. Must be 1-64 lowercase alphanumeric characters and hyphens, no leading/trailing/consecutive hyphens."
        })
    if not description:
        return response(400, {"error": "description is required"})
    if compatibility and len(compatibility) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if not isinstance(metadata, dict):
        return response(400, {"error": "metadata must be a key-value object"})

    ts = now_iso()
    item = {
        "userId": user_id,
        "skillName": skill_name,
        "description": description,
        "instructions": instructions,
        "allowedTools": allowed_tools,
        "createdAt": ts,
        "updatedAt": ts,
    }
    if skill_license:
        item["license"] = skill_license
    if compatibility:
        item["compatibility"] = compatibility
    if metadata:
        item["metadata"] = metadata

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(userId) AND attribute_not_exists(skillName)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return response(409, {"error": f"Skill '{skill_name}' already exists for userId '{user_id}'"})

    return response(201, {"message": f"Skill '{skill_name}' created", "userId": user_id, "skillName": skill_name})


def get_skill(event):
    """GET /skills/{userId}/{skillName}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")

    resp = table.get_item(Key={"userId": user_id, "skillName": skill_name})
    item = resp.get("Item")
    if not item:
        return response(404, {"error": f"Skill '{skill_name}' not found for userId '{user_id}'"})
    if "allowedTools" in item and isinstance(item["allowedTools"], set):
        item["allowedTools"] = list(item["allowedTools"])
    return response(200, item)


def update_skill(event):
    """PUT /skills/{userId}/{skillName}  body: {description, instructions, allowedTools, license, compatibility, metadata}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")

    if "compatibility" in body and body["compatibility"] and len(body["compatibility"]) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if "metadata" in body and not isinstance(body["metadata"], dict):
        return response(400, {"error": "metadata must be a key-value object"})

    # Build update expression dynamically
    update_parts = []
    expr_names = {}
    expr_values = {":updatedAt": now_iso()}
    update_parts.append("#updatedAt = :updatedAt")
    expr_names["#updatedAt"] = "updatedAt"

    for field in ("description", "instructions", "allowedTools", "license", "compatibility", "metadata"):
        if field in body:
            safe = f"#{field}"
            expr_names[safe] = field
            expr_values[f":{field}"] = body[field]
            update_parts.append(f"{safe} = :{field}")

    if len(update_parts) == 1:
        return response(400, {"error": "No fields to update."})

    try:
        table.update_item(
            Key={"userId": user_id, "skillName": skill_name},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(userId) AND attribute_exists(skillName)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return response(404, {"error": f"Skill '{skill_name}' not found for userId '{user_id}'"})

    return response(200, {"message": f"Skill '{skill_name}' updated", "userId": user_id, "skillName": skill_name})


def delete_skill(event):
    """DELETE /skills/{userId}/{skillName}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")

    table.delete_item(Key={"userId": user_id, "skillName": skill_name})

    # Cascade-delete S3 files for this skill
    if SKILL_FILES_BUCKET:
        prefix = f"{user_id}/{skill_name}/"
        try:
            resp = s3_client.list_objects_v2(Bucket=SKILL_FILES_BUCKET, Prefix=prefix)
            objects = resp.get("Contents", [])
            if objects:
                s3_client.delete_objects(
                    Bucket=SKILL_FILES_BUCKET,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
        except Exception as e:
            logger.warning(f"Failed to delete S3 files for {user_id}/{skill_name}: {e}")

    return response(200, {"message": f"Skill '{skill_name}' deleted", "userId": user_id, "skillName": skill_name})


def list_users(_event):
    """GET /skills/users — return distinct userIds in the table."""
    resp = table.scan(ProjectionExpression="userId")
    user_ids = sorted(set(
        item["userId"] for item in resp.get("Items", [])
        if not item["userId"].startswith("__")
    ))
    return response(200, {"userIds": user_ids})


def get_settings(event):
    """GET /settings/{userId} — return user settings.

    Two models plus the user's place in the world: `timezone` decides when "every
    night at 23:00" actually fires, and the coordinates are what make "at sunset"
    computable at all (see shared/solar.py).
    """
    path_params = event.get("pathParameters") or {}
    # unquote, like every other handler that takes a userId. An email in a path
    # arrives as `admin%40smarthome.local`, and writing that literal string as the
    # partition key produces a row the AGENT can never find — it reads by plain
    # email. That went unnoticed while these settings were only `modelId`, because
    # the console wrote and read the same escaped key; the coordinates broke the
    # symmetry, since the agent is the one that reads them.
    user_id = unquote(path_params.get("userId", ""))

    resp = table.get_item(Key={"userId": user_id, "skillName": "__settings__"})
    item = resp.get("Item")
    if not item:
        return response(200, {"userId": user_id, "modelId": "",
                              "modelEndpoint": "",
                              "visionModelId": "", "timezone": "",
                              "latitude": None, "longitude": None})
    return response(200, {
        "userId": item["userId"],
        "modelId": item.get("modelId", ""),
        # Which Bedrock endpoint serves modelId — "runtime" or "mantle". Stored
        # rather than derived because the AGENT is the consumer and it cannot call
        # this Lambda; see model_catalog.endpoint_for.
        "modelEndpoint": item.get("modelEndpoint", ""),
        "visionModelId": item.get("visionModelId", ""),
        "timezone": item.get("timezone", ""),
        # Decimal is not JSON-serialisable, and the coordinates are stored as
        # Decimal because DynamoDB rejects a Python float outright.
        "latitude": _coord_out(item.get("latitude")),
        "longitude": _coord_out(item.get("longitude")),
    })


def get_model_catalog(event):
    """GET /settings/{userId}?action=catalog — the live model picker catalog.

    Always 200, even when a listing failed: the response carries `catalogError`
    and whatever models were reachable. Failing the request would leave the Models
    page with nothing to render and no stated reason, which is the ambiguity this
    endpoint exists to remove.

    `?refresh=1` skips the container cache, for an admin who has just enabled a
    model in the Bedrock console and does not want to wait out the TTL.
    """
    params = event.get("queryStringParameters") or {}
    refresh = params.get("refresh") in ("1", "true", "yes")
    try:
        return response(200, model_catalog.catalog(refresh=refresh))
    except Exception as exc:  # noqa: BLE001
        logger.exception("model catalog failed")
        return response(200, {"models": [], "defaultModelId": model_catalog.DEFAULT_MODEL_ID,
                              "catalogError": str(exc)})


def _coord_out(value):
    """A stored coordinate as a JSON number, or None when unset."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_location(body: dict, existing: dict) -> tuple[dict, str]:
    """The location fields to store, or an error message.

    Validated here rather than in shared/scenarios.py: that module is pure and
    deliberately never reads DynamoDB, while these values live in a `__settings__`
    row. Latitude and longitude are stored as Decimal (DynamoDB rejects float) and
    only ever together — one without the other computes a sunrise for a place that
    is half real, which is worse than having none.
    """
    from decimal import Decimal, InvalidOperation

    out: dict = {}

    if "timezone" in body:
        tz = (body.get("timezone") or "").strip()
        if tz:
            # Checked against the runtime's own tz database, which is what
            # Scheduler is effectively validating against too. Accepting an
            # unknown name here would surface much later as a schedule that
            # refuses to save.
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
                return {}, (f"unknown timezone {tz!r} — use an IANA name such as "
                            f"'Asia/Shanghai' or 'America/Los_Angeles'")
            if len(tz) > 50:
                # Scheduler's ScheduleExpressionTimezone caps at 50 characters.
                return {}, f"timezone {tz!r} is longer than the 50-character limit"
        out["timezone"] = tz
    else:
        out["timezone"] = existing.get("timezone", "")

    for field, limit in (("latitude", 90), ("longitude", 180)):
        if field not in body:
            if existing.get(field) is not None:
                out[field] = existing[field]
            continue
        raw = body.get(field)
        if raw is None or raw == "":
            out[field] = None  # explicit clear
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return {}, f"{field} must be a number"
        if not -limit <= value <= limit:
            return {}, f"{field} must be between -{limit} and {limit}"
        out[field] = value

    lat, lon = out.get("latitude"), out.get("longitude")
    if (lat is None) != (lon is None):
        return {}, ("latitude and longitude must be set together — one without "
                    "the other cannot locate anything")
    return out, ""


def update_settings(event):
    """PUT /settings/{userId} — update user settings.

    Accepts `modelId` (text agent), `visionModelId` (image captioning),
    `timezone` (IANA name) and `latitude`/`longitude`. Only fields present in the
    request body are updated; absent fields retain their existing value so callers
    can patch one without clobbering the others. Passing null or "" for a
    coordinate clears it.
    """
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))  # see get_settings
    body = json.loads(event.get("body") or "{}")

    existing = table.get_item(Key={"userId": user_id, "skillName": "__settings__"}).get("Item") or {}
    model_id = body.get("modelId", existing.get("modelId", ""))
    vision_model_id = body.get("visionModelId", existing.get("visionModelId", ""))
    location, err = _validate_location(body, existing)
    if err:
        return response(400, {"error": err})
    ts = now_iso()

    # Resolve the endpoint here, at the moment the model is chosen, so the agent
    # reads it instead of working it out on a cold start. Resolution failing is not
    # a reason to reject the save: the agent falls back to its own lookup when this
    # is empty, whereas a 500 here would leave the admin unable to change models
    # because an unrelated listing was throttled.
    model_endpoint = body.get("modelEndpoint", "")
    if model_id and not model_endpoint:
        if model_id == existing.get("modelId"):
            model_endpoint = existing.get("modelEndpoint", "")
        if not model_endpoint:
            try:
                model_endpoint = model_catalog.endpoint_for(model_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not resolve endpoint for %s: %s", model_id, exc)
                model_endpoint = ""

    item = {
        "userId": user_id,
        "skillName": "__settings__",
        "modelId": model_id,
        "modelEndpoint": model_endpoint,
        "visionModelId": vision_model_id,
        "timezone": location["timezone"],
        "updatedAt": ts,
    }
    # Omit a cleared coordinate rather than writing null: `attribute_exists` is how
    # the agent tools ask "does this user have a location", and a null would answer
    # yes.
    for field in ("latitude", "longitude"):
        if location.get(field) is not None:
            item[field] = location[field]

    table.put_item(Item=item)
    return response(200, {
        "message": f"Settings updated for '{user_id}'",
        "modelId": model_id,
        "modelEndpoint": model_endpoint,
        "visionModelId": vision_model_id,
        "timezone": location["timezone"],
        "latitude": _coord_out(location.get("latitude")),
        "longitude": _coord_out(location.get("longitude")),
    })


# ---------------------------------------------------------------------------
# Agent System Prompts
# ---------------------------------------------------------------------------

# The two runtimes whose prompts ship in the agent image. Both have a built-in
# default in agent_prompt_defaults, so "Revert to Default" has something to show.
BUILTIN_AGENT_TYPES = ("text", "voice")
PROMPT_MAX_LEN = 16 * 1024  # 16 KB ceiling to avoid accidentally saving a doc

# Approved A2A agent names, cached per container. Populated from the Registry
# rather than hardcoded so a newly deployed sub-agent becomes governable without
# a Lambda change — the same reason the fleet page derives its list.
_A2A_PROMPT_AGENTS: set[str] | None = None


def _a2a_prompt_agents(refresh: bool = False) -> set[str]:
    """AgentCard names of the approved A2A agents, as prompt agent types.

    The card `name` is the key because it is the one identifier the console, the
    Registry record and the running sub-agent each derive independently — the
    runtime name and the directory slug are known to only some of the three.

    Cached for the container's life, since a roster change means a deploy anyway.
    """
    global _A2A_PROMPT_AGENTS
    if _A2A_PROMPT_AGENTS is not None and not refresh:
        return _A2A_PROMPT_AGENTS
    names: set[str] = set()
    if REGISTRY_ID:
        try:
            for rec in registry_ns.list_records(
                    registry_control, REGISTRY_ID,
                    record_type=registry_ns.RECORD_TYPE_AGENT,
                    status=registry_ns.STATUS_APPROVED):
                name = rec.get("displayName") or rec.get("name") or ""
                if name:
                    names.add(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not list A2A agents for prompt scope: %s", e)
            # Do NOT cache a failed lookup as an empty set: that would turn a
            # transient Registry error into "this agent does not exist" for the
            # rest of the container's life, and the caller would get a 400 that
            # looks like a bad request rather than a backend problem.
            return _A2A_PROMPT_AGENTS or set()
    _A2A_PROMPT_AGENTS = names
    return names


def _valid_agent_type(agent_type: str) -> bool:
    """True when `agent_type` names an agent whose prompt we govern.

    Re-reads the Registry on a miss before rejecting. A stale cache is exactly
    how the Cedar action map silently authorised nothing: an agent deployed after
    this container warmed up would otherwise be permanently unaddressable, and the
    400 would point at the caller rather than at the cache.
    """
    if not agent_type:
        return False
    if agent_type in BUILTIN_AGENT_TYPES:
        return True
    if agent_type in _a2a_prompt_agents():
        return True
    return agent_type in _a2a_prompt_agents(refresh=True)


def _agent_type_error(agent_type: str) -> dict:
    known = list(BUILTIN_AGENT_TYPES) + sorted(_a2a_prompt_agents())
    return response(400, {
        "error": f"unknown agentType {agent_type!r}; expected one of {known}",
    })


def _prompt_sk(agent_type: str) -> str:
    return f"__prompt_{agent_type}__"


def _read_prompt_record(user_id: str, agent_type: str) -> dict:
    """Return the editable record at `user_id` scope plus the read-only global
    context, for one (user, agent) pair.

    Fields:
      body            str — saved override at requested scope; "" if none
      updatedAt       str — timestamp of that saved override (empty if none)
      updatedBy       str — email of admin who last saved (empty if none)
      isOverride      bool — true iff body above came from a saved row
      globalBody      str — current body saved at __global__ scope (empty
                            if no global override). At user scope the UI
                            shows this read-only as additive context.
      builtinDefault  str — hardcoded default shipped with agent image. Used
                            by the Global editor's "Revert to Default" flow.

    Semantics match the agent runtime's additive `load_system_prompt`:
    final prompt = global_body + "\\n\\n" + user_body (any empty part omitted),
    with the built-in default used only when both are empty.
    """
    sk = _prompt_sk(agent_type)

    def _read(uid: str) -> dict:
        resp = table.get_item(Key={"userId": uid, "skillName": sk})
        item = resp.get("Item") or {}
        body = item.get("promptBody", "")
        return {
            "body": body if isinstance(body, str) else "",
            "updatedAt": item.get("updatedAt", ""),
            "updatedBy": item.get("updatedBy", ""),
            "isOverride": isinstance(body, str) and bool(body.strip()),
        }

    scope_rec = _read(user_id)
    # Always surface the current global body — at global scope it equals
    # scope_rec.body; at user scope it's read-only context shown above the
    # user's addendum editor.
    global_rec = scope_rec if user_id == "__global__" else _read("__global__")

    return {
        "body": scope_rec["body"],
        "updatedAt": scope_rec["updatedAt"],
        "updatedBy": scope_rec["updatedBy"],
        "isOverride": scope_rec["isOverride"],
        "globalBody": global_rec["body"],
        # The prompt the agent ships with, so the editor can show what an
        # override replaces and offer "Revert to Default". A sub-agent's lives in
        # a2a_prompt_defaults (generated from its system_prompt.md) because this
        # Lambda is packaged without the a2a-agent-registry tree.
        "builtinDefault": PROMPT_DEFAULTS.get(agent_type) or A2A_DEFAULTS.get(agent_type, ""),
    }


def _agent_type_from_sk(skill_name: str) -> str:
    """Extract agentType from reserved sort key `__prompt_<agentType>__`."""
    if skill_name.startswith("__prompt_") and skill_name.endswith("__"):
        return skill_name[len("__prompt_"):-2]
    return ""


def get_prompt_record(event):
    """GET /skills/{userId}/__prompt_{agentType}__ — single prompt record.

    Dispatched from the existing /skills/{userId}/{skillName} GET handler when
    `skillName` matches the reserved prompt sort key. Returns the prompt
    record shape ({body, updatedAt, updatedBy, isDefault}) rather than the
    full skill shape.
    """
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    skill_name = path_params.get("skillName", "")
    agent_type = _agent_type_from_sk(skill_name)
    if not _valid_agent_type(agent_type):
        return _agent_type_error(agent_type)
    rec = _read_prompt_record(user_id, agent_type)
    rec["userId"] = user_id
    rec["agentType"] = agent_type
    return response(200, rec)


def save_prompt_record(event):
    """POST /skills or PUT /skills/{userId}/{skillName} — upsert prompt override.

    Dispatched from the existing skills POST/PUT handlers when `skillName`
    matches the reserved prompt sort key. Body: {userId?, skillName?,
    promptBody}. Validates length (≤16 KB) and non-empty.
    """
    body_obj = json.loads(event.get("body") or "{}")
    path_params = event.get("pathParameters") or {}

    user_id = body_obj.get("userId") or unquote(path_params.get("userId", ""))
    skill_name = body_obj.get("skillName") or path_params.get("skillName", "")
    agent_type = _agent_type_from_sk(skill_name)
    prompt_body = body_obj.get("promptBody", "")

    if not user_id:
        return response(400, {"error": "userId is required"})
    if not _valid_agent_type(agent_type):
        return _agent_type_error(agent_type)
    if not isinstance(prompt_body, str) or not prompt_body.strip():
        return response(400, {"error": "promptBody is required and must be non-empty"})
    if len(prompt_body) > PROMPT_MAX_LEN:
        return response(400, {"error": f"promptBody exceeds {PROMPT_MAX_LEN} character limit"})

    # Capture who made the change — the admin's email from their Cognito claims.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    updated_by = claims.get("email") or claims.get("cognito:username") or claims.get("sub", "")

    ts = now_iso()
    table.put_item(Item={
        "userId": user_id,
        "skillName": _prompt_sk(agent_type),
        "promptBody": prompt_body,
        "updatedAt": ts,
        "updatedBy": updated_by,
    })
    return response(200, {
        "message": f"{agent_type} prompt saved for '{user_id}'",
        "updatedAt": ts,
        "updatedBy": updated_by,
    })


def delete_prompt_record(event):
    """DELETE /skills/{userId}/{skillName} — remove prompt override.

    Dispatched from the existing skills DELETE handler when `skillName`
    matches the reserved prompt sort key. The agent will fall back to
    __global__ or the hardcoded default on its next invocation.
    """
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    skill_name = path_params.get("skillName", "")
    agent_type = _agent_type_from_sk(skill_name)
    if not _valid_agent_type(agent_type):
        return _agent_type_error(agent_type)
    table.delete_item(Key={"userId": user_id, "skillName": _prompt_sk(agent_type)})
    return response(200, {"message": f"{agent_type} prompt override removed for '{user_id}'"})


def _fetch_token_totals_7d():
    """Query CloudWatch Logs Insights over the last 7 days for the sum of
    gen_ai.usage.total_tokens per session.id.

    AgentCore Runtime exports Strands/ADOT spans to the `aws/spans` log group.
    Each `chat` span carries both `attributes.session.id` and
    `attributes.gen_ai.usage.total_tokens` — summing the latter grouped by the
    former yields per-session token consumption shown in the CloudWatch
    GenAI Observability dashboard.

    Returns: dict {sessionId: int}. On failure (e.g. query timeout, permission
    issue) returns {} so the sessions list still renders without token data.
    """
    end_time = int(time.time())
    start_time = end_time - 7 * 24 * 3600
    # `aws/spans` is an account-wide log group — around forty unrelated runtimes
    # share it in this account. Without a service filter this sums every project's
    # tokens and attributes them to our sessions; it happens to be harmless today
    # only because no other project logged Strands token spans in the window.
    # dashboard.py already derives the exact list from the runtime ARNs, so a new
    # sub-agent is covered by DASHBOARD_EXTRA_RUNTIME_ARNS with no code change.
    #
    # Grouped by service.name as well as session.id so a row can be attributed to
    # one agent rather than to the fleet.
    #
    # Measured caveat, worth stating because it bounds what this page can claim: a
    # sub-agent's runtime stamps its OWN session id (a bare UUID) rather than
    # inheriting the orchestrator's `user-session-*`. AgentCore assigns
    # runtimeSessionId per runtime, and the A2A hop does not propagate it. So a
    # delegated turn's tokens are recorded under a session id this table has no row
    # for, and they surface in the per-agent split of a session that is not listed
    # here rather than alongside the orchestrator turn that caused them. Joining
    # the two would need the orchestrator to pass its session id across the A2A
    # hop; until then, per-agent totals are correct and per-TURN attribution across
    # a delegation is not available.
    query = (
        'filter scope.name = "strands.telemetry.tracer"\n'
        + dashboard._spans_service_filter() +
        '| filter ispresent(attributes.gen_ai.usage.total_tokens)\n'
        '| stats sum(attributes.gen_ai.usage.total_tokens) as totalTokens '
        'by attributes.session.id as sessionId, '
        'resource.attributes.service.name as serviceName\n'
        '| limit 10000'
    )
    groups = dashboard._spans_log_groups()
    if not groups:
        logger.info("no span log group exists; returning empty token totals")
        return {}
    try:
        start = logs_client.start_query(
            # Both the per-runtime groups (where spans go since 2026-08-05) and
            # the legacy account-wide one, so a 7d window spanning the cutover is
            # whole. See dashboard.LEGACY_SPANS_LOG_GROUP.
            logGroupNames=groups,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = start["queryId"]
    except logs_client.exceptions.ResourceNotFoundException:
        logger.info("span log group vanished between check and query; "
                    "returning empty token totals")
        return {}
    except Exception as e:
        logger.warning("Logs Insights start_query failed: %s", e)
        return {}

    # Poll for up to ~20s — Lambda timeout is 30s and the rest of list_sessions
    # is fast, so we can afford to wait.
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            res = logs_client.get_query_results(queryId=query_id)
        except Exception as e:
            logger.warning("Logs Insights get_query_results failed: %s", e)
            return {}
        status = res.get("status", "")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            if status != "Complete":
                logger.warning("Logs Insights query ended with status=%s", status)
                return {}
            break
        time.sleep(0.5)
    else:
        logger.warning("Logs Insights query %s timed out client-side", query_id)
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:
            pass
        return {}

    # {sessionId: {"total": int, "byAgent": {agentId: int}}}. The rows now arrive
    # split by service.name, so they are summed into a total AND kept per agent —
    # a delegated turn spends tokens in the sub-agent's runtime under the same
    # session id, and folding those into one number is what made per-agent cost
    # unanswerable from this page.
    totals: dict[str, dict] = {}
    for row in res.get("results", []):
        record = {field["field"]: field["value"] for field in row}
        session_id = record.get("sessionId") or record.get("attributes.session.id", "")
        if not session_id:
            continue
        try:
            tokens = int(float(record.get("totalTokens", "0")))
        except (TypeError, ValueError):
            continue
        service = (record.get("serviceName")
                   or record.get("resource.attributes.service.name", ""))
        # `smarthome_smarthome.DEFAULT` -> `smarthome`: the same agent id the
        # fleet page uses, so a token figure here can be matched to a row there.
        agent_id = _agent_id_from_service_name(service)
        slot = totals.setdefault(session_id, {"total": 0, "byAgent": {}})
        slot["total"] += tokens
        if agent_id:
            slot["byAgent"][agent_id] = slot["byAgent"].get(agent_id, 0) + tokens
    return totals


def _agent_id_from_service_name(service: str) -> str:
    """`smarthome_smarthome.DEFAULT` -> `smarthome`, matching the fleet's agentId.

    Mirrors agents.py: the runtime name is `<project>_<runtime>` and the project
    half is the id, except where the two halves differ (`smarthome_bundles`), in
    which case the full name is the id so the A/B variant does not collapse onto
    the orchestrator.
    """
    name = (service or "").split(".")[0]
    if not name:
        return ""
    head, _, tail = name.partition("_")
    return (name if tail and tail != head else head) or name


def list_sessions(_event):
    """GET /sessions — list every per-login AgentCore Runtime session in the
    dedicated runtime-sessions table (one row per session, never overwritten),
    enriched with past-7-day token totals from CloudWatch Logs Insights."""
    sessions = []
    scan_kwargs = {}
    while True:
        resp = runtime_sessions_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            sessions.append({
                "userId": item.get("userId", ""),
                "sessionId": item.get("sessionId", ""),
                "lastActiveAt": item.get("lastActiveAt", ""),
                "kind": item.get("kind", "text"),
            })
        token = resp.get("LastEvaluatedKey")
        if not token:
            break
        scan_kwargs["ExclusiveStartKey"] = token
    sessions.sort(key=lambda s: s.get("lastActiveAt", ""), reverse=True)

    token_totals = _fetch_token_totals_7d()
    for s in sessions:
        entry = token_totals.get(s["sessionId"]) or {}
        s["totalTokens7d"] = entry.get("total", 0)
        # Per-agent split, so a session's spend can be attributed to the
        # orchestrator versus the specialists it delegated to. Empty when the
        # session predates the split or made no delegated call.
        s["tokensByAgent"] = entry.get("byAgent", {})

    return response(200, {"sessions": sessions})


def stop_session(event):
    """POST /sessions/{sessionId}/stop?kind=text|voice — stop an AgentCore runtime session.

    Text and voice sessions share the same sessionId (chatbot derives it from
    the Cognito sub) but live on separate runtimes. The caller must supply
    `kind` in the query string so we know which runtime ARN to target.
    Defaults to text for backwards-compat if omitted.
    """
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("sessionId", "")
    qs = event.get("queryStringParameters") or {}
    kind = (qs.get("kind") or "text").lower()

    if not session_id:
        return response(400, {"error": "sessionId is required"})
    if kind not in ("text", "voice"):
        return response(400, {"error": "kind must be 'text' or 'voice'"})

    target_arn = VOICE_RUNTIME_ARN if kind == "voice" else RUNTIME_ARN
    if not target_arn:
        env_key = "VOICE_AGENT_RUNTIME_ARN" if kind == "voice" else "AGENT_RUNTIME_ARN"
        return response(500, {"error": f"{env_key} not configured"})

    stopped_ok = False
    not_found = False
    err_msg = ""
    try:
        agentcore_client.stop_runtime_session(
            runtimeSessionId=session_id,
            agentRuntimeArn=target_arn,
        )
        stopped_ok = True
    except agentcore_client.exceptions.ResourceNotFoundException:
        not_found = True
    except Exception as e:
        err_msg = str(e)

    if err_msg:
        return response(500, {"error": f"Failed to stop session: {err_msg}"})

    # Always clean up the DynamoDB record — whether the runtime session was
    # active (stopped_ok) or had already idle-expired (not_found). Stale
    # records would otherwise linger in the admin list forever because the
    # runtime-sessions table is append-only per login.
    uid = _resolve_user_for_session(session_id, kind)
    if uid:
        try:
            runtime_sessions_table.delete_item(
                Key={"userId": uid, "sessionKey": f"{kind}#{session_id}"}
            )
        except Exception as e:
            logger.warning(f"stop_session: record delete skipped: {e}")

    if stopped_ok:
        return response(200, {"message": f"Session '{session_id}' ({kind}) stop requested"})
    return response(200, {"message": f"Session '{session_id}' ({kind}) already stopped; record cleared"})


def _resolve_user_for_session(session_id: str, kind: str) -> str:
    """Look up the userId of the matching session record so stop can delete it.

    Returns "" if not found — delete_item will then be a harmless no-op.
    """
    try:
        # `kind` is a DynamoDB reserved word — alias via ExpressionAttributeNames.
        scan_resp = runtime_sessions_table.scan(
            FilterExpression="sessionId = :sid AND #k = :k",
            ExpressionAttributeNames={"#k": "kind"},
            ExpressionAttributeValues={":sid": session_id, ":k": kind},
            ProjectionExpression="userId",
        )
        items = scan_resp.get("Items", [])
        return items[0].get("userId", "") if items else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# User & Tool Permission Management
# ---------------------------------------------------------------------------

def create_cognito_user(event):
    """POST /users with body {action: 'create', email}.
    Creates the user with MessageAction=SUPPRESS (no invite email), then
    immediately sets a randomly generated 16-char permanent password. The
    password is returned in the response — admin shows it once to the
    user, who can sign in directly without a forced password change."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})
    body = json.loads(event.get("body") or "{}")
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return response(400, {"error": "Valid email required"})
    try:
        # 16-char password mixing upper / lower / digit / symbol — satisfies
        # any reasonable Cognito password policy without a custom check.
        import secrets, string
        alphabet_upper = string.ascii_uppercase
        alphabet_lower = string.ascii_lowercase
        alphabet_digit = string.digits
        alphabet_symbol = "!@#$%^&*"
        # Guarantee at least one of each class, then fill with mixed chars.
        guaranteed = [
            secrets.choice(alphabet_upper),
            secrets.choice(alphabet_lower),
            secrets.choice(alphabet_digit),
            secrets.choice(alphabet_symbol),
        ]
        pool = alphabet_upper + alphabet_lower + alphabet_digit + alphabet_symbol
        rest = [secrets.choice(pool) for _ in range(12)]
        chars = guaranteed + rest
        # Shuffle (Fisher-Yates via secrets.randbelow).
        for i in range(len(chars) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            chars[i], chars[j] = chars[j], chars[i]
        password = "".join(chars)

        resp = cognito_client.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        cognito_client.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            Password=password,
            Permanent=True,
        )
        u = resp.get("User", {})
        return response(200, {
            "username": u.get("Username"),
            "email": email,
            # Status will be CONFIRMED after AdminSetUserPassword(Permanent=True),
            # but the AdminCreateUser response captured the pre-set value.
            # Return CONFIRMED explicitly so the UI doesn't have to refresh.
            "status": "CONFIRMED",
            "password": password,
        })
    except cognito_client.exceptions.UsernameExistsException:
        return response(409, {"error": f"User {email} already exists"})
    except Exception as e:
        return response(500, {"error": str(e)})


def add_user_to_admin_group(event):
    """POST /users with body {action: 'add-to-admin', username}.
    Idempotent — AdminAddUserToGroup is a no-op if already a member."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})
    body = json.loads(event.get("body") or "{}")
    username = (body.get("username") or "").strip()
    if not username:
        return response(400, {"error": "username required"})
    try:
        cognito_client.admin_add_user_to_group(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            GroupName="admin",
        )
        return response(200, {"ok": True})
    except cognito_client.exceptions.UserNotFoundException:
        return response(404, {"error": f"User {username} not found"})
    except Exception as e:
        return response(500, {"error": str(e)})


def remove_user_from_admin_group(event):
    """POST /users with body {action: 'remove-from-admin', username}.
    Idempotent — AdminRemoveUserFromGroup is a no-op if not a member."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})
    body = json.loads(event.get("body") or "{}")
    username = (body.get("username") or "").strip()
    if not username:
        return response(400, {"error": "username required"})
    try:
        cognito_client.admin_remove_user_from_group(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            GroupName="admin",
        )
        return response(200, {"ok": True})
    except cognito_client.exceptions.UserNotFoundException:
        return response(404, {"error": f"User {username} not found"})
    except Exception as e:
        return response(500, {"error": str(e)})


def delete_cognito_user(event):
    """POST /users with body {action: 'delete', username}.
    Leaves per-user data (DynamoDB settings, AgentCore memory, KB S3 prefix)
    orphaned — admins can clean those up separately if needed."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})
    body = json.loads(event.get("body") or "{}")
    username = (body.get("username") or "").strip()
    if not username:
        return response(400, {"error": "username required"})
    try:
        cognito_client.admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
        )
        return response(200, {"ok": True})
    except cognito_client.exceptions.UserNotFoundException:
        return response(404, {"error": f"User {username} not found"})
    except Exception as e:
        return response(500, {"error": str(e)})


def list_cognito_users(_event):
    """GET /users — list all users from Cognito user pool."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})

    users = []
    params = {"UserPoolId": COGNITO_USER_POOL_ID, "Limit": 60}
    while True:
        resp = cognito_client.list_users(**params)
        for u in resp.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            # Fetch groups for this user
            groups = []
            try:
                g_resp = cognito_client.admin_list_groups_for_user(
                    UserPoolId=COGNITO_USER_POOL_ID,
                    Username=u["Username"],
                )
                groups = [g["GroupName"] for g in g_resp.get("Groups", [])]
            except Exception:
                pass
            users.append({
                "username": u["Username"],
                "email": attrs.get("email", ""),
                "sub": attrs.get("sub", u["Username"]),
                "status": u.get("UserStatus", ""),
                "createdAt": u.get("UserCreateDate", "").isoformat() if hasattr(u.get("UserCreateDate", ""), "isoformat") else str(u.get("UserCreateDate", "")),
                "groups": groups,
            })
        token = resp.get("PaginationToken")
        if not token:
            break
        params["PaginationToken"] = token

    return response(200, {"users": users})


# Curated list of Strands SDK + AgentCore built-in tools we expose to admins
# for per-user permissioning. Keep this short and safe-by-default: anything
# that touches a shell, spawns an LLM, or browses the web is omitted unless
# there's a clear operational reason an admin would enable it on a per-user
# basis. Built-ins are allowed for every user by default — admins uncheck to
# deny.
BUILTIN_TOOLS = [
    {"name": "calculator", "description": "Evaluate math expressions.", "targetName": "Strands built-in"},
    {"name": "current_time", "description": "Return the current date/time, optionally in a requested time zone.", "targetName": "Strands built-in"},
    {"name": "http_request", "description": "Perform an HTTP request to a public URL.", "targetName": "Strands built-in"},
    {"name": "think", "description": "Give the model an extended scratchpad before answering.", "targetName": "Strands built-in"},
    {"name": "sleep", "description": "Pause for a short interval (useful for staggering actions).", "targetName": "Strands built-in"},
    {"name": "handoff_to_user", "description": "Escalate a conversation back to a human user.", "targetName": "Strands built-in"},
    {"name": "retrieve", "description": "Retrieve documents from a configured knowledge base index.", "targetName": "Strands built-in"},
    {"name": "file_read", "description": "Read a file from the runtime filesystem (e.g. /mnt/workspace/).", "targetName": "Strands built-in"},
    {"name": "agent_core_memory", "description": "Read and write AgentCore Memory records programmatically.", "targetName": "AgentCore built-in"},
    {"name": "agent_core_browser", "description": "Open a Chrome session in AgentCore Browser for automated web use.", "targetName": "AgentCore built-in"},
    {"name": "agent_core_code_interpreter", "description": "Run Python snippets in an AgentCore Code Interpreter sandbox.", "targetName": "AgentCore built-in"},
]


def list_gateway_tools(_event):
    """GET /tools — list built-in Strands/AgentCore tools + tools discovered
    on every AgentCore Gateway. Each entry is tagged with a `source` field:
    `builtin` for the curated list above, `gateway` for everything scanned.

    Target parsing lives in `gateway_catalog` because the Cedar write path needs
    exactly the same answer. When the two walked the targets separately, adding a
    kind of target the Tool Policy page could list but `build_cedar_statement`
    could not name was a one-line mistake away.

    `catalogError` is reported rather than swallowed: an unreachable gateway and a
    gateway with no targets both produce a short list, and only one of them is
    something an admin should act on.
    """
    tools = [dict(t, source="builtin") for t in BUILTIN_TOOLS]

    if not gateway_catalog.gateways():
        return response(200, {"tools": tools, "catalogError": ""})

    import tool_consumers

    catalog, errors = gateway_catalog.catalog(s3_client)
    for entry in catalog:
        tools.append({
            "name": entry["name"],
            "description": entry["description"],
            "targetName": entry["targetName"],
            "source": "gateway",
            # Which gateway, so the page can say why a tool sits in another region.
            "gatewayLabel": entry["gatewayLabel"],
            "region": entry["region"],
            # Which agents call this tool, so the Tool Policy page can
            # say who breaks when it is revoked. Derived from each
            # agent's declared tool list — see tool_consumers.py.
            "consumers": tool_consumers.consumers_for(entry["name"]),
        })

    return response(200, {"tools": tools, "catalogError": "; ".join(errors)})


def get_user_permissions(event):
    """GET /users/{userId}/permissions — get allowed tools for a user."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})

    resp = table.get_item(Key={"userId": user_id, "skillName": "__permissions__"})
    item = resp.get("Item")
    if not item:
        return response(200, {"userId": user_id, "allowedTools": []})
    allowed = item.get("allowedTools", [])
    if isinstance(allowed, set):
        allowed = list(allowed)
    return response(200, {
        "userId": user_id,
        "allowedTools": allowed,
        "updatedAt": item.get("updatedAt", ""),
    })


def update_user_permissions(event):
    """PUT /users/{userId}/permissions — update allowed tools and sync Cedar policies."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})

    body = json.loads(event.get("body") or "{}")
    new_tools = body.get("allowedTools", [])
    if not isinstance(new_tools, list):
        return response(400, {"error": "allowedTools must be a list"})

    # Read old permissions to determine which tools changed
    old_resp = table.get_item(Key={"userId": user_id, "skillName": "__permissions__"})
    old_item = old_resp.get("Item")
    old_tools = list(old_item.get("allowedTools", [])) if old_item else []

    # Save to DynamoDB
    ts = now_iso()
    if new_tools:
        table.put_item(Item={
            "userId": user_id,
            "skillName": "__permissions__",
            "allowedTools": new_tools,
            "updatedAt": ts,
        })
    else:
        # Remove permissions entry if no tools selected
        table.delete_item(Key={"userId": user_id, "skillName": "__permissions__"})

    # Determine which tools need policy rebuild.
    #
    # The SYMMETRIC difference — only tools whose membership actually changed for
    # this user. It was the union, which is wrong in a way that is invisible until
    # it is not: a tool with no Cedar policy is allow-all (measured — the tools
    # gateway runs in ENFORCE mode with zero policies and serves all six tools),
    # and rebuilding materialises a permit naming only the users who hold the tool
    # in DynamoDB. Just 14 of 40 users have a `__permissions__` row here, so
    # granting ONE new tool to ONE user would have created permits for the six
    # device tools and revoked them from the other 26 — reported as a successful
    # save of an unrelated grant.
    #
    # A tool present in both old and new cannot need a new policy: the policy is
    # derived from every user holding it, and that set did not change.
    affected_tools = set(old_tools) ^ set(new_tools)

    if not GATEWAY_ID:
        return response(200, {
            "message": f"Permissions saved for '{user_id}' (policy sync skipped — no GATEWAY_ID)",
            "allowedTools": new_tools,
        })

    # Rebuild Cedar policies for each affected tool
    errors = []
    for tool_name in affected_tools:
        try:
            rebuild_tool_policy(tool_name)
        except Exception as e:
            logger.error(f"Failed to rebuild policy for tool '{tool_name}': {e}")
            errors.append(f"{tool_name}: {str(e)}")

    if errors:
        return response(200, {
            "message": f"Permissions saved for '{user_id}' with policy sync errors",
            "allowedTools": new_tools,
            "policyErrors": errors,
        })

    return response(200, {
        "message": f"Permissions saved for '{user_id}'",
        "allowedTools": new_tools,
    })


# ---------------------------------------------------------------------------
# A2A Permissions (per-user grants keyed by Registry recordId → [skillId,…])
#
# Stored as a single row in the skills table:
#   userId     = <sub> | "__global__"
#   skillName  = "__a2a_permissions__"
#   a2aGrants  = Map<String, List<String>>
#
# Uses the existing /users/{userId}/permissions resource with ?action=a2a so we
# don't grow the admin Lambda's API Gateway resource policy past 20 KB. The
# route table in handler() dispatches based on the action query param.
# ---------------------------------------------------------------------------

_A2A_PERMS_SK = "__a2a_permissions__"
# The scope key for grants that apply to everyone. Mirrors
# `subagent_policy.GLOBAL_SCOPE`, which is where the merge rule reads it.
GLOBAL_SCOPE = subagent_policy.GLOBAL_SCOPE


def _fetch_a2a_records(status_values: list[str] | None = None) -> list[dict]:
    """Every AGENT record, with the fields grantability and the console both need.

    Returns [{recordId, name, description, status, updatedAt, createdAt, card}, ...].
    `updatedAt` stays as boto3 returned it (a datetime) because
    `registry_ns.grantable` reads it; the console serialises it separately.

    Unfiltered by default, deliberately. The catalog used to be fetched
    APPROVED-only, which made a record that had just left APPROVED indistinguishable
    from one that never existed — and `get_user_a2a_permissions` reports a grant on
    an unknown record as STALE and drops it from the page. So a version bump made
    every grant look deleted, and any save from that page would have written the
    loss back. Fetch everything, then decide with one rule.
    """
    if not REGISTRY_ID:
        return []
    out = []
    token = None
    while True:
        filters = [{"name": "recordType",
                    "values": [registry_ns.RECORD_TYPE_AGENT]}]
        if status_values:
            filters.append({"name": "status", "values": status_values})
        kwargs = {"registryId": REGISTRY_ID, "maxResults": 50, "filters": filters}
        if token:
            kwargs["nextToken"] = token
        resp = registry_control.list_registry_records(**kwargs)
        for r in resp.get("registryRecords", []):
            rid = r.get("recordId", "")
            if not rid:
                continue
            try:
                detail = registry_control.get_registry_record(
                    registryId=REGISTRY_ID, recordId=rid)
                raw = registry_ns.read_agent_card(detail)
                card = json.loads(raw) if raw else {}
            except Exception as e:
                logger.warning("GetRegistryRecord failed for %s: %s", rid, e)
                continue
            out.append({
                "recordId": rid,
                "name": r.get("name", ""),
                "description": r.get("description", ""),
                "status": r.get("status", ""),
                "updatedAt": r.get("updatedAt"),
                "createdAt": r.get("createdAt"),
                "card": card,
            })
        token = resp.get("nextToken")
        if not token:
            return out


def _grantability(records: list[dict]) -> dict[str, tuple[bool, str]]:
    """{recordId: (grantable, reason)} under the shared rule.

    `time.time()` is read once so every record in one sweep is judged against the
    same instant; otherwise a long pagination could put two records on opposite
    sides of the same window boundary.
    """
    now = time.time()
    grace = registry_ns.grant_grace_seconds()
    return {r["recordId"]: registry_ns.grantable(r, now, grace) for r in records}


def _fetch_grantable_a2a_cards() -> list[dict]:
    """Return [{recordId, name, description, skills:[{id,name,description}]}, ...]
    for every A2A record whose skills may be granted right now.

    APPROVED, plus a record still inside its re-approval window — see
    `registry_ns.grantable`. Used by the grant page, by PUT validation and by the
    sweep, so all three agree on what exists.
    """
    records = _fetch_a2a_records()
    verdicts = _grantability(records)
    out = []
    for r in records:
        ok, _reason = verdicts[r["recordId"]]
        if not ok:
            continue
        card = r["card"]
        out.append({
            "recordId": r["recordId"],
            "name": r["name"],
            "description": r["description"],
            "skills": [
                {
                    "id": s.get("id", ""),
                    "name": s.get("name", s.get("id", "")),
                    "description": s.get("description", ""),
                }
                for s in card.get("skills") or []
            ],
        })
    return out



def _resolve_ddb_user_key(user_id: str) -> str:
    """Convert a UI-supplied identifier (Cognito sub, username, or email) into
    the DDB row key the text agent uses at runtime.

    agent.py reads per-user rows keyed by the value of payload["userId"], which
    the chatbot derives as `email or cognito:username or sub` (first present).
    Cognito users created via the default flow always have both an email and a
    sub; the chatbot sends the email. Admin Console, though, works by sub (so
    it can disambiguate users with the same email across pools). This helper
    resolves sub → email so grants written here match what the agent reads.

    ``__global__`` bypasses the lookup. Values that are already emails flow
    through unchanged. Unknown subs fall back to the literal value so the
    write still lands in a valid row even if Cognito is unreachable.
    """
    if not user_id or user_id == "__global__":
        return user_id
    if "@" in user_id:
        return user_id  # already an email
    # Try to resolve by sub via a narrow filter.
    try:
        resp = cognito_client.list_users(
            UserPoolId=COGNITO_USER_POOL_ID,
            Filter=f'sub = "{user_id}"',
            Limit=1,
        )
        for u in resp.get("Users", []):
            email = next(
                (a["Value"] for a in u.get("Attributes", []) if a["Name"] == "email"),
                "",
            )
            if email:
                return email
            return u.get("Username", user_id)
    except Exception as e:
        logger.warning("could not resolve sub %s to email: %s", user_id, e)
    return user_id


def get_user_a2a_permissions(event):
    """GET /users/{userId}/permissions?action=a2a — return a user's A2A grants
    plus the full catalog of APPROVED A2A agents so the UI renders in one round
    trip."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})
    ddb_key = _resolve_ddb_user_key(user_id)

    resp = table.get_item(Key={"userId": ddb_key, "skillName": _A2A_PERMS_SK})
    item = resp.get("Item")
    grants_raw = (item or {}).get("a2aGrants") or {}
    # DynamoDB returns Sets/Lists; coerce to plain lists.
    grants = {}
    for rid, skills in grants_raw.items():
        if isinstance(skills, (set, list, tuple)):
            grants[rid] = sorted(str(s) for s in skills)

    # A Registry failure here used to be indistinguishable from an empty registry:
    # both produced `availableAgents: []` inside a 200, so the console showed "no
    # agents available to grant" whether the catalog was genuinely empty, the
    # registryId was wrong, or the role lacked ListRegistryRecords. That cost a
    # real misdiagnosis — a wrong REGISTRY_ID was read as two other causes, and the
    # only evidence either way was a warning in a Lambda log. The reason travels
    # with the response now.
    catalog_error = ""
    try:
        available = _fetch_grantable_a2a_cards()
    except Exception as e:
        logger.warning("Failed to fetch A2A agent catalog: %s", e)
        available = []
        catalog_error = str(e)

    # Drop grants whose record is gone. A recordId is minted per Registry record,
    # so redeploying an agent in a way that recreates its record leaves the old id
    # behind — pointing at nothing, granting nothing.
    #
    # They cannot merely be ignored. The UI round-trips whatever this returns, and
    # PUT validates every recordId against the grantable catalog, so ONE dead entry
    # makes every future save fail with a 400 that names a record the admin has
    # never heard of and cannot remove from the page. Filtered only when the
    # catalog actually loaded: with `available` empty from a Registry error, every
    # grant would look dead and one bad fetch would appear to revoke everything.
    #
    # A record merely waiting to be re-approved is NOT stale — the catalog includes
    # it for the length of its window (see `_fetch_grantable_a2a_cards`). Before it
    # did, a version bump made every grant on that agent look deleted here, and a
    # save from that page would have written the loss back.
    stale: list[str] = []
    if available:
        known = {c["recordId"] for c in available}
        stale = sorted(set(grants) - known)
        for rid in stale:
            grants.pop(rid, None)
        if stale:
            logger.info("ignoring %d grant(s) for records that no longer exist: %s",
                        len(stale), stale)

    return response(200, {
        "userId": user_id,
        "a2aGrants": grants,
        "availableAgents": available,
        # Surfaced rather than hidden: the row still holds them, and an admin
        # looking at why a user lost access deserves to see that the cause was a
        # record being replaced.
        "staleGrants": stale,
        # Empty when the catalog loaded. Non-empty means `availableAgents` is empty
        # because the lookup FAILED, not because there is nothing to grant — the
        # console renders it instead of the "no agents" empty state.
        "catalogError": catalog_error,
        "updatedAt": (item or {}).get("updatedAt", ""),
    })


def update_user_a2a_permissions(event):
    """PUT /users/{userId}/permissions?action=a2a — replace a user's A2A grants.

    Body: {"a2aGrants": {recordId: [skillId,...], ...}}. Empty map deletes the
    row. Validates every recordId is APPROVED + A2A and every skillId is in the
    card. Returns 400 on validation error, 200 on success."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})
    ddb_key = _resolve_ddb_user_key(user_id)

    body = json.loads(event.get("body") or "{}")
    grants_in = body.get("a2aGrants", {})
    if not isinstance(grants_in, dict):
        return response(400, {"error": "a2aGrants must be an object"})
    # Normalise values to lists of strings.
    grants: dict = {}
    for rid, skills in grants_in.items():
        if not isinstance(rid, str) or not rid:
            return response(400, {"error": f"invalid recordId: {rid!r}"})
        if not isinstance(skills, list):
            return response(400, {"error": f"skills for {rid} must be a list"})
        clean = [str(s) for s in skills if str(s).strip()]
        if clean:
            grants[rid] = sorted(set(clean))

    # Validate each recordId against the approved catalog.
    catalog = {c["recordId"]: c for c in _fetch_grantable_a2a_cards()}
    errors = []
    for rid, skills in grants.items():
        card = catalog.get(rid)
        if not card:
            errors.append(f"recordId {rid} is not an approved A2A record")
            continue
        valid_skill_ids = {s["id"] for s in card["skills"]}
        bad = [s for s in skills if s not in valid_skill_ids]
        if bad:
            errors.append(
                f"recordId {rid} does not offer skill(s) {bad}; valid: {sorted(valid_skill_ids)}"
            )
    if errors:
        return response(400, {"error": "validation failed", "details": errors})

    ts = now_iso()
    if grants:
        table.put_item(Item={
            "userId": ddb_key,
            "skillName": _A2A_PERMS_SK,
            "a2aGrants": grants,
            "updatedAt": ts,
            "updatedBy": _caller_id(event),
        })
    else:
        try:
            table.delete_item(Key={"userId": ddb_key, "skillName": _A2A_PERMS_SK})
        except Exception:
            pass

    # Materialise the new intent into Cognito group membership. This is what the
    # sub-agents actually authorize on, so a save that skipped it would look
    # successful and change nothing the platform can see.
    sync = _materialise_a2a_grants(user_id, ddb_key, catalog)

    return response(200, {
        "userId": user_id,
        "a2aGrants": grants,
        "updatedAt": ts,
        "groupSync": sync,
    })


def _record_card_names(catalog: dict) -> dict[str, str]:
    """recordId -> AgentCard name.

    Grants are stored by recordId, which survives a rename; group names are keyed on
    the card name, which is what the sub-agent knows itself as (it reads it from its
    own card.json). This is the join between the two.
    """
    return {rid: (card.get("name") or "") for rid, card in catalog.items()}


def _affected_users(user_id: str) -> list[tuple[str, str]]:
    """(cognito_username, intent_key) for every user a change to `user_id` affects.

    A per-user change affects one user. A change to `__global__` affects **every**
    user, because global grants are inherited by anyone without an override — which
    is also why narrowing the global default can sign everyone out.

    **Two identifiers, and they are not interchangeable.** This pool has
    `UsernameAttributes: ['email']`, so a user has a generated UUID `Username` AND
    an email, and Cognito's admin APIs accept either. But grant INTENT is stored
    under the email (`_resolve_ddb_user_key` normalises to it, because that is the
    key the agent reads per-user rows by at runtime). So the two have to be carried
    separately: the UUID for the Cognito calls, the email for the DynamoDB read.

    Returning one string was a silent authorization bug. `list_users` yields the
    UUID `Username`, so a change to `__global__` read each user's override under the
    UUID, found nothing, fell back to global and re-granted everything — discarding
    every per-user narrowing while reporting a successful save. Observed live: an
    admin removed `advisory_review` from one user, the page saved it, and a later
    global save silently put the group back. The reconcile could not see it either,
    because it computed `wanted` the same wrong way — so the safety net shared the
    blind spot with the thing it was meant to catch.
    """
    if user_id != "__global__":
        # The caller's identifier may be an email or a sub; Cognito accepts both
        # here, while the intent row is keyed the way the write path keyed it.
        return [(user_id, _resolve_ddb_user_key(user_id))]
    users: list[tuple[str, str]] = []
    params = {"UserPoolId": COGNITO_USER_POOL_ID, "Limit": 60}
    while True:
        resp = cognito_client.list_users(**params)
        for user in resp.get("Users", []):
            username = user.get("Username")
            if not username:
                continue
            email = next(
                (a["Value"] for a in user.get("Attributes", [])
                 if a["Name"] == "email"), "")
            # Falls back to the username when a user somehow has no email, so an
            # override written under that literal value is still honoured.
            users.append((username, email or username))
        token = resp.get("PaginationToken")
        if not token:
            return users
        params["PaginationToken"] = token


def _materialise_a2a_grants(user_id: str, ddb_key: str, catalog: dict,
                            fan_out_global: bool = False) -> dict:
    """Push grant intent into Cognito groups, signing out anyone who lost access.

    Never raises: the DDB write has already happened and returning 500 here would
    tell the admin their change failed when the intent was in fact saved. The result
    is reported so the UI can show that the two stores disagree, which is the whole
    reason the page carries a sync status column.
    """
    if not COGNITO_USER_POOL_ID:
        return {"ok": False, "error": "COGNITO_USER_POOL_ID not configured"}

    names = _record_card_names(catalog)
    results, signed_out, errors = [], [], []

    def _one(username: str, intent_key: str) -> dict:
        # `intent_key` (the email) reads the override; `username` (the UUID) is what
        # Cognito is called with. Using one for both re-grants what an admin just
        # revoked — see `_affected_users`.
        #
        # PER-USER intent only. Global grants are no longer materialised: they are
        # injected into the `cognito:groups` claim at token issue by
        # cdk/lambda/pre-token, because one membership per user for a value that is
        # the same for everybody hits a 25 RPS non-adjustable Cognito quota, a
        # 100-groups-per-user cap and one billable MAU per write. Merging global in
        # here would write back exactly what that change removed.
        per_user = ({} if intent_key == "__global__"
                    else subagent_policy.read_intent(table, intent_key))
        wanted = subagent_policy.wanted_groups(per_user, names)
        res = subagent_policy.materialise_user(
            cognito_client, COGNITO_USER_POOL_ID, username, wanted)
        # A removal only takes effect on the next token, so close the window rather
        # than leaving a revoke that does nothing for an hour.
        res["signedOut"] = bool(res["narrowed"]) and subagent_policy.force_token_refresh(
            cognito_client, COGNITO_USER_POOL_ID, username)
        return res

    try:
        global_intent = subagent_policy.read_intent(table, "__global__")

        # A SAVE to `__global__` now touches NO user's memberships. Global grants
        # reach a user through the `cognito:groups` claim the pre-token trigger
        # builds, and per-user materialisation reads per-user intent only — so there
        # is nothing per-user to rewrite. The groups themselves are still ensured
        # below, because an authorizer matches a group NAME.
        #
        # This is the fan-out that used to make a global save iterate every user,
        # sign out everyone it narrowed, and (measured, in this function's own
        # history) exceed API Gateway's 29s ceiling at 39 users.
        #
        # `fan_out_global` re-enables the iteration for the REPAIR path only, which
        # is what removes memberships the old behaviour left behind: with per-user
        # intent as the target, a leftover global membership is `extra` and gets
        # dropped. An admin action, not a migration script — and deliberately not
        # something a routine save does.
        affected = ([] if user_id == GLOBAL_SCOPE and not fan_out_global
                    else _affected_users(user_id))

        # Create every group ONCE before touching memberships. Groups are shared, so
        # doing this per user was pure waste and it throttled: measured on 39 users
        # and 17 groups, Cognito rejected most CreateGroup calls with
        # TooManyRequestsException and only 6 users ended up with any membership.
        #
        # Every group the CLAIM could name has to exist too, not just the ones a
        # membership will reference: a sub-agent authorizer matches a group NAME, and
        # `cognito:groups` carrying a name no group backs is a normal state now. So
        # the union covers global intent as well, even though global is never
        # materialised.
        wanted_any: set = subagent_policy.wanted_groups(global_intent, names)
        for _username, intent_key in affected:
            per_user = ({} if intent_key == "__global__"
                        else subagent_policy.read_intent(table, intent_key))
            wanted_any |= subagent_policy.wanted_groups(per_user, names)
        errors.extend(subagent_policy.ensure_groups(
            cognito_client, COGNITO_USER_POOL_ID, wanted_any))

        # Concurrent, because a change to `__global__` touches EVERY user and each
        # one costs a list-groups plus an add or remove per changed group. Measured
        # serially on 39 users and three groups: past API Gateway's 29s ceiling, so
        # the caller got a 504 while the Lambda kept going and finished. The write
        # was correct and the admin was told it failed, which is the worst pairing.
        #
        # Eight workers, not more: these are Cognito admin APIs on one user pool, and
        # the point is to fit the request budget, not to saturate the service.
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            pending = {pool.submit(_one, u, k): u for u, k in affected}
            for future in futures.as_completed(pending):
                username = pending[future]
                try:
                    res = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("group sync failed for %s: %s", username, exc)
                    errors.append(f"{username}: {exc}")
                    continue
                results.append(res)
                if res.get("signedOut"):
                    signed_out.append(username)
    except Exception as exc:  # noqa: BLE001
        logger.exception("group materialisation failed")
        return {"ok": False, "error": str(exc)}

    return {
        "ok": not errors,
        "users": results,
        "signedOut": signed_out,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Authorizer conformance: is a registered agent actually callable, and by whom?
#
# Registering an APPROVED card is enough to be DISCOVERED — this Lambda lists it,
# the orchestrator registers tools for it, the delegation prompt names it. It is not
# enough to be CALLABLE: that is decided by the sub-agent's own Runtime authorizer,
# which lives with whoever deployed that runtime. Nothing connected the two, so a
# record could read `Reachable / approved` on the Integration Registry page while
# nobody could call the agent — or while everybody could.
#
# The rule itself is `shared/a2a_conformance.py`, pure and tested. What lives here is
# the part that needs AWS: getting from a card's URL to the runtime behind it.
# ---------------------------------------------------------------------------

# The card-URL -> runtime hop moved to `a2a_runtimes.py`: the conformance check, the
# gateway-target reconcile and the ops dashboard all need it, and three copies of a
# URL-encoding rule is three chances for them to disagree about which runtime a record
# refers to.
def _runtime_id_from_url(url: str) -> str:
    return a2a_runtimes.runtime_id_from_url(url)


def _resolve_runtime(url: str) -> tuple[str, str]:
    return a2a_runtimes.resolve(url, agentcore_control)


def check_a2a_conformance(_event=None):
    """GET /registry/records?action=a2a-conformance — is each registered agent callable?

    Read-only. Returns one row per AGENT record with the findings from
    `a2a_conformance.check`, so the console can mark a record whose authorizer does
    not match the card it published.

    Separate from `a2a-list` rather than folded into it: this costs a
    GetAgentRuntime per record plus a gateway-target lookup, and the inventory page
    should not get slower for a check that can be rendered as it arrives.
    """
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    discovery_url = (f"https://cognito-idp.{REGION}.amazonaws.com/"
                     f"{COGNITO_USER_POOL_ID}/.well-known/openid-configuration")
    app_client = COGNITO_APP_CLIENT_ID

    try:
        records = _fetch_a2a_records()
    except Exception as exc:  # noqa: BLE001
        return response(502, {"error": f"cannot read the registry: {exc}"})

    rows = []
    for record in records:
        card = record.get("card") or {}
        url = card.get("url") or ""
        runtime_id, via = _resolve_runtime(url)
        authorizer = None
        if runtime_id:
            try:
                authorizer = agentcore_control.get_agent_runtime(
                    agentRuntimeId=runtime_id).get("authorizerConfiguration") or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read runtime %s: %s", runtime_id, exc)
                via = f"{via}; GetAgentRuntime failed: {exc}"

        findings = a2a_conformance.check(card, authorizer, discovery_url, app_client)
        rows.append({
            "recordId": record["recordId"],
            "name": record["name"],
            "status": record["status"],
            "runtimeId": runtime_id,
            "resolvedVia": via,
            "conformant": not findings,
            "severity": a2a_conformance.worst_severity(findings),
            "findings": findings,
        })

    return response(200, {
        "records": rows,
        # Echoed so a row that says "wrong pool" can be read against what this
        # deployment actually expects, without going to look it up.
        "expected": {"discoveryUrl": discovery_url, "appClientId": app_client},
    })


def _discovery_url() -> str:
    return (f"https://cognito-idp.{REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/openid-configuration")


A2A_GATEWAY_NAME = "smarthome-a2a-gw"


def _a2a_gateway_url(records: list[dict] | None = None) -> str:
    """This deployment's A2A gateway base URL, or "" if it does not exist.

    NOT read from an environment variable, deliberately. This Lambda's script-patched
    env has been silently wiped by `cdk deploy` more than once (CloudFormation rewrites
    the whole Environment map whenever the DECLARED map changes), and the failure mode
    for a published manifest is the worst kind: we would hand a third party a blank or
    stale gateway URL and they would configure against it.

    Two sources, in this order:

      1. A record whose card already routes through the gateway NAMES it exactly, and
         costs nothing — the records are already in hand.
      2. Otherwise, look it up by name. Needed because (1) is circular: on a deployment
         where nothing is behind the gateway yet, deriving only from the records means
         the reconcile can never front the first agent, and the manifest can never tell
         anyone the gateway exists.
    """
    for record in records or []:
        base = a2a_runtimes.gateway_base_url((record.get("card") or {}).get("url") or "")
        if base:
            return base
    try:
        for page in agentcore_control.get_paginator("list_gateways").paginate():
            for gw in page.get("items", []):
                if gw.get("name") != A2A_GATEWAY_NAME:
                    continue
                url = agentcore_control.get_gateway(
                    gatewayIdentifier=gw["gatewayId"]).get("gatewayUrl") or ""
                # GetGateway returns the MCP endpoint path on some shapes; the A2A
                # passthrough targets hang off the host root, so keep the origin only.
                return a2a_runtimes.gateway_base_url(url) or url.rstrip("/")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not look up the A2A gateway by name: %s", exc)
    return ""


def get_a2a_manifest(_event=None):
    """GET /registry/records?action=a2a-manifest — the contract for an agent team.

    Read-only, and the reason it exists at all: everything a third party needs to know
    about this deployment used to travel by hand — a pool id pasted into a message, an
    app client id copied off a wiki. A mistyped pool id is the silent 401 that has the
    orchestrator still offering the tool while the model apologises.

    The document itself is GENERATED from the modules that enforce each rule (see
    `shared/a2a_manifest.py`), so "what we publish" cannot drift from "what we check".
    """
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})
    if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
        # Refuse rather than publish a manifest with a hole in it. A third party
        # configuring an empty audience gets a 401 with no explanation, which is
        # exactly what this route exists to prevent.
        missing = [n for n, v in (("COGNITO_USER_POOL_ID", COGNITO_USER_POOL_ID),
                                  ("COGNITO_APP_CLIENT_ID", COGNITO_APP_CLIENT_ID))
                   if not v]
        return response(500, {
            "error": f"cannot publish a manifest without {', '.join(missing)}",
            "hint": "the admin Lambda's env was probably reset by a cdk deploy; "
                    "re-run scripts/setup-agentcore.py and check-registry-wiring.py",
        })

    # Best-effort: the gateway section is optional, so a registry read failure
    # degrades the manifest rather than failing the route.
    try:
        records = _fetch_a2a_records()
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest: could not read the registry for the gateway "
                       "URL: %s", exc)
        records = []

    manifest = a2a_manifest.build(
        region=REGION,
        registry_id=REGISTRY_ID,
        user_pool_id=COGNITO_USER_POOL_ID,
        app_client_id=COGNITO_APP_CLIENT_ID,
        discovery_url=_discovery_url(),
        gateway_url=_a2a_gateway_url(records),
        grant_grace_seconds=registry_ns.grant_grace_seconds(),
    )
    return response(200, manifest)


# ---------------------------------------------------------------------------
# Gateway-target reconcile: an APPROVED record should not need a human to be fronted
# ---------------------------------------------------------------------------

def reconcile_a2a_gateway_targets(event=None):
    """Converge the A2A gateway's passthrough targets on the grantable records.

    GET (or `?apply=false`) reports; POST with `?apply=true` creates. Removing targets
    is deliberately NOT automated — see below.

    Why this is here rather than only in `scripts/setup-a2a-gateway.py`: that script
    reads `a2a-agent-registry/deployed-state.json`, a file only the platform team has.
    A third-party team that registers an approved agent then had to ask someone to add
    a gateway target by hand, which is precisely the cross-team ticket this work exists
    to delete. Driven from the Registry, "approved" is enough.

    Matched by RUNTIME, named by CARD
    ---------------------------------
    An existing target is matched to a record by the runtime its endpoint resolves to,
    never by its name. The eight built-in targets are named after internal short names
    (`air-quality`, `device-control`) while their cards carry long names
    (`air-quality-agent`) — matching on name would decide all eight were missing and
    create eight duplicates pointing at the same runtimes.

    Deletion is reported, not performed
    -----------------------------------
    A target whose record is gone is listed as `orphaned` and left alone. Deleting it
    would break any card still pointing at it, and the sweep already closes the
    authorization hole by revoking that agent's groups — so an orphaned target is dead
    weight, not an open door. Dead weight does not justify an automated delete of
    something another team's card may reference.
    """
    qs = (event or {}).get("queryStringParameters") or {}
    apply = str(qs.get("apply", "")).lower() in ("1", "true", "yes")

    try:
        records = _fetch_a2a_records()
    except Exception as exc:  # noqa: BLE001
        # Same reasoning as the sweep: an unreadable registry must not be read as
        # "nothing should be fronted".
        return response(503, {"error": f"registry unavailable: {exc}",
                              "reconciled": False})
    if not records:
        return response(503, {"error": "registry reported no AGENT records",
                              "reconciled": False})

    gateway_url = _a2a_gateway_url(records)
    gateway_id = (a2a_runtimes.gateway_target_from_url(gateway_url + "/x")[0]
                  if gateway_url else "")
    if not gateway_id:
        return response(200, {
            "reconciled": False,
            "reason": "no A2A gateway is in use by any record; nothing to converge",
            "targets": [], "missing": [], "orphaned": [],
        })

    try:
        targets = a2a_runtimes.list_targets(agentcore_control, gateway_id)
    except Exception as exc:  # noqa: BLE001
        return response(503, {"error": f"could not list gateway targets: {exc}",
                              "reconciled": False})
    by_runtime = {t["runtimeId"]: t for t in targets if t["runtimeId"]}

    verdicts = _grantability(records)
    missing, fronted, created, errors = [], [], [], []
    # Every grantable record's RESOLVED runtime, gateway hop included. Built before the
    # loop because the orphan check at the end needs it for all of them, and resolving
    # only the ones that need a target got that wrong in the obvious way: our own cards
    # point AT the gateway, so a raw URL parse yields no runtime id, `live_runtimes`
    # collapsed to {""} and all eight live targets were reported orphaned.
    live_runtimes: set[str] = set()
    for record in records:
        if not verdicts[record["recordId"]][0]:
            continue
        rid, _via = _resolve_runtime((record.get("card") or {}).get("url") or "")
        if rid:
            live_runtimes.add(rid)

    for record in records:
        ok, _reason = verdicts[record["recordId"]]
        if not ok:
            continue
        card = record.get("card") or {}
        url = card.get("url") or ""
        card_name = card.get("name") or ""
        # A card already pointing at the gateway needs no target created — either it
        # has one, or it is broken in a way `check_a2a_conformance` reports properly.
        if a2a_runtimes.GATEWAY_HOST_MARKER in url:
            _gw, target_name = a2a_runtimes.gateway_target_from_url(url)
            fronted.append({"recordId": record["recordId"], "name": card_name,
                            "targetName": target_name,
                            "targetUrl": url,
                            "via": "card already points at the gateway"})
            continue
        runtime_id = a2a_runtimes.runtime_id_from_url(url)
        if not runtime_id:
            errors.append(f"{card_name or record['recordId']}: card url is not a "
                          "runtime invocations URL, so no target can be created")
            continue
        existing = by_runtime.get(runtime_id)
        if existing:
            fronted.append({
                "recordId": record["recordId"], "name": card_name,
                "targetName": existing["name"],
                "targetUrl": f"{gateway_url}/{existing['name']}",
                "via": "matched by runtime id",
            })
            continue
        entry = {
            "recordId": record["recordId"], "name": card_name,
            "runtimeId": runtime_id,
            "targetName": card_name,
            "targetUrl": f"{gateway_url}/{card_name}",
            "endpoint": url,
        }
        missing.append(entry)
        if not apply:
            continue
        try:
            resp = agentcore_control.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=card_name,
                description=f"A2A passthrough to {card_name} (Registry "
                            f"{record['recordId']})",
                targetConfiguration={"http": {"passthrough": {
                    "endpoint": url, "protocolType": "A2A"}}},
                # JWT_PASSTHROUGH so the END USER's idToken reaches the sub-agent
                # unchanged — that token IS the authorization. A GATEWAY_IAM_ROLE
                # credential would replace the caller's identity with the gateway's
                # and the container would see no user at all.
                credentialProviderConfigurations=[
                    {"credentialProviderType": "JWT_PASSTHROUGH"}],
            )
            entry["targetId"] = resp["targetId"]
            created.append(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not create gateway target %s: %s", card_name, exc)
            errors.append(f"{card_name}: {exc}")

    orphaned = [t["name"] for t in targets
                if t["runtimeId"] and t["runtimeId"] not in live_runtimes]

    logger.info("A2A gateway reconcile on %s: %d target(s), %d fronted, %d missing, "
                "%d created, %d orphaned", gateway_id, len(targets), len(fronted),
                len(missing), len(created), len(orphaned))
    return response(200, {
        "reconciled": True,
        "applied": apply,
        "gatewayId": gateway_id,
        "gatewayUrl": gateway_url,
        "fronted": fronted,
        "missing": missing,
        "created": created,
        # Left in place on purpose — see the docstring.
        "orphaned": orphaned,
        "errors": errors,
    })


def sweep_a2a_revocations(_event=None):
    """Revoke `a2a-` group memberships whose record may no longer be granted.

    Driven two ways, deliberately. An EventBridge schedule calls it as the safety
    net, because the approval flow itself happens in the AWS Console — the most
    common change of all is one this Lambda never sees. Our own write paths call it
    inline so an admin's action takes effect while they are still looking at it.

    Idempotent and side-effect-free when nothing is wrong: with every record
    approved, this is one ListGroups and no writes.

    What it does NOT do is touch grant INTENT. `__a2a_permissions__` is left exactly
    as it was, so re-approving a record and letting the next sweep or save run puts
    the groups back. A revocation here is an outage, never a data loss — which is
    what makes it safe to run on a timer.
    """
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})

    try:
        records = _fetch_a2a_records()
    except Exception as exc:  # noqa: BLE001
        # A Registry failure must not be read as "nothing is grantable". That
        # inference would revoke every A2A group in the pool on one bad call, and
        # the same mistake in `get_user_a2a_permissions` once looked like a mass
        # revocation to an admin. Refuse to act instead.
        logger.warning("A2A sweep skipped: could not read the registry: %s", exc)
        return response(503, {"error": f"registry unavailable: {exc}",
                              "swept": False})
    if not records:
        # Same reasoning: an empty answer with no error is indistinguishable from a
        # registry that has not finished being provisioned.
        logger.warning("A2A sweep skipped: the registry reported zero AGENT records")
        return response(503, {"error": "registry reported no AGENT records",
                              "swept": False})

    verdicts = _grantability(records)
    grantable_agents: set[str] = set()
    for r in records:
        ok, _reason = verdicts[r["recordId"]]
        if not ok:
            continue
        # The CARD name, not the record name. Group names are keyed on what the
        # sub-agent knows itself as, which is what its authorizer was configured
        # with; the record name is free to differ and a rename must not orphan a
        # membership. Same join as `_record_card_names`.
        card_name = (r["card"] or {}).get("name") or ""
        if card_name:
            grantable_agents.add(card_name)
        else:
            logger.warning("record %s is grantable but its card has no name; "
                           "no group can be derived for it", r["recordId"])

    try:
        present = subagent_policy.all_a2a_groups(cognito_client,
                                                COGNITO_USER_POOL_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("A2A sweep could not list groups: %s", exc)
        return response(503, {"error": f"could not list groups: {exc}",
                              "swept": False})

    doomed = subagent_policy.groups_to_revoke(present, grantable_agents)
    # Logged on every pass, including the clean one. This runs on a timer and its
    # steady state is "one ListGroups, no writes" — which is indistinguishable in
    # the logs from "did not run" unless it says so. An enforcement job that cannot
    # be shown to have looked is not an enforcement job.
    logger.info("A2A sweep: %d group(s) present, %d grantable agent(s), "
                "%d group(s) to revoke", len(present), len(grantable_agents),
                len(doomed))
    if not doomed:
        return response(200, {
            "swept": True,
            "groupsChecked": len(present),
            "revokedGroups": [],
            "affectedUsers": {},
            "signedOut": [],
            "errors": [],
        })

    logger.info("A2A sweep revoking %d group(s): %s", len(doomed), doomed)
    result = subagent_policy.revoke_groups(
        cognito_client, COGNITO_USER_POOL_ID, doomed)
    result["swept"] = True
    result["groupsChecked"] = len(present)
    # Why each revoked group went, so the log answers "who took my access away".
    result["reasons"] = {
        r["recordId"]: verdicts[r["recordId"]][1]
        for r in records if not verdicts[r["recordId"]][0]
    }
    return response(200, result)


def reconcile_a2a_grants(event):
    """GET /users/{userId}/permissions?action=a2a-reconcile — intent vs reality.

    Read-only. Compares what DDB says each user should hold against the `a2a-`
    groups they actually hold, for every user when called on `__global__`. A one-way
    materialisation drifts, and a sync with no way to see the drift is not a sync.
    """
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", "")) or "__global__"

    try:
        catalog = {c["recordId"]: c for c in _fetch_grantable_a2a_cards()}
    except Exception as exc:  # noqa: BLE001
        # Without the catalog there are no card names, so every group would look
        # "extra" and the reconcile would advise stripping every grant.
        return response(200, {"ok": False, "catalogError": str(exc), "users": []})

    names = _record_card_names(catalog)
    global_intent = subagent_policy.read_intent(table, "__global__")
    # What the CLAIM adds on top of memberships, reported so an admin can tell the
    # two apart. A globally granted group appears in a user's token and in no
    # membership, which is correct and is NOT drift — but `AdminListGroupsForUser`
    # cannot show it, so a page that only rendered memberships would look like every
    # user had lost their global grants.
    claim_injected = sorted(subagent_policy.wanted_groups(global_intent, names))
    out = []
    # Same two identifiers as the write path, for the same reason. When this read
    # the override under the Cognito UUID it reported every user as in sync while
    # their saved intent said otherwise — a reconcile that cannot see the drift the
    # sync creates is worse than no reconcile, because it certifies the drift.
    for username, intent_key in _affected_users(user_id):
        # PER-USER intent only, matching what materialisation now writes. Comparing
        # against the merged set would report every user as missing every global
        # group forever, and the repair would then write back the million
        # memberships the pre-token trigger exists to avoid.
        per_user = ({} if intent_key == "__global__"
                    else subagent_policy.read_intent(table, intent_key))
        wanted = subagent_policy.wanted_groups(per_user, names)
        try:
            out.append(subagent_policy.diff_user(
                cognito_client, COGNITO_USER_POOL_ID, username, wanted))
        except Exception as exc:  # noqa: BLE001
            out.append({"username": username, "error": str(exc), "inSync": False})

    return response(200, {
        "ok": True,
        "users": out,
        "outOfSync": [u["username"] for u in out if not u.get("inSync")],
        # Held by every user through the token, backed by no membership.
        "claimInjectedGroups": claim_injected,
    })


def repair_a2a_grants(event):
    """PUT /users/{userId}/permissions?action=a2a-reconcile — apply the reconcile.

    Materialises intent for every affected user, which is the same code path a save
    takes. Separate from the read so looking is never a mutation.
    """
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", "")) or "__global__"
    try:
        catalog = {c["recordId"]: c for c in _fetch_grantable_a2a_cards()}
    except Exception as exc:  # noqa: BLE001
        return response(502, {"error": f"cannot read the A2A catalog: {exc}"})
    # `fan_out_global=True`: a repair on `__global__` is exactly the "walk every
    # user and make reality match per-user intent" pass, which is also the one-time
    # cleanup of global memberships written before the pre-token trigger existed.
    # At a million users this is an hours-long job against a 25 RPS quota — see the
    # scaling note in shared/subagent_policy.py — so it stays an explicit action.
    return response(200, _materialise_a2a_grants(
        user_id, _resolve_ddb_user_key(user_id), catalog, fan_out_global=True))


def list_a2a_grants_for_record(event):
    """GET /registry/records?action=a2a-grants&recordId=X — reverse lookup.

    Returns every user who has this recordId in their __a2a_permissions__ row,
    plus the skills they were granted, for the Integration Registry read view.
    """
    qs = event.get("queryStringParameters") or {}
    record_id = qs.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId query param is required"})

    from boto3.dynamodb.conditions import Attr
    grants = []
    scan_kwargs = {
        "FilterExpression": Attr("skillName").eq(_A2A_PERMS_SK),
    }
    try:
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                a2a_grants = item.get("a2aGrants") or {}
                if record_id in a2a_grants:
                    skills = a2a_grants[record_id]
                    if isinstance(skills, (set, list, tuple)):
                        skills = sorted(str(s) for s in skills)
                    else:
                        skills = []
                    grants.append({
                        "userId": item.get("userId", ""),
                        "skillIds": skills,
                        "updatedAt": item.get("updatedAt", ""),
                    })
            token = resp.get("LastEvaluatedKey")
            if not token:
                break
            scan_kwargs["ExclusiveStartKey"] = token
    except Exception as e:
        return response(500, {"error": f"scan failed: {str(e)}"})

    return response(200, {"recordId": record_id, "grants": grants})


# ---------------------------------------------------------------------------
# Memory Management
# ---------------------------------------------------------------------------

def list_memory_actors(_event):
    """GET /memories — list all actors in AgentCore Memory."""
    if not MEMORY_ID:
        return response(500, {"error": "MEMORY_ID not configured"})
    try:
        actors = []
        params = {"memoryId": MEMORY_ID, "maxResults": 100}
        while True:
            resp = agentcore_client.list_actors(**params)
            actors.extend(a["actorId"] for a in resp.get("actorSummaries", []))
            token = resp.get("nextToken")
            if not token:
                break
            params["nextToken"] = token
        return response(200, {"actors": actors})
    except Exception as e:
        return response(500, {"error": f"Failed to list memory actors: {str(e)}"})


def get_memory_records(event):
    """GET /memories/{actorId} — get long-term memory records for a user."""
    if not MEMORY_ID:
        return response(500, {"error": "MEMORY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    actor_id = unquote(path_params.get("actorId", ""))
    if not actor_id:
        return response(400, {"error": "actorId is required"})

    # facts / preferences are user-scoped; episodes are STRATEGY-scoped, so the
    # third namespace cannot be built from the actor alone and is skipped when the
    # strategy id is unknown rather than guessed (a wrong namespace lists nothing,
    # forever, and looks the same as a strategy with no records yet).
    namespaces = [("facts", f"/users/{actor_id}/facts"),
                  ("preferences", f"/users/{actor_id}/preferences")]
    if EPISODIC_STRATEGY_ID:
        namespaces.append(
            ("episodes", f"/strategy/{EPISODIC_STRATEGY_ID}/actor/{actor_id}/"))

    records = []
    for ns_type, namespace in namespaces:
        try:
            params = {"memoryId": MEMORY_ID, "namespace": namespace, "maxResults": 50}
            while True:
                resp = agentcore_client.list_memory_records(**params)
                for r in resp.get("memoryRecordSummaries", []):
                    content = r.get("content", {})
                    records.append({
                        "id": r.get("memoryRecordId", ""),
                        "type": ns_type,
                        "text": content.get("text", ""),
                        "strategy": r.get("memoryStrategyId", ""),
                        "createdAt": r.get("createdAt", "").isoformat() if hasattr(r.get("createdAt", ""), "isoformat") else str(r.get("createdAt", "")),
                    })
                token = resp.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        except Exception as e:
            logger.warning(f"Failed to list memory records for {namespace}: {e}")

    records.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return response(200, {"actorId": actor_id, "records": records})


# ---------------------------------------------------------------------------
# Cedar Policy Helpers
# ---------------------------------------------------------------------------

def _policy_engine_sk(region):
    """The DynamoDB sort key holding one region's policy engine.

    The original single-region key is kept verbatim for the home region so an
    existing deployment keeps using the engine it already has; a second region
    gets its own row. Sharing one row would have made the second region's
    provisioning overwrite the first's engine id, and the symptom is every tool
    policy in us-west-2 quietly targeting an engine in another region.
    """
    if region == REGION:
        return "__policy_engine__"
    return f"__policy_engine_{region}__"


def ensure_policy_engine(region=None):
    """Get or create the policy engine for one region.

    Returns (policyEngineId, policyEngineArn). A policy engine is a regional
    resource, so the web-search gateway in us-east-1 cannot share the engine that
    governs the tools gateway in us-west-2 — hence the parameter.
    """
    region = region or REGION
    control = gateway_catalog.control(region)
    sk = _policy_engine_sk(region)

    # Check DynamoDB first
    resp = table.get_item(Key={"userId": "__system__", "skillName": sk})
    item = resp.get("Item")
    if item and item.get("policyEngineId"):
        return item["policyEngineId"], item.get("policyEngineArn", "")

    # Create policy engine
    try:
        create_resp = control.create_policy_engine(
            name="SmartHomeUserPermissions",
            description="Per-user tool access control for SmartHome Gateway",
        )
        engine_id = create_resp["policyEngineId"]
        engine_arn = create_resp.get("policyEngineArn", "")
    except control.exceptions.ConflictException:
        # Already exists — list and find it
        list_resp = control.list_policy_engines()
        for eng in list_resp.get("policyEngines", []):
            if eng.get("name") == "SmartHomeUserPermissions":
                engine_id = eng["policyEngineId"]
                engine_arn = eng.get("policyEngineArn", "")
                break
        else:
            raise Exception("Policy engine conflict but could not find existing engine")

    # Poll until ACTIVE (max 30s)
    for _ in range(15):
        try:
            get_resp = control.get_policy_engine(policyEngineId=engine_id)
            if get_resp.get("status") == "ACTIVE":
                engine_arn = get_resp.get("policyEngineArn", engine_arn)
                break
        except Exception:
            pass
        time.sleep(2)

    # Store in DynamoDB
    table.put_item(Item={
        "userId": "__system__",
        "skillName": sk,
        "policyEngineId": engine_id,
        "policyEngineArn": engine_arn,
        "region": region,
        "updatedAt": now_iso(),
    })

    return engine_id, engine_arn


def ensure_gateway_policy_engine(policy_engine_arn, gateway_id=None, region=None):
    """Associate the policy engine with the gateway if not already.

    Defaults to the tools gateway in the home region so existing callers keep
    their behaviour; the web-search gateway passes its own id and region.
    """
    gateway_id = gateway_id or GATEWAY_ID
    region = region or REGION
    control = gateway_catalog.control(region)
    gw = control.get_gateway(gatewayIdentifier=gateway_id)

    existing_config = gw.get("policyEngineConfiguration")
    if existing_config and existing_config.get("arn") == policy_engine_arn:
        return  # Already associated

    # The gateway role needs GetPolicyEngine permission to use the policy engine
    gw_role_arn = gw.get("roleArn", "")
    if gw_role_arn:
        gw_role_name = gw_role_arn.split("/")[-1]
        # IAM is global, so this is deliberately NOT the gateway's region — the two
        # gateways share one service role and granting it twice in two regions
        # would be the same PutRolePolicy call made twice.
        iam_client = boto3.client("iam", region_name=REGION)
        try:
            iam_client.put_role_policy(
                RoleName=gw_role_name,
                PolicyName="PolicyEngineAccess",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:GetPolicyEngine",
                            "bedrock-agentcore:ListPolicies",
                            "bedrock-agentcore:GetPolicy",
                            "bedrock-agentcore:AuthorizeAction",
                            "bedrock-agentcore:PartiallyAuthorizeActions",
                        ],
                        "Resource": "*",
                    }],
                }),
            )
            logger.info(f"Granted PolicyEngineAccess to gateway role {gw_role_name}")
            # IAM policy propagation delay
            time.sleep(10)
        except Exception as e:
            logger.warning(f"Failed to grant PolicyEngineAccess to gateway role: {e}")

    # UpdateGateway requires re-supplying existing fields: gatewayIdentifier,
    # name, roleArn and authorizerType are the API's required members, so they are
    # read straight off the GetGateway response.
    update_kwargs = dict(
        gatewayIdentifier=gateway_id,
        name=gw["name"],
        roleArn=gw["roleArn"],
        authorizerType=gw["authorizerType"],
        policyEngineConfiguration={
            "arn": policy_engine_arn,
            "mode": "ENFORCE",
        },
    )
    # `protocolType` is OPTIONAL on UpdateGateway and GetGateway no longer returns
    # it, so `gw["protocolType"]` raised KeyError and took the whole request down.
    # The handler died before returning, so API Gateway answered with a bare 502
    # carrying no CORS header — and the browser reported a CORS failure, which is
    # what makes this worth a comment: the visible symptom named the wrong system
    # entirely, and the Admin Console meanwhile showed the checkboxes as saved.
    if gw.get("protocolType"):
        update_kwargs["protocolType"] = gw["protocolType"]
    if gw.get("authorizerConfiguration"):
        update_kwargs["authorizerConfiguration"] = gw["authorizerConfiguration"]
    control.update_gateway(**update_kwargs)
    logger.info(f"Associated policy engine {policy_engine_arn} (ENFORCE) with gateway {gateway_id}")


def build_cedar_statement(tool_name, user_ids):
    """Build a Cedar permit statement for a tool with per-user access control.

    Uses the permit model with default-deny:
      - If any user has the tool enabled → create a permit policy with principal.id checks
      - If no users have the tool → no permit → default-deny blocks the tool

    Requires gateway with authorizerType: CUSTOM_JWT so that principal.id
    is available during policy evaluation.

    `user_ids` must be Cognito **subs**, not emails. `principal.id` carries the
    token's `sub`; the Admin Console keys these rows on `user.sub` for exactly this
    reason. Measured on a live gateway: the identical statement written with an
    email is ACTIVE and matches nobody, so the tool vanishes from `tools/list` and
    the grant reads as a successful save that silently denies.

    The `principal is ...` guard is load-bearing rather than defensive. Without it
    the policy does not merely fail to match — it fails to ATTACH, with
    `attribute 'id' on entity type 'AgentCore::UnauthenticatedUser' not found`,
    leaving the policy UPDATE_FAILED while CreatePolicy/UpdatePolicy returned 200.

    Cedar schema (discovered via StartPolicyGeneration):
      action == AgentCore::Action::"{TargetName}___{toolName}"
      resource == AgentCore::Gateway::"{gatewayArn}"
      principal.id for user identity (from JWT)

    Returns (statement, entry) where `entry` is the tool's gateway_catalog row —
    the caller needs it to write the policy to the engine in the tool's OWN
    region. Returning the region alongside the statement rather than looking it up
    again is deliberate: the two lookups could disagree, and the failure mode is a
    valid-looking policy in the wrong region's engine, which permits nothing.
    """
    entry = gateway_catalog.entry_for(tool_name, s3_client)
    if entry is None:
        # Re-read the targets once: a tool registered after this container warmed
        # up is absent from the cached map, and the old fallback to the bare tool
        # name produced a policy that permits nothing while reporting success.
        entry = gateway_catalog.entry_for(tool_name, s3_client, refresh=True)
    if entry is None:
        # Still unknown — the tool is not on any Gateway target. Refuse rather
        # than writing a policy whose action the Gateway will never emit.
        raise ValueError(
            f"tool '{tool_name}' is not exposed by any gateway target, so no "
            f"Cedar action name exists for it; refusing to write a policy that "
            f"would silently permit nothing"
        )
    action_name = entry["actionName"]
    gateway_arn = gateway_catalog.gateway_arn(entry["gatewayId"], entry["region"])

    if not user_ids:
        return "", entry  # No permit → default-deny blocks this tool

    conditions = " || ".join(
        f'(principal.id) == "{uid}"' for uid in sorted(user_ids)
    )
    return (
        f'permit(\n'
        f'  principal,\n'
        f'  action == AgentCore::Action::"{action_name}",\n'
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        f') when {{\n'
        f'  ((principal is AgentCore::OAuthUser) || (principal is AgentCore::IamEntity)) &&\n'
        f'  ({conditions})\n'
        f'}};'
    ), entry


def _wait_for_policy_settled(engine_id, policy_id, timeout=30, region=None):
    """Block until a policy leaves CREATING/UPDATING. Returns its final status.

    UpdatePolicy and DeletePolicy both reject with ConflictException ("Policy
    cannot be updated while it is in UPDATING status") while a previous edit is
    still settling, and an update takes a few seconds to land. Two permission
    saves in quick succession — or one save that touches several tools — would
    otherwise have the second one fail, leaving DynamoDB saying the user has the
    tool while Cedar still denies it.
    """
    deadline = time.time() + timeout
    status = ""
    control = gateway_catalog.control(region or REGION)
    while time.time() < deadline:
        try:
            status = control.get_policy(
                policyEngineId=engine_id, policyId=policy_id).get("status", "")
        except Exception as e:
            logger.warning(f"Could not read policy {policy_id} status: {e}")
            return status
        if status not in ("CREATING", "UPDATING"):
            return status
        time.sleep(2)
    logger.warning(f"Policy {policy_id} still {status} after {timeout}s")
    return status


def rebuild_tool_policy(tool_name):
    """Scan DynamoDB for all users with this tool and create/update/delete the Cedar policy.

    The tool's own gateway decides which region's policy engine the policy is
    written to. Web search lives on a gateway in us-east-1, and a policy written
    to the us-west-2 engine for it would be ACTIVE, valid and completely inert.
    """
    # Resolve the gateway BEFORE provisioning an engine: `build_cedar_statement`
    # raises for a tool no gateway exposes, and provisioning an engine first would
    # leave a resource behind for a tool that cannot be governed.
    cedar_stmt, entry = build_cedar_statement(tool_name, _tool_user_ids(tool_name))
    region = entry["region"]
    engine_id, engine_arn = ensure_policy_engine(region)
    ensure_gateway_policy_engine(engine_arn, entry["gatewayId"], region)
    control = gateway_catalog.control(region)

    user_ids = _tool_user_ids(tool_name)

    # Look up existing policy for this tool in DynamoDB
    policy_record = table.get_item(
        Key={"userId": "__system__", "skillName": f"__tool_policy_{tool_name}__"}
    ).get("Item")
    existing_policy_id = policy_record.get("policyId") if policy_record else None

    policy_name = f"ToolPolicy_{tool_name.replace('-', '_')}"

    if not cedar_stmt:
        # No users have this tool → delete the permit policy (default-deny blocks it)
        if existing_policy_id:
            try:
                _wait_for_policy_settled(engine_id, existing_policy_id, region=region)
                control.delete_policy(
                    policyEngineId=engine_id, policyId=existing_policy_id)
            except Exception as e:
                logger.warning(f"Failed to delete policy {existing_policy_id}: {e}")
            table.delete_item(
                Key={"userId": "__system__", "skillName": f"__tool_policy_{tool_name}__"})
            logger.info(f"Deleted permit policy for tool '{tool_name}' (no authorized users)")
        return

    if existing_policy_id:
        # A policy still settling from a previous edit rejects the update with
        # ConflictException, which would leave DynamoDB and Cedar disagreeing
        # about who may call this tool.
        _wait_for_policy_settled(engine_id, existing_policy_id, region=region)
        control.update_policy(
            policyEngineId=engine_id,
            policyId=existing_policy_id,
            definition={"cedar": {"statement": cedar_stmt}},
            validationMode="IGNORE_ALL_FINDINGS",
        )
        logger.info(f"Updated policy {existing_policy_id} for tool '{tool_name}' "
                    f"with {len(user_ids)} users in {region}")
    else:
        # Create new policy
        create_resp = control.create_policy(
            policyEngineId=engine_id,
            name=policy_name,
            definition={"cedar": {"statement": cedar_stmt}},
            description=f"Controls access to the {tool_name} tool",
            validationMode="IGNORE_ALL_FINDINGS",
        )
        new_policy_id = create_resp["policyId"]
        table.put_item(Item={
            "userId": "__system__",
            "skillName": f"__tool_policy_{tool_name}__",
            "policyId": new_policy_id,
            "policyName": policy_name,
            # Which engine holds it, so a later delete does not have to re-derive
            # the region from a catalog that may have changed under it.
            "region": region,
            "updatedAt": now_iso(),
        })
        logger.info(f"Created policy {new_policy_id} for tool '{tool_name}' "
                    f"with {len(user_ids)} users in {region}")


def _tool_user_ids(tool_name):
    """Every user whose stored permissions include this tool."""
    user_ids = []
    scan_params = {
        "FilterExpression": "skillName = :sk",
        "ExpressionAttributeValues": {":sk": "__permissions__"},
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            allowed = item.get("allowedTools", [])
            if isinstance(allowed, set):
                allowed = list(allowed)
            if tool_name in allowed:
                user_ids.append(item["userId"])
        if "LastEvaluatedKey" not in resp:
            break
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return user_ids


# ---------------------------------------------------------------------------
# Skill File Management (S3)
# ---------------------------------------------------------------------------

def list_skill_files(event):
    """GET /skills/{userId}/{skillName}/files — list files in skill directory."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    prefix = f"{user_id}/{skill_name}/"

    resp = s3_client.list_objects_v2(Bucket=SKILL_FILES_BUCKET, Prefix=prefix)
    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        relative = key[len(prefix):]
        if not relative:
            continue
        files.append({
            "path": relative,
            "size": obj["Size"],
            "lastModified": obj["LastModified"].isoformat(),
        })
    return response(200, {"files": files})


def get_upload_url(event):
    """POST /skills/{userId}/{skillName}/files/upload-url — generate presigned PUT URL."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")
    directory = body.get("directory", "")
    filename = body.get("filename", "")

    if directory not in ALLOWED_FILE_DIRS:
        return response(400, {"error": f"Invalid directory '{directory}'. Must be one of: {', '.join(sorted(ALLOWED_FILE_DIRS))}"})
    if not filename or "/" in filename or "\\" in filename:
        return response(400, {"error": "Invalid filename"})

    key = f"{user_id}/{skill_name}/{directory}/{filename}"
    content_type = body.get("contentType", "application/octet-stream")

    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": SKILL_FILES_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=900,
    )
    return response(200, {"uploadUrl": url, "key": key})


def get_download_url(event):
    """POST /skills/{userId}/{skillName}/files/download-url — generate presigned GET URL."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")
    file_path = body.get("path", "")

    if not file_path:
        return response(400, {"error": "path is required"})

    key = f"{user_id}/{skill_name}/{file_path}"
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": SKILL_FILES_BUCKET, "Key": key},
        ExpiresIn=900,
    )
    return response(200, {"downloadUrl": url})


def delete_skill_file(event):
    """DELETE /skills/{userId}/{skillName}/files?path=... — delete a file."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    params = event.get("queryStringParameters") or {}
    file_path = params.get("path", "")

    if not file_path:
        return response(400, {"error": "path query parameter is required"})

    key = f"{user_id}/{skill_name}/{file_path}"
    s3_client.delete_object(Bucket=SKILL_FILES_BUCKET, Key=key)
    return response(200, {"message": f"File '{file_path}' deleted"})


# ---------------------------------------------------------------------------
# Knowledge Base Management
# ---------------------------------------------------------------------------


def _get_kb_config():
    """Get KB configuration from DynamoDB. Returns (kb_id, data_source_id) or (None, None)."""
    # Prefer env vars (set by setup script)
    if KB_ID and KB_DATA_SOURCE_ID:
        return KB_ID, KB_DATA_SOURCE_ID
    resp = table.get_item(Key={"userId": "__kb_config__", "skillName": "__default__"})
    item = resp.get("Item")
    if item and item.get("knowledgeBaseId"):
        return item["knowledgeBaseId"], item.get("dataSourceId", "")
    return None, None




def get_kb_status(_event):
    """GET /knowledge-bases — return KB status and document counts per scope."""
    kb_id, ds_id = _get_kb_config()

    result = {
        "initialized": bool(kb_id),
        "knowledgeBaseId": kb_id or "",
        "dataSourceId": ds_id or "",
        "status": "NOT_INITIALIZED",
        "scopes": [],
    }

    if kb_id:
        try:
            kb = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)
            result["status"] = kb["knowledgeBase"]["status"]
        except Exception as e:
            result["status"] = f"ERROR: {str(e)}"

    # List scopes (top-level prefixes in KB docs bucket)
    if KB_DOCS_BUCKET:
        try:
            resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Delimiter="/")
            for prefix in resp.get("CommonPrefixes", []):
                scope = prefix["Prefix"].rstrip("/")
                # Count files in this scope (exclude .metadata.json files)
                scope_resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Prefix=f"{scope}/")
                doc_count = sum(
                    1 for obj in scope_resp.get("Contents", [])
                    if not obj["Key"].endswith(".metadata.json")
                )
                result["scopes"].append({"scope": scope, "documentCount": doc_count})
        except Exception as e:
            logger.warning(f"Failed to list KB scopes: {e}")

    return response(200, result)


def list_kb_documents(event):
    """GET /knowledge-bases/documents?scope=__shared__ — list documents in a scope."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    params = event.get("queryStringParameters") or {}
    scope = params.get("scope", "__shared__")

    prefix = f"{scope}/"
    resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Prefix=prefix)

    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        # Skip metadata sidecar files
        if key.endswith(".metadata.json"):
            continue
        relative = key[len(prefix):]
        if not relative:
            continue
        files.append({
            "name": relative,
            "key": key,
            "size": obj["Size"],
            "lastModified": obj["LastModified"].isoformat(),
        })

    return response(200, {"scope": scope, "documents": files})


def get_kb_upload_url(event):
    """POST /knowledge-bases/documents/upload-url — get presigned PUT URL and create metadata sidecar."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    body = json.loads(event.get("body") or "{}")
    scope = body.get("scope", "__shared__")
    filename = body.get("filename", "")

    if not filename or "/" in filename or "\\" in filename:
        return response(400, {"error": "Invalid filename"})

    key = f"{scope}/{filename}"
    content_type = body.get("contentType", "application/octet-stream")

    # Generate presigned upload URL
    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": KB_DOCS_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=900,
    )

    # Pre-create metadata sidecar file (Bedrock KB expects simple key-value pairs)
    metadata_key = f"{key}.metadata.json"
    metadata_content = {
        "metadataAttributes": {
            "scope": scope,
        }
    }
    s3_client.put_object(
        Bucket=KB_DOCS_BUCKET,
        Key=metadata_key,
        Body=json.dumps(metadata_content),
        ContentType="application/json",
    )

    return response(200, {"uploadUrl": url, "key": key})


def delete_kb_document(event):
    """POST /knowledge-bases/documents/delete — delete a document and its metadata sidecar."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    body = json.loads(event.get("body") or "{}")
    key = body.get("key", "")

    if not key:
        return response(400, {"error": "key is required"})

    # Delete the document and its metadata sidecar
    objects_to_delete = [{"Key": key}]
    metadata_key = f"{key}.metadata.json"
    objects_to_delete.append({"Key": metadata_key})

    s3_client.delete_objects(
        Bucket=KB_DOCS_BUCKET,
        Delete={"Objects": objects_to_delete},
    )

    return response(200, {"message": f"Document '{key}' deleted"})


def start_kb_sync(event):
    """POST /knowledge-bases/sync — start a data source ingestion job."""
    kb_id, ds_id = _get_kb_config()
    if not kb_id or not ds_id:
        return response(500, {"error": "Knowledge base not initialized. Run scripts/setup-agentcore.py to set up the knowledge base."})

    try:
        resp = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
        )
        job = resp.get("ingestionJob", {})
        return response(200, {
            "message": "Sync started",
            "ingestionJobId": job.get("ingestionJobId", ""),
            "status": job.get("status", ""),
        })
    except Exception as e:
        return response(500, {"error": f"Failed to start sync: {str(e)}"})


def get_kb_sync_status(_event):
    """GET /knowledge-bases/sync — get latest ingestion job status."""
    kb_id, ds_id = _get_kb_config()
    if not kb_id or not ds_id:
        return response(200, {"status": "NOT_INITIALIZED", "jobs": []})

    try:
        resp = bedrock_agent_client.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            maxResults=5,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
        jobs = []
        for job in resp.get("ingestionJobSummaries", []):
            jobs.append({
                "ingestionJobId": job.get("ingestionJobId", ""),
                "status": job.get("status", ""),
                "startedAt": job.get("startedAt", "").isoformat() if hasattr(job.get("startedAt", ""), "isoformat") else str(job.get("startedAt", "")),
                "updatedAt": job.get("updatedAt", "").isoformat() if hasattr(job.get("updatedAt", ""), "isoformat") else str(job.get("updatedAt", "")),
                "statistics": job.get("statistics", {}),
            })
        return response(200, {"status": "OK", "jobs": jobs})
    except Exception as e:
        return response(500, {"error": f"Failed to get sync status: {str(e)}"})


# ---------------------------------------------------------------------------
# AgentCore Registry — Import approved records into DynamoDB skills
# ---------------------------------------------------------------------------

def _parse_skill_md(skill_md_text):
    """Mirror of skill-erp-api parser; returns (description, body, tools, meta)."""
    if not skill_md_text:
        return "", "", [], {}
    lines = skill_md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", skill_md_text, [], {}
    try:
        end_idx = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return "", skill_md_text, [], {}
    frontmatter = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")

    description = ""
    allowed_tools = []
    metadata = {}
    for raw in frontmatter:
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "description":
            description = value
        elif key == "allowed_tools":
            inner = value.strip("[]")
            allowed_tools = [t.strip() for t in inner.split(",") if t.strip()]
        elif key.startswith("x-"):
            metadata[key[2:]] = value
    return description, body, allowed_tools, metadata


def list_registry_records(event):
    """GET /registry/records?status=APPROVED — list records from the registry."""
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    params = event.get("queryStringParameters") or {}
    status_filter = params.get("status", "APPROVED")

    records = []
    token = None
    try:
        while True:
            # GA: structured filters, and descriptorType=AGENT_SKILLS becomes
            # recordType=SKILL.
            kwargs = {"registryId": REGISTRY_ID, "maxResults": 50}
            filters = [{"name": "recordType",
                        "values": [registry_ns.RECORD_TYPE_SKILL]}]
            if status_filter and status_filter != "ALL":
                filters.append({"name": "status", "values": [status_filter]})
            kwargs["filters"] = filters
            if token:
                kwargs["nextToken"] = token
            resp = registry_control.list_registry_records(**kwargs)
            for r in resp.get("registryRecords", []):
                records.append({
                    "recordId": r.get("recordId", ""),
                    # GA moved the human-readable label to displayName and made
                    # `name` the dedup key. Prefer displayName, fall back to name
                    # so a preview-era record still shows something.
                    "name": r.get("displayName") or r.get("name", ""),
                    "description": r.get("description", ""),
                    "status": r.get("status", ""),
                    "recordVersion": r.get("recordVersion", ""),
                    "createdAt": r.get("createdAt").isoformat() if hasattr(r.get("createdAt"), "isoformat") else str(r.get("createdAt", "")),
                    "updatedAt": r.get("updatedAt").isoformat() if hasattr(r.get("updatedAt"), "isoformat") else str(r.get("updatedAt", "")),
                })
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        return response(500, {"error": f"Failed to list registry records: {str(e)}"})

    return response(200, {"records": records})


# ---------------------------------------------------------------------------
# Skill approval (POST /registry/records?action=review)
#
# The approval state machine is Registry-managed and, until now, had no caller.
# `agent-registry:UpdateRegistryRecordStatus` was already granted to this Lambda
# in the CDK stack and never used, so a skill published from the Skill ERP sat in
# PENDING_APPROVAL with the only way to move it being the AWS console. Reviewing
# in the Admin Console is what makes the curation gate part of the product rather
# than a manual step someone has to be told about.
#
# Transitions measured against a throwaway record rather than read off the docs:
#
#   DRAFT            -> PENDING_APPROVAL | DEPRECATED | DRAFT   (not REJECTED)
#   PENDING_APPROVAL -> APPROVED | REJECTED
#   REJECTED         -> APPROVED                                (reversible)
#
# So a DRAFT record cannot be rejected outright — it has to be submitted first,
# which is why `approve` handles both and `reject` refuses with an explanation
# rather than a raw ValidationException listing enum members.
# ---------------------------------------------------------------------------

_REVIEWABLE = ("approve", "reject", "deprecate")


def review_registry_record(event):
    """POST /registry/records?action=review — approve, reject or deprecate.

    Body: {recordId, decision: approve|reject|deprecate, reason?}. The reason is
    stored as the record's `statusReason`, which is the only place the Registry
    keeps *why* — so it is required for a rejection and optional otherwise. A
    rejected skill whose reason is blank tells the author nothing.
    """
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    body = json.loads(event.get("body") or "{}")
    record_id = (body.get("recordId") or "").strip()
    decision = (body.get("decision") or "").strip().lower()
    reason = (body.get("reason") or "").strip()

    if not record_id:
        return response(400, {"error": "recordId is required"})
    if decision not in _REVIEWABLE:
        return response(400, {
            "error": f"decision must be one of {list(_REVIEWABLE)}"})
    if decision == "reject" and not reason:
        # The author sees only the statusReason, so an unexplained rejection is
        # indistinguishable from the system losing their skill.
        return response(400, {
            "error": "a reason is required when rejecting — it is the only "
                     "feedback the skill's author receives"})

    reviewer = _caller_identity(event)
    try:
        current = registry_control.get_registry_record(
            registryId=REGISTRY_ID, recordId=record_id)
    except Exception as e:  # noqa: BLE001
        return response(404, {"error": f"no such record: {e}"})
    status = current.get("status", "")

    # ---- the approval gate -------------------------------------------------
    # Approving an AGENT record is what makes it discoverable: the console lists it,
    # the orchestrator registers tools for it, the delegation prompt names it. Until
    # now the check that its own Runtime authorizer agrees with the card was a REPORT
    # an admin might read, on a page they might not open. Making it a gate moves the
    # correction loop entirely to the team that can act on it — the one that deployed
    # the runtime — instead of routing it through whoever happens to click Approve.
    #
    # Both blocked directions are silent in production, and both are worse than an
    # unapproved record:
    #   OPEN   - authorization is not happening; anyone in the pool can reach it.
    #   CLOSED - nobody can reach it; the model offers the tool and then apologises.
    # INFO passes: an unreadable runtime (someone else's account) and the
    # still-coupled-but-working pre-migration authorizer are both real states that a
    # platform admin should be able to approve.
    gate = None
    if decision == "approve" and _is_agent_record(current):
        gate = _conformance_gate(record_id, current)
        forced = str((event.get("queryStringParameters") or {})
                     .get("force", "")).lower() in ("1", "true", "yes")
        if gate and gate["blocking"] and not forced:
            logger.warning("approval of %s blocked by conformance: %s",
                           record_id, gate["severity"])
            return response(409, {
                "error": "this agent's Runtime authorizer does not match the card it "
                         "registered, so approving it would publish an agent that is "
                         "either unreachable or unprotected",
                "status": status,
                "conformance": gate,
                "hint": "the agent's own team fixes this by re-running "
                        "scripts/a2a-authorizer-contract.py against their runtime. "
                        "Add ?force=true to approve anyway and record the override.",
            })
        if gate and gate["blocking"] and forced:
            # Loud, and in the record's own statusReason below, because an override of
            # an authorization check must not be reconstructable only from a Lambda log
            # that ages out.
            logger.warning("CONFORMANCE OVERRIDE: %s approved by %s despite %s: %s",
                           record_id, reviewer, gate["severity"], gate["findings"])
            reason = (f"{reason} [conformance override: {gate['severity']}]"
                      if reason else f"[conformance override: {gate['severity']}]")

    stamped = f"{reason or decision} (by {reviewer})" if reviewer else (reason or decision)

    try:
        if decision == "approve":
            # approve_record submits first when the record is still DRAFT, because
            # DRAFT cannot go straight to APPROVED.
            final = registry_ns.approve_record(
                registry_control, REGISTRY_ID, record_id, reason=stamped)
        elif decision == "deprecate":
            registry_ns.set_record_status(
                registry_control, REGISTRY_ID, record_id,
                registry_ns.STATUS_DEPRECATED, reason=stamped)
            final = registry_ns.STATUS_DEPRECATED
        else:
            if status == registry_ns.STATUS_DRAFT:
                return response(409, {
                    "error": "a DRAFT record cannot be rejected — it has not been "
                             "submitted for review yet. Deprecate it instead, or "
                             "wait for the author to submit it.",
                    "status": status})
            registry_ns.set_record_status(
                registry_control, REGISTRY_ID, record_id,
                registry_ns.STATUS_REJECTED, reason=stamped)
            final = registry_ns.STATUS_REJECTED
    except registry_ns.RecordNotApprovable as e:
        # A transition the state machine does not have, not a server fault. Most
        # often DEPRECATED, which is terminal: the record can only be recreated, and
        # that mints a new recordId and voids every grant keyed on the old one.
        # Reported as a conflict so the console can say so, rather than as a 200
        # carrying an unchanged status — which is how this used to present.
        logger.warning("review of %s could not be applied: %s", record_id, e)
        return response(409, {"error": str(e), "status": status,
                              "terminal": status == registry_ns.STATUS_DEPRECATED})
    except Exception as e:  # noqa: BLE001
        logger.exception("review failed for %s", record_id)
        return response(500, {"error": str(e), "status": status})

    logger.info("record %s: %s -> %s by %s", record_id, status, final, reviewer)

    # A decision that removes an AGENT record from the grantable set takes effect
    # NOW rather than at the next scheduled sweep. Inline because an admin who has
    # just rejected an agent expects its access to be gone when they look, and a
    # revocation that lands minutes later is indistinguishable from one that failed.
    #
    # Best-effort: the status change is the outcome this endpoint promises, so a
    # sweep failure is reported alongside it, never instead of it. The schedule
    # will pick it up regardless.
    sweep_summary = None
    if final in (registry_ns.STATUS_REJECTED, registry_ns.STATUS_DEPRECATED):
        try:
            swept = sweep_a2a_revocations(event)
            sweep_summary = json.loads(swept.get("body") or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("inline A2A sweep after reviewing %s failed: %s",
                           record_id, exc)
            sweep_summary = {"swept": False, "error": str(exc)}

    return response(200, {"recordId": record_id, "previousStatus": status,
                          "status": final, "decision": decision,
                          "reviewedBy": reviewer, "reason": reason,
                          # Absent for an approval, which grants nothing by itself
                          # — the next sweep or save materialises it.
                          "a2aSweep": sweep_summary,
                          # Present on an AGENT approval even when it passed, so the
                          # console can surface an INFO (e.g. "still enumerates skill
                          # groups") that did not block.
                          "conformance": gate})


def _is_agent_record(detail: dict) -> bool:
    """Does this GetRegistryRecord response describe an AGENT (not a SKILL)?

    Checked via the card rather than a type field, because that is what the gate needs
    anyway and because a SKILL record has no runtime to check — running the gate on one
    would report `runtime-unreadable` on every skill approval.
    """
    try:
        return bool(registry_ns.read_agent_card(detail))
    except Exception:  # noqa: BLE001
        return False


def _conformance_gate(record_id: str, detail: dict) -> dict | None:
    """Conformance findings for one record, shaped for the approval gate.

    Returns None when the check could not be run at all, which does NOT block: being
    unable to check is not evidence of a problem, and refusing every approval because
    the control plane was throttled would make the gate the outage.
    """
    if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
        logger.warning("conformance gate skipped for %s: pool/app client not "
                       "configured in this Lambda's env", record_id)
        return None
    try:
        card = json.loads(registry_ns.read_agent_card(detail) or "{}")
    except ValueError:
        return None
    url = (card or {}).get("url") or ""
    runtime_id, via = a2a_runtimes.resolve(url, agentcore_control)
    authorizer = None
    if runtime_id:
        try:
            authorizer = agentcore_control.get_agent_runtime(
                agentRuntimeId=runtime_id).get("authorizerConfiguration") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("conformance gate could not read runtime %s: %s",
                           runtime_id, exc)
            via = f"{via}; GetAgentRuntime failed: {exc}"

    findings = a2a_conformance.check(
        card, authorizer, _discovery_url(), COGNITO_APP_CLIENT_ID)
    severity = a2a_conformance.worst_severity(findings)
    return {
        "runtimeId": runtime_id,
        "resolvedVia": via,
        "conformant": not findings,
        "severity": severity,
        # INFO never blocks — see the call site.
        "blocking": severity in (a2a_conformance.OPEN, a2a_conformance.CLOSED),
        "findings": findings,
    }


def _caller_identity(event) -> str:
    claims = (event.get("requestContext", {})
              .get("authorizer", {}).get("claims", {}))
    return claims.get("email") or claims.get("cognito:username") or claims.get("sub", "")


def import_registry_records(event):
    """POST /registry/import  body: {recordIds: [...], userId: "__global__" | <userId>}

    Pulls SKILL.md content from each record, then writes a skill row into
    DynamoDB (same shape as manual skills created from the Skills tab)."""
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    body = json.loads(event.get("body") or "{}")
    record_ids = body.get("recordIds", []) or []
    target_user = body.get("userId", "__global__")
    if not isinstance(record_ids, list) or not record_ids:
        return response(400, {"error": "recordIds must be a non-empty list"})

    imported = []
    errors = []
    for rid in record_ids:
        try:
            r = registry_control.get_registry_record(
                registryId=REGISTRY_ID, recordId=rid
            )
            if r.get("status", "") != "APPROVED":
                errors.append(f"{rid}: skipped — not APPROVED")
                continue
            # displayName carries what preview called `name`; `name` is now the
            # dedup key, which may have a collision suffix on it.
            skill_name = r.get("displayName") or r.get("name", "")
            if not skill_name or not SKILL_NAME_RE.match(skill_name):
                errors.append(f"{rid}: invalid name '{skill_name}'")
                continue

            # GA: agentSkillsDefinition.data + .additionalData.skillMd.data, with a
            # preview fallback so an old record still imports.
            skill_def_raw, skill_md = registry_ns.read_skill_definition(r)

            description_fm, instructions, allowed_tools, metadata = _parse_skill_md(skill_md)
            description = r.get("description") or description_fm

            license_name = ""
            compatibility = ""
            try:
                sd = json.loads(skill_def_raw) if skill_def_raw else {}
                meta = sd.get("_meta") or {}
                license_name = meta.get("license", "") or ""
                compatibility = meta.get("compatibility", "") or ""
            except Exception:
                pass

            ts = now_iso()
            item = {
                "userId": target_user,
                "skillName": skill_name,
                "description": description or "(imported from registry)",
                "instructions": instructions or "",
                "allowedTools": allowed_tools or [],
                "createdAt": ts,
                "updatedAt": ts,
                "importedFromRegistry": rid,
            }
            if license_name:
                item["license"] = license_name
            if compatibility:
                item["compatibility"] = compatibility
            if metadata:
                item["metadata"] = metadata
            table.put_item(Item=item)
            imported.append({"recordId": rid, "skillName": skill_name, "userId": target_user})
        except Exception as e:
            errors.append(f"{rid}: {str(e)}")

    return response(200, {
        "imported": imported,
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# A2A Agents (Integration Registry admin view — read only)
# ---------------------------------------------------------------------------

def _a2a_iso(ts):
    if not ts:
        return ""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _scan_a2a_ownership_map():
    """Return {recordId: {sub, email}} from all a2a ownership rows. One scan."""
    owner_map = {}
    from boto3.dynamodb.conditions import Attr
    scan_params = {
        "FilterExpression": Attr("userId").eq("__erp_owner__") & Attr("recordType").eq("a2a"),
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            sk = item.get("skillName", "")
            if sk.startswith("a2a:"):
                rid = sk[len("a2a:"):]
                owner_map[rid] = {
                    "sub": item.get("ownerSub", ""),
                    "email": item.get("ownerEmail", ""),
                }
        if "LastEvaluatedKey" not in resp:
            break
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return owner_map


def list_registry_skills(_event):
    """GET /registry/records?action=skill-list — approved SKILL records for the
    Integration Registry's Skills sub-tab.

    The read-only sibling of `list_a2a_agents`. Two enrichments the raw records do
    not carry:

      - `publishedBy`, from the Skill ERP ownership rows, so a curator can see who
        published something without opening each record.
      - `importedBy`, the scopes that have already imported this record into the
        skills table (`importedFromRegistry`). Without it the page cannot answer
        "is this live for anyone", which is the question an admin actually has, and
        the Import button would invite a duplicate import of something already in
        use.

    Always 200. A record whose descriptors cannot be parsed contributes a row with
    the metadata that did read rather than being dropped: a skill that is present but
    malformed is a thing the curator needs to see, and silently omitting it looks
    identical to it not existing.
    """
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    try:
        records = registry_ns.list_records(
            registry_control, REGISTRY_ID,
            record_type=registry_ns.RECORD_TYPE_SKILL,
            status=registry_ns.STATUS_APPROVED)
    except Exception as exc:  # noqa: BLE001
        # Same reasoning as the A2A catalog: a failed lookup must not render as an
        # empty registry, or an admin goes looking for records to approve.
        logger.warning("failed to list SKILL records: %s", exc)
        return response(200, {"skills": [], "catalogError": str(exc)})

    owner_map = {}
    try:
        owner_map = _scan_skill_ownership_map()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to build the skill ownership map: %s", exc)

    imported_map = {}
    try:
        imported_map = _scan_imported_skill_map()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to build the imported-skill map: %s", exc)

    out = []
    for record in records:
        record_id = record.get("recordId", "")
        display = record.get("displayName") or record.get("name", "")
        row = {
            "recordId": record_id,
            "name": display,
            "dedupName": record.get("name", ""),
            "description": record.get("description", ""),
            "version": record.get("recordVersion", ""),
            "status": record.get("status", ""),
            "updatedAt": _a2a_iso(record.get("updatedAt")),
            "publishedBy": (owner_map.get(record_id) or {}).get("email", ""),
            "importedBy": sorted(imported_map.get(record_id, [])),
            "license": "",
            "compatibility": "",
            "skillMd": "",
        }
        try:
            detail = registry_control.get_registry_record(
                registryId=REGISTRY_ID, recordId=record_id)
            definition_raw, skill_md = registry_ns.read_skill_definition(detail)
            row["skillMd"] = skill_md or ""
            if definition_raw:
                meta = (json.loads(definition_raw) or {}).get("_meta") or {}
                row["license"] = meta.get("license", "") or ""
                row["compatibility"] = meta.get("compatibility", "") or ""
                # The built-in skills are published by the deploy, not by a person
                # through the Skill ERP, so they have no ownership row and would
                # show a bare "—" under Published by. "the deploy did" is a real
                # answer and a more useful one.
                if not row["publishedBy"]:
                    row["publishedBy"] = meta.get("publishedBy", "") or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read SKILL descriptors for %s: %s",
                           record_id, exc)
            row["readError"] = str(exc)
        out.append(row)

    return response(200, {"skills": out, "catalogError": ""})


def _scan_skill_ownership_map():
    """{recordId: {sub, email}} from the Skill ERP's skill ownership rows.

    Mirrors `_scan_a2a_ownership_map`, which filters on `recordType == "a2a"`; skill
    rows are the other half of the same `__erp_owner__` partition.
    """
    from boto3.dynamodb.conditions import Attr

    owner_map = {}
    scan_params = {
        "FilterExpression": Attr("userId").eq("__erp_owner__")
        & Attr("recordType").eq("skill"),
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            sk = item.get("skillName", "")
            record_id = sk.split(":", 1)[1] if ":" in sk else sk
            owner_map[record_id] = {
                "sub": item.get("ownerSub", ""),
                "email": item.get("ownerEmail", ""),
            }
        if "LastEvaluatedKey" not in resp:
            return owner_map
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _scan_imported_skill_map():
    """{recordId: [scope, ...]} for records already imported into the skills table.

    `importedFromRegistry` is written by `import_registry_records`, so this is the
    reverse lookup: which scopes are actually running a given registry skill.
    """
    from boto3.dynamodb.conditions import Attr

    out: dict[str, set] = {}
    scan_params = {
        "FilterExpression": Attr("importedFromRegistry").exists(),
        "ProjectionExpression": "userId, importedFromRegistry",
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            record_id = item.get("importedFromRegistry")
            if record_id:
                out.setdefault(str(record_id), set()).add(item.get("userId", ""))
        if "LastEvaluatedKey" not in resp:
            return {k: sorted(v) for k, v in out.items()}
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def list_a2a_agents(_event):
    """GET /registry/records?action=a2a-list — the live A2A inventory.

    Read straight from the Registry on every request, with no cache: registering a
    record is the ONLY thing that makes an A2A agent visible to this system, so this
    page must not be able to show a stale answer.

    Lists records in EVERY status, not just APPROVED. An agent knocked back to DRAFT
    by an edit, or REJECTED by a reviewer, used to vanish from here entirely — which
    is precisely when an admin comes looking, and "access to the energy specialist
    disappeared" has no answer on a page that only shows healthy records. Each row
    carries whether its skills may be granted right now and why, plus the seconds
    left if it is inside its re-approval window, so a revocation can be seen coming.

    One status this cannot show, measured 2026-08-15: **DEPRECATED**. Deprecating
    does not just make a record terminal, it removes it from the API — a
    `GetRegistryRecord` on it answers `ResourceNotFoundException` and it lists under
    no status at all. So a deprecated agent is absent here because it no longer
    exists, not because of a filter. `grantable(record=None)` is what covers it, and
    the sweep revokes its groups because no grantable agent carries that name.
    """
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    try:
        records = _fetch_a2a_records()
    except Exception as e:
        return response(500, {"error": f"Failed to list A2A records: {str(e)}"})

    owner_map = {}
    try:
        owner_map = _scan_a2a_ownership_map()
    except Exception as e:
        logger.warning("Failed to build A2A ownership map: %s", e)

    verdicts = _grantability(records)
    now = time.time()
    grace = registry_ns.grant_grace_seconds()

    result = []
    for r in records:
        rid = r["recordId"]
        grantable, reason = verdicts[rid]
        owner = owner_map.get(rid, {})
        result.append({
            "recordId": rid,
            "name": r["name"],
            "description": r["description"],
            "status": r["status"],
            "createdAt": _a2a_iso(r.get("createdAt")),
            "updatedAt": _a2a_iso(r.get("updatedAt")),
            "card": r["card"],
            "publishedBy": owner.get("email") or owner.get("sub") or "",
            # Why a granted user can or cannot reach this agent at this moment.
            # Separate from `status` because the two genuinely differ: a DRAFT
            # record inside its window is not approved and is still grantable.
            "grantable": grantable,
            "grantableReason": reason,
            "graceRemainingSeconds": registry_ns.grace_remaining_seconds(
                r, now, grace),
        })

    return response(200, {"records": result, "graceSeconds": grace})


# ---------------------------------------------------------------------------
# Agent fleet (GET /registry/records?action=fleet)
#
# Rides on the existing /registry/records resource rather than adding a path:
# the admin Lambda's auto-generated API Gateway resource policy is already near
# the 20 KB cap, and a new resource would push it over. Same reason the a2a-list
# and a2a-grants actions live there.
# ---------------------------------------------------------------------------

def list_agent_fleet(event):
    """Every agent in the fleet, derived from runtimes + Registry + metadata.

    Deliberately tolerant: each of the three sources is optional. A missing
    Registry (or one this caller cannot read) still yields the runtime list, and a
    dashboard health call that fails still yields the fleet without its metrics.
    An operator opening this page during a partial outage should see what IS
    working, not an error.
    """
    import agents as fleet_model

    runtime_arns = []
    try:
        runtime_arns = dashboard._all_runtime_arns()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not resolve runtime ARNs: %s", e)

    registry_records = []
    if REGISTRY_ID:
        try:
            for rec in registry_ns.list_records(
                    registry_control, REGISTRY_ID,
                    record_type=registry_ns.RECORD_TYPE_AGENT,
                    status=registry_ns.STATUS_APPROVED):
                rid = rec.get("recordId", "")
                if not rid:
                    continue
                try:
                    detail = registry_control.get_registry_record(
                        registryId=REGISTRY_ID, recordId=rid)
                    raw = registry_ns.read_agent_card(detail)
                    card = json.loads(raw) if raw else {}
                except Exception as e:  # noqa: BLE001
                    logger.warning("fleet: card unreadable for %s: %s", rid, e)
                    card = {}
                registry_records.append({
                    "recordId": rid,
                    "displayName": rec.get("displayName") or rec.get("name", ""),
                    "name": rec.get("name", ""),
                    "status": rec.get("status", ""),
                    "card": card,
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("fleet: Registry listing failed: %s", e)

    metadata = fleet_model.load_metadata(table)

    # Per-runtime metrics the dashboard already computes. Reused rather than
    # re-queried: CloudWatch GetMetricData is the expensive part of that call and
    # the fleet page needs exactly the same numbers.
    health_runtimes = []
    try:
        # 24h window: the fleet page wants "is this agent being used", not a
        # month's trend, and the shorter window is the cheaper CloudWatch call.
        health = dashboard._fetch_health(1)
        health_runtimes = (health or {}).get("runtimes") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("fleet: health breakdown unavailable: %s", e)

    # The navigation DeepLink is a Gateway target, not an agent — included so an
    # operator can see where the seventh entity of the architecture went.
    gateway_tools = []
    try:
        listed = json.loads(list_gateway_tools(event).get("body") or "{}")
        gateway_tools = listed.get("tools") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("fleet: gateway tool listing failed: %s", e)

    fleet = fleet_model.build_fleet(
        runtime_arns=runtime_arns,
        registry_records=registry_records,
        metadata=metadata,
        health_runtimes=health_runtimes,
        gateway_tools=gateway_tools,
    )
    return response(200, {"agents": fleet, "count": len(fleet)})


# ---------------------------------------------------------------------------
# Scenarios (GET /registry/records?action=scenarios | sync-schedules)
#
# Rides on /registry/records for the same reason the fleet does: the admin
# Lambda's auto-generated API Gateway resource policy is near the 20 KB cap and a
# new path would push it over.
# ---------------------------------------------------------------------------

SCENARIOS_TABLE_NAME = os.environ.get("SCENARIOS_TABLE_NAME", "smarthome-scenarios")


def _scenarios_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(SCENARIOS_TABLE_NAME)


def list_scenarios(event):
    """Every saved scene, across users, with its schedule state.

    An operator's view: which automations exist, what fires them, and whether the
    last run worked. `lastRunAt`/`lastRunOk` come from the runner, and they are the
    only way to answer "did it fire" for something that runs at 07:30 when nobody
    is watching.
    """
    import scenarios as sc

    items = []
    try:
        # Deliberately NOT named `table`: the module-level `table` is the skills
        # table, and shadowing it here would send the settings reads below to the
        # scenarios table, where they would find nothing and report UTC for
        # everyone.
        scenarios_table = _scenarios_table()
        kwargs = {}
        while True:
            resp = scenarios_table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:  # noqa: BLE001
        logger.warning("could not list scenarios: %s", e)
        return response(200, {"scenarios": [], "count": 0, "error": str(e)[:200]})

    # The owner's timezone, memoised across rows: several scenes usually belong to
    # the same user, and a cron reads wrong without the zone it runs in — "0 15"
    # is 23:00 in Shanghai and 07:00 in Los Angeles.
    tz_cache: dict[str, str] = {}

    def _timezone(user_id: str) -> str:
        if user_id not in tz_cache:
            try:
                settings = table.get_item(
                    Key={"userId": user_id, "skillName": "__settings__"}
                ).get("Item") or {}
                tz_cache[user_id] = str(settings.get("timezone") or "UTC")
            except Exception:  # noqa: BLE001
                tz_cache[user_id] = "UTC"
        return tz_cache[user_id]

    out = []
    for item in items:
        row = sc.summarise(item)
        trigger = item.get("trigger") or {}
        user_id = item.get("userId", "")
        row["userId"] = user_id
        # `wants_schedule`, not `bool(cron_for(...))`. A solar scene HAS a schedule
        # but `cron_for` cannot name it — the expression depends on the owner's
        # coordinates and today's date, and is computed by scenario_schedules /
        # the runner. Asking the wrong question here made the page report "no
        # schedule" for a sunrise scene that was in fact scheduled.
        row["scheduled"] = sc.wants_schedule(trigger)
        if trigger.get("sceneType") == sc.SCENE_SOLAR:
            # Read the live expression rather than recomputing it: what matters to
            # an operator is what Scheduler actually holds, which is also how a
            # drifted or missing schedule becomes visible.
            row["cron"], row["timezone"] = _live_schedule(user_id, row["scenarioId"])
        else:
            row["cron"] = sc.cron_for(trigger)
            row["timezone"] = _timezone(user_id) if row["cron"] else ""
        row["lastRunAt"] = item.get("lastRunAt", "")
        row["lastRunOk"] = item.get("lastRunOk")
        # Truncated. The runner stores the whole per-device MCP reply, which is a
        # few hundred characters of nested JSON per action — useful in the table
        # only as "something went wrong here", and the full text is in the row and
        # the runner's log for anyone who needs it.
        detail = str(item.get("lastRunDetail", "") or "")
        row["lastRunDetail"] = detail[:200] + ("…" if len(detail) > 200 else "")
        out.append(row)
    out.sort(key=lambda r: r.get("updatedAt", ""), reverse=True)
    return response(200, {"scenarios": out, "count": len(out)})


def _live_schedule(user_id: str, scenario_id: str) -> tuple[str, str]:
    """(expression, timezone) as EventBridge Scheduler currently holds it.

    ("", "") when there is no schedule — which for a solar scene means it is not
    firing, and is exactly the state the page exists to make visible.
    """
    import scenarios as sc
    import scenario_schedules

    try:
        sched = scenario_schedules._client().get_schedule(
            Name=sc.schedule_name(user_id, scenario_id),
            GroupName=os.environ.get("SCENARIO_SCHEDULE_GROUP", "smarthome-scenarios"),
        )
    except Exception:  # noqa: BLE001
        # ResourceNotFound is the expected case, not an error worth logging per row.
        return "", ""
    return (sched.get("ScheduleExpression", ""),
            sched.get("ScheduleExpressionTimezone", ""))


def sync_scenario_schedules(event):
    """Reconcile EventBridge Scheduler against the saved scenes.

    Exposed as an endpoint rather than run only on write, because the scenes are
    written by an A2A sub-agent that has no business holding Scheduler permissions
    — it stores data, and this control plane turns that data into schedules.
    """
    import scenario_schedules

    # The skills table comes along because that is where each owner's timezone
    # lives, and a schedule created without it runs in UTC.
    return response(200, scenario_schedules.sync_schedules(_scenarios_table(), table))


# ---------------------------------------------------------------------------
# Scenes as code (spec 5 S6)
#
# Export and import a user's scenes as JSON. For the developer-shaped end users
# this product has: paste a definition instead of describing it in a dozen turns,
# keep it in version control, move it between accounts.
#
# Import goes through `scenarios.build_scenario`, the same validator the A2A agent
# uses. That is the point — an imported scene is validated exactly like a spoken
# one, so it cannot store a device action the execution path would then refuse. A
# separate import validator would be a second definition of "valid", and it would
# drift.
# ---------------------------------------------------------------------------

# Fields that describe the scene ITSELF. Everything else on the row is either
# derived (scenarioKey, scenarioId), owner-specific (userId), or runtime state
# (lastRunAt, lastRunOk, lastRunDetail, createdAt) — exporting those would invite a
# round trip that claims to restore a run history it cannot.
_EXPORTABLE_FIELDS = ("name", "description", "trigger", "deviceActions",
                      "isActive", "isTemplate")


def _scenario_owner_keys(user_id: str) -> list[str]:
    """Every key a user's scene rows might be stored under, most likely first.

    Scenes are keyed by Cognito **sub**, not email — the A2A agent stores them from
    the verified token's `sub`, and the runner reads them back the same way. But
    every other admin surface (settings, prompts, A2A grants) is keyed by EMAIL,
    and the Scenarios page passes whatever it has.

    So both are tried. This is the same two-key-space trap that once wrote a
    settings row under `admin%40smarthome.local`: the failure mode is an empty
    result for a user who plainly has data, which reads as "the feature does not
    work" rather than "you asked about a different key".
    """
    keys = [user_id]
    if "@" in user_id:
        sub = _resolve_sub_for_email(user_id)
        if sub and sub not in keys:
            keys.append(sub)
    return keys


def _resolve_sub_for_email(email: str) -> str:
    """The Cognito sub for an email, or "" if it cannot be resolved.

    The inverse of `_resolve_ddb_user_key`, which goes sub -> email for the rows
    keyed that way. Scenes are the one table keyed by sub, hence both directions
    existing in the same file.
    """
    if not COGNITO_USER_POOL_ID:
        return ""
    try:
        resp = boto3.client("cognito-idp", region_name=REGION).list_users(
            UserPoolId=COGNITO_USER_POOL_ID, Filter=f'email = "{email}"', Limit=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not resolve sub for %s: %s", email, e)
        return ""
    for user in resp.get("Users", []):
        for attr in user.get("Attributes", []):
            if attr.get("Name") == "sub":
                return attr.get("Value", "")
    return ""


def export_scenarios(event):
    """One user's scenes as a portable JSON document.

    `?userId=` is required rather than defaulting to every user: a scene carries
    device ids and daily routines, and an export endpoint that silently dumped the
    whole fleet's automations would be a data-disclosure bug wearing a convenience
    feature's clothes.
    """
    import scenarios as sc

    params = event.get("queryStringParameters") or {}
    user_id = unquote(params.get("userId", "") or "")
    if not user_id:
        return response(400, {"error": "userId is required"})

    items = []
    try:
        scenarios_table = _scenarios_table()
        for key in _scenario_owner_keys(user_id):
            resp = scenarios_table.query(
                KeyConditionExpression=Key("userId").eq(key))
            items = resp.get("Items", [])
            if items:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("scene export query failed for %s: %s", user_id, e)
        return response(500, {"error": f"could not read scenes: {str(e)[:200]}"})

    scenes = []
    for item in items:
        if sc.is_template_key(item.get("scenarioKey", "")):
            continue  # templates are library content, not this user's automations
        scenes.append({k: _plain(item[k]) for k in _EXPORTABLE_FIELDS if k in item})

    return response(200, {
        "version": 1,
        "exportedFor": user_id,
        "count": len(scenes),
        "scenes": scenes,
    })


def _plain(value):
    """DynamoDB Decimals to JSON numbers, recursively.

    `json.dumps` cannot serialise Decimal, and the response helper's `default=str`
    would quietly turn brightness 20 into the string "20" — which then fails
    validation on the way back in, on an export the user never edited.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def import_scenarios(event):
    """Create scenes from an exported (or hand-written) JSON document.

    Per-scene outcomes rather than all-or-nothing: a document with one bad trigger
    should not cost the user the other five scenes, and a partial import that says
    exactly which entries failed and why is more useful than a single 400.

    Does NOT create schedules. `sync-schedules` does that, and keeping them
    separate means an import cannot start firing automations as a side effect of
    being parsed — the operator reconciles when they are ready.
    """
    import scenarios as sc

    body = json.loads(event.get("body") or "{}")
    user_id = unquote(body.get("userId", "") or "")
    scenes = body.get("scenes")
    if not user_id:
        return response(400, {"error": "userId is required"})
    # Scenes are keyed by Cognito **sub**: that is what the A2A agent writes and
    # what the scenario runner reads. Importing under an email would store rows the
    # runner never looks at — a scene that appears on the page and never fires,
    # which is the worst of both outcomes. So resolve to a sub before writing, and
    # refuse rather than guess if it cannot be resolved.
    owner = user_id
    if "@" in owner:
        resolved = _resolve_sub_for_email(owner)
        if not resolved:
            return response(400, {
                "error": f"could not resolve a Cognito sub for {owner}; scenes are "
                         f"stored by sub, so importing under an email would create "
                         f"rows the scenario runner never reads"})
        owner = resolved
    if not isinstance(scenes, list) or not scenes:
        return response(400, {"error": "scenes must be a non-empty array"})
    if len(scenes) > 50:
        return response(400, {"error": "at most 50 scenes per import"})

    now = datetime.now(timezone.utc).isoformat()
    scenarios_table = _scenarios_table()
    created, failed = [], []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            failed.append({"index": index, "error": "each scene must be an object"})
            continue
        try:
            built = sc.build_scenario(
                user_id=owner,
                name=scene.get("name", ""),
                trigger=scene.get("trigger") or {},
                actions=scene.get("deviceActions") or [],
                description=scene.get("description", ""),
                # Templates are library content owned by the catalog, not something
                # a user import should be able to mint.
                is_template=False,
                is_active=scene.get("isActive", True),
                now=now,
                source="import",
            )
        except sc.ScenarioError as exc:
            failed.append({"index": index, "name": scene.get("name", ""),
                           "error": str(exc)})
            continue
        item = built["item"]
        try:
            scenarios_table.put_item(Item=_decimalise(item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("scene import put failed: %s", exc)
            failed.append({"index": index, "name": item["name"],
                           "error": str(exc)[:200]})
            continue
        created.append({
            "scenarioId": item["scenarioId"],
            "name": item["name"],
            "trigger": sc.describe_trigger(item["trigger"]),
            "warnings": built.get("warnings") or [],
            "autoInferredFields": built.get("autoInferredFields") or {},
        })

    return response(200, {
        "created": created,
        "failed": failed,
        "createdCount": len(created),
        "failedCount": len(failed),
        # Said explicitly so nobody waits for an automation that has no schedule.
        "note": ("Imported scenes are stored but not scheduled. Run Reconcile "
                 "(sync-schedules) to create their EventBridge schedules."),
    })


def _decimalise(value):
    """Floats to Decimal, recursively — DynamoDB rejects Python floats.

    A latitude or a brightness that arrived as JSON is a float, and `put_item`
    fails with "Float types are not supported" on the way in. Converting via `str`
    rather than `Decimal(float)` avoids inheriting the float's binary noise
    (`Decimal(0.1)` is 0.1000000000000000055511151231257827).
    """
    from decimal import Decimal

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalise(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Browser sessions / workspace files (user-facing; chatbot polls these)
# ---------------------------------------------------------------------------

BROWSER_SESSIONS_TABLE_NAME = os.environ.get("BROWSER_SESSIONS_TABLE_NAME", "smarthome-browser-sessions")
CODE_SESSIONS_TABLE_NAME = os.environ.get("CODE_SESSIONS_TABLE_NAME", "smarthome-code-sessions")
FEEDBACK_TABLE_NAME = os.environ.get("FEEDBACK_TABLE_NAME", "smarthome-feedback")

# A 👎 reason is free text from an end user. Capped because it is rendered in the
# console and aggregated on the dashboard; DynamoDB's own 400 KB item limit is far
# too generous to be a useful guard here.
MAX_FEEDBACK_REASON_CHARS = 1000
# Enough to identify which turn was voted on, without turning the feedback table
# into a second copy of the conversation.
MAX_FEEDBACK_PROMPT_CHARS = 200
VALID_FEEDBACK_VOTES = ("up", "down")
VALID_FEEDBACK_SOURCES = ("user", "sim")


def _browser_sessions_table():
    return dynamodb.Table(BROWSER_SESSIONS_TABLE_NAME)


def _code_sessions_table():
    return dynamodb.Table(CODE_SESSIONS_TABLE_NAME)


def _feedback_table():
    return dynamodb.Table(FEEDBACK_TABLE_NAME)


def handle_chat_history(event):
    """GET /sessions?action=history&limit=20 — the CALLER's recent transcript.

    Reached before the check_admin gate: every user needs their own history, and
    behind the gate this would 403 for everyone but the admin.

    The actor is taken from the verified JWT claims, never from a query parameter.
    A `userId` parameter here would be an authorization bug wearing a convenience
    parameter's clothes — any signed-in user could read any other user's
    conversation, and it would look like a feature.

    Always 200 with whatever it could assemble. An empty transcript is the normal
    state for a new user, and a 500 on the login path would block the chat window
    over an optional convenience.
    """
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("claims", {}) or {})
    actor = claims.get("email") or claims.get("cognito:username") or claims.get("sub")
    if not actor:
        return response(401, {"error": "no verified identity on the request"})
    if not MEMORY_ID:
        return response(200, {"turns": [], "sessionsRead": 0,
                              "note": "MEMORY_ID is not configured"})

    try:
        limit = int((event.get("queryStringParameters") or {}).get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    # Bounded: this renders into a chat window and is fetched on every login.
    limit = max(1, min(limit, 100))

    actor_id = memory_actor.sanitize_actor_id(actor)
    try:
        session_ids = chat_history.sessions_newest_first(
            agentcore_client, MEMORY_ID, actor_id)
        turns, read = chat_history.recent_turns(
            agentcore_client, MEMORY_ID, actor_id, limit, session_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat history unavailable for %s: %s", actor_id, exc)
        return response(200, {"turns": [], "sessionsRead": 0, "error": str(exc)})

    return response(200, {
        "turns": turns,
        # How many logins this had to reach back through. A transcript stitched
        # from four sessions is not one conversation, and the UI says so.
        "sessionsRead": len(read),
        "actorId": actor_id,
    })


def handle_submit_feedback(event):
    """POST /sessions?action=feedback — record one thumbs up/down.

    Reached BEFORE the check_admin gate in _dispatch, because the whole point is
    that ordinary users vote. Behind the gate every non-admin vote would 403 and
    the UI would show nothing but an unresponsive button.

    The sort key puts `ts` first so the dashboard's per-day trend is a range
    query, and embeds `turnId` so re-voting the same turn overwrites rather than
    double-counting. A user who changes their mind must not move the average
    twice.
    """
    body = json.loads(event.get("body") or "{}")
    user_id = (body.get("userId") or "").strip()
    if not user_id:
        return response(400, {"error": "userId required"})
    guard = _require_self_or_admin(event, user_id)
    if guard:
        return guard

    vote = (body.get("vote") or "").strip()
    if vote not in VALID_FEEDBACK_VOTES:
        return response(400, {"error": f"vote must be one of {list(VALID_FEEDBACK_VOTES)}"})

    source = (body.get("source") or "user").strip()
    if source not in VALID_FEEDBACK_SOURCES:
        return response(400, {"error": f"source must be one of {list(VALID_FEEDBACK_SOURCES)}"})
    # Only an admin may attribute a vote to the simulator. Otherwise any client
    # could file its votes as `sim`, and the dashboard's "N% simulated" note —
    # the one thing that keeps the card honest — becomes unreliable.
    if source == "sim" and not check_admin(event):
        return response(403, {"error": "Forbidden: source=sim requires admin"})

    turn_id = (body.get("turnId") or "").strip()
    if not turn_id:
        return response(400, {"error": "turnId required"})
    session_id = (body.get("sessionId") or "").strip()

    reason = (body.get("reason") or "").strip()[:MAX_FEEDBACK_REASON_CHARS]
    turn_prompt = (body.get("turnPrompt") or "").strip()[:MAX_FEEDBACK_PROMPT_CHARS]

    # `agentDim` arrives from the chatbot's delegation trace: which specialists
    # this turn actually consulted. Stored so "which agent draws the 👎" is
    # answerable — a question mock data could never answer.
    agent_dim = body.get("agentDim") or []
    if isinstance(agent_dim, str):
        agent_dim = [agent_dim]
    agent_dim = [str(a)[:120] for a in agent_dim][:10]

    # A caller-supplied timestamp is accepted only from an admin, and only so
    # the simulator can lay votes across past days for a long-range demo. An
    # ordinary user's vote is always stamped server-side.
    ts = now_iso()
    supplied_ts = (body.get("ts") or "").strip()
    if supplied_ts and check_admin(event):
        ts = supplied_ts

    # Keyed by the TURN, not by the timestamp. Putting `ts` first looked
    # attractive (a per-day range query instead of a filter) but broke the thing
    # the key exists for: re-voting the same turn produced a second row, because
    # the server stamps a new `ts` each time. Verified live — one 👎 followed by
    # its reason wrote two rows and counted as two negatives.
    #
    # Aggregation filters on the `ts` attribute instead, which the dashboard
    # already does over a table holding one row per vote.
    item = {
        "userId": user_id,
        "feedbackKey": f"{session_id}#{turn_id}",
        "ts": ts,
        "vote": vote,
        "source": source,
        "turnId": turn_id,
    }
    if session_id:
        item["sessionId"] = session_id
    if reason:
        item["reason"] = reason
    if turn_prompt:
        item["turnPrompt"] = turn_prompt
    if agent_dim:
        item["agentDim"] = agent_dim

    _feedback_table().put_item(Item=item)
    logger.info("feedback recorded user=%s vote=%s source=%s agents=%s",
                user_id[:12], vote, source, agent_dim)
    return response(200, {"ok": True, "feedbackKey": item["feedbackKey"]})


def handle_browser_sessions_active(event):
    params = event.get("queryStringParameters") or {}
    user_id = params.get("userId", "")
    if not user_id:
        return response(400, {"error": "userId required"})
    guard = _require_self_or_admin(event, user_id)
    if guard:
        return guard

    t = _browser_sessions_table()
    # Return the row with the most recent startedAt (any status). The sort
    # key is sessionId (a ULID), not time, so we can't rely on DDB ordering;
    # fetch the last ~20 partitions and pick the highest startedAt client
    # side. ConsistentRead is required because the agent writes
    # `running` → `completed` within ~30s and eventually-consistent reads
    # were missing the brief running window entirely.
    resp = t.query(
        KeyConditionExpression=Key("userId").eq(user_id),
        Limit=20,
        ConsistentRead=True,
    )
    items = resp.get("Items", [])
    if not items:
        return response(200, {})
    items.sort(key=lambda i: i.get("startedAt", ""), reverse=True)
    return response(200, items[0])


def handle_code_sessions_active(event):
    """Return the most recent code-interpreter run for a user (any status).
    Same contract as handle_browser_sessions_active: the chatbot polls this
    while the agent is typing to render the CodeInterpreter tab live."""
    params = event.get("queryStringParameters") or {}
    user_id = params.get("userId", "")
    if not user_id:
        return response(400, {"error": "userId required"})
    guard = _require_self_or_admin(event, user_id)
    if guard:
        return guard

    t = _code_sessions_table()
    resp = t.query(
        KeyConditionExpression=Key("userId").eq(user_id),
        Limit=20,
        ConsistentRead=True,
    )
    items = resp.get("Items", [])
    if not items:
        return response(200, {})
    items.sort(key=lambda i: i.get("startedAt", ""), reverse=True)
    return response(200, items[0])


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def handler(event, context):
    """Entry point. Never lets an exception escape.

    An unhandled exception means API Gateway answers with its own 502, and that
    response carries NO CORS header — so a browser reports "blocked by CORS
    policy" and says nothing about the actual error. That misdirection cost real
    time once already: a KeyError in the Cedar policy rebuild presented as a CORS
    failure while the Admin Console showed the permission checkboxes as saved.

    Converting it here to a 500 WITH the CORS headers means the browser shows the
    real exception, and the page can display it instead of appearing to succeed.
    """
    try:
        return _dispatch(event, context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("unhandled error in %s %s",
                         event.get("httpMethod", ""), event.get("resource", ""))
        return response(500, {"error": type(exc).__name__, "message": str(exc)})


def _dispatch(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))

    # A scheduled invocation, which carries no HTTP envelope at all. Keyed on an
    # explicit field in the rule's constant input rather than on `source ==
    # "aws.events"`, so the trigger is greppable from here and a test can build the
    # event by hand.
    if event.get("task") == "a2a-sweep":
        return sweep_a2a_revocations(event)

    method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    # User-facing browser session lookup: chatbot polls this during an
    # agent turn. We piggyback on the existing GET /sessions route and
    # dispatch by action= because the admin Lambda's auto-generated resource
    # policy is already at the 20 KB API Gateway cap (same reason prompts
    # reuse /skills/{userId}/{skillName}). Workspace file browsing does NOT
    # route through this Lambda — per arch §9.10 the chatbot calls
    # InvokeAgentRuntimeCommand directly with Identity Pool credentials.
    if resource == "/sessions" and method == "GET":
        action = (event.get("queryStringParameters") or {}).get("action", "")
        if action == "browser-active":
            return handle_browser_sessions_active(event)
        if action == "code-active":
            return handle_code_sessions_active(event)
        # The caller's own recent transcript, for the chatbot to render on login.
        # Before the admin gate for the same reason as feedback: the people who
        # need it are ordinary users. Scoped to the CALLER's identity from the
        # verified JWT claims, never to a userId in the query string — that is the
        # difference between "show me my history" and "show me anyone's".
        if action == "history":
            return handle_chat_history(event)
        # Fall through to the admin-gated list_sessions below if no action=.

    # User feedback (thumbs up/down). Also before the gate, and for the same
    # reason: ordinary users are the ones voting. Piggybacks on /sessions rather
    # than taking a new API Gateway method — see the resource-policy note above.
    if resource == "/sessions" and method == "POST":
        action = (event.get("queryStringParameters") or {}).get("action", "")
        if action == "feedback":
            return handle_submit_feedback(event)
        return response(400, {"error": f"Unknown sessions action: {action}"})

    if not check_admin(event):
        return response(403, {"error": "Forbidden: admin group required"})

    # Overview ops dashboard. Admin-only (it aggregates cross-tenant cost and
    # error data), hence placed after the check_admin gate. Wrapped so a
    # boto3-level error still returns CORS headers — otherwise the browser
    # reports a generic CORS failure and masks the real cause (same reasoning
    # as the /optimization/* wrapper below).
    if resource == "/dashboard" and method == "GET":
        try:
            return dashboard.get_dashboard(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("Dashboard handler error")
            return response(500, {"error": type(e).__name__, "message": str(e)})

    # User & tool permission routes
    if resource == "/users" and method == "GET":
        return list_cognito_users(event)
    if resource == "/users" and method == "POST":
        body_obj = json.loads(event.get("body") or "{}")
        action = body_obj.get("action", "")
        if action == "create":
            return create_cognito_user(event)
        if action == "add-to-admin":
            return add_user_to_admin_group(event)
        if action == "remove-from-admin":
            return remove_user_from_admin_group(event)
        if action == "delete":
            return delete_cognito_user(event)
        return response(400, {"error": f"Unknown users action: {action}"})
    if resource == "/tools" and method == "GET":
        return list_gateway_tools(event)
    if resource == "/memories" and method == "GET":
        return list_memory_actors(event)
    if resource == "/memories/{actorId}" and method == "GET":
        return get_memory_records(event)
    if resource == "/users/{userId}/permissions" and method == "GET":
        # action=a2a → A2A grants + approved catalog; a2a-reconcile → intent vs the
        # Cognito groups that actually enforce it; else → legacy tool perms. All on
        # one resource because the admin Lambda's resource policy is near the 20 KB
        # cap (see cdk/lib/smarthome-stack.ts).
        action = (event.get("queryStringParameters") or {}).get("action")
        if action == "a2a":
            return get_user_a2a_permissions(event)
        if action == "a2a-reconcile":
            return reconcile_a2a_grants(event)
        return get_user_permissions(event)
    if resource == "/users/{userId}/permissions" and method == "PUT":
        action = (event.get("queryStringParameters") or {}).get("action")
        if action == "a2a":
            return update_user_a2a_permissions(event)
        if action == "a2a-reconcile":
            return repair_a2a_grants(event)
        return update_user_permissions(event)

    # Skill routes — also carry agent-prompt traffic on the {userId}/{skillName}
    # variant when skillName matches the reserved `__prompt_*__` sort key, so
    # the Agent Prompt tab doesn't need new API Gateway methods (the admin
    # Lambda's resource policy is already near the 20 KB cap).
    if resource == "/skills" and method == "GET":
        qs = event.get("queryStringParameters") or {}
        if qs.get("tenantEnv") == "1":
            import tenant_env
            if qs.get("userId"):
                return tenant_env.get_tenant_env(event)
            return tenant_env.list_tenant_envs(event)
        return list_skills(event)
    if resource == "/skills" and method == "POST":
        body_obj = json.loads(event.get("body") or "{}")
        if str(body_obj.get("skillName", "")).startswith("__prompt_"):
            return save_prompt_record(event)
        return create_skill(event)
    if resource == "/skills/users" and method == "GET":
        return list_users(event)
    if resource == "/skills/{userId}/{skillName}" and method == "GET":
        sk = (event.get("pathParameters") or {}).get("skillName", "")
        if sk == "__tenant_env__":
            import tenant_env
            return tenant_env.get_tenant_env({**event, "queryStringParameters": {
                "tenantEnv": "1",
                "userId": (event.get("pathParameters") or {}).get("userId", ""),
            }})
        if sk.startswith("__prompt_"):
            return get_prompt_record(event)
        return get_skill(event)
    if resource == "/skills/{userId}/{skillName}" and method == "PUT":
        sk = (event.get("pathParameters") or {}).get("skillName", "")
        if sk == "__tenant_env__":
            import tenant_env
            return tenant_env.put_tenant_env(event)
        if sk.startswith("__prompt_"):
            return save_prompt_record(event)
        return update_skill(event)
    if resource == "/skills/{userId}/{skillName}" and method == "DELETE":
        sk = (event.get("pathParameters") or {}).get("skillName", "")
        if sk == "__tenant_env__":
            import tenant_env
            return tenant_env.delete_tenant_env(event)
        if sk.startswith("__prompt_"):
            return delete_prompt_record(event)
        return delete_skill(event)
    if resource == "/skills/{userId}/{skillName}/files" and method == "GET":
        return list_skill_files(event)
    if resource == "/skills/{userId}/{skillName}/files" and method == "DELETE":
        return delete_skill_file(event)
    if resource == "/skills/{userId}/{skillName}/files/upload-url" and method == "POST":
        return get_upload_url(event)
    if resource == "/skills/{userId}/{skillName}/files/download-url" and method == "POST":
        return get_download_url(event)
    if resource == "/settings/{userId}" and method == "GET":
        # ?action=catalog rides this resource rather than getting a /models/catalog
        # of its own, for the reason spelled out at smarthome-stack.ts:770 — the
        # admin Lambda's auto-generated resource policy is near the 20 KB cap, so a
        # new API Gateway method is not free. The catalog ignores {userId}; the
        # Models page fetches it once per page load, not once per user row.
        if (event.get("queryStringParameters") or {}).get("action") == "catalog":
            return get_model_catalog(event)
        return get_settings(event)
    if resource == "/settings/{userId}" and method == "PUT":
        return update_settings(event)

    if resource == "/sessions" and method == "GET":
        return list_sessions(event)
    if resource == "/sessions/{sessionId}/stop" and method == "POST":
        return stop_session(event)

    # Registry routes
    if resource == "/registry/records" and method == "GET":
        # Consolidated dispatch — adding more resources here would push the
        # admin Lambda's resource policy past the 20KB cap.
        action = (event.get("queryStringParameters") or {}).get("action", "")
        if action == "a2a-list":
            return list_a2a_agents(event)
        if action == "a2a-conformance":
            return check_a2a_conformance(event)
        if action == "a2a-manifest":
            return get_a2a_manifest(event)
        if action == "a2a-gateway-reconcile":
            # GET reports; the POST branch below applies. Split so "show me what
            # would change" cannot create anything by accident.
            return reconcile_a2a_gateway_targets(event)
        if action == "a2a-grants":
            return list_a2a_grants_for_record(event)
        if action == "skill-list":
            return list_registry_skills(event)
        if action == "fleet":
            return list_agent_fleet(event)
        if action == "scenarios":
            return list_scenarios(event)
        if action == "sync-schedules":
            return sync_scenario_schedules(event)
        if action == "export-scenes":
            return export_scenarios(event)
        return list_registry_records(event)
    if resource == "/registry/records" and method == "POST":
        # Rides on the existing resource: the admin Lambda's auto-generated API
        # Gateway resource policy is near the 20 KB cap, so a new path is not free.
        #
        # The action check comes FIRST. Reconciling creates and deletes schedules,
        # so it belongs on POST rather than GET — and without this branch it fell
        # through to the skill reviewer, which rejected it with "recordId is
        # required": a confusing error for a button that has nothing to do with
        # records.
        action = (event.get("queryStringParameters") or {}).get("action", "")
        if action == "sync-schedules":
            return sync_scenario_schedules(event)
        if action == "import-scenes":
            return import_scenarios(event)
        # A mutation — it removes group memberships — so POST, not the GET block
        # where the other a2a- actions live. Exposed at all so an admin does not
        # have to wait out a schedule interval, and so the e2e suite can drive it.
        if action == "a2a-sweep":
            return sweep_a2a_revocations(event)
        # Also a mutation (it creates gateway targets), so POST. The GET route above
        # reports the same diff without applying it.
        if action == "a2a-gateway-reconcile":
            return reconcile_a2a_gateway_targets(event)
        return review_registry_record(event)
    if resource == "/registry/import" and method == "POST":
        return import_registry_records(event)

    # Knowledge Base routes (consolidated — action-based dispatch)
    if resource == "/knowledge-bases" and method == "GET":
        action = (event.get("queryStringParameters") or {}).get("action", "status")
        if action == "documents":
            return list_kb_documents(event)
        elif action == "sync-status":
            return get_kb_sync_status(event)
        else:
            return get_kb_status(event)
    if resource == "/knowledge-bases" and method == "POST":
        body = json.loads(event.get("body") or "{}")
        action = body.get("action", "")
        if action == "upload-url":
            return get_kb_upload_url(event)
        elif action == "delete":
            return delete_kb_document(event)
        elif action == "sync":
            return start_kb_sync(event)
        else:
            return response(400, {"error": f"Unknown KB action: {action}"})

    # Optimization routes — see docs/superpowers/specs/2026-05-14-agentcore-optimization-design.md.
    # Uses one wildcard lambda:InvokeFunction permission on /optimization/* (set in CDK)
    # so adding methods here doesn't grow the Lambda resource policy.
    #
    # Wrap dispatch in a try/except: optimization handlers throw on
    # boto3 ParamValidationError before they reach their own ClientError
    # branches, which yields a bare 500 with no CORS headers (browser
    # surfaces it as a generic CORS error, masking the real cause).
    if resource.startswith("/optimization/"):
        try:
            if resource == "/optimization/ab-toggle" and method == "GET":
                return optimization.get_ab_toggle(event)
            if resource == "/optimization/ab-toggle" and method == "PUT":
                return optimization.put_ab_toggle(event)
            if resource == "/optimization/recommendations" and method == "GET":
                return optimization.list_recommendations(event)
            if resource == "/optimization/recommendations" and method == "POST":
                return optimization.start_recommendation(event)
            if resource == "/optimization/recommendations/{recId}" and method == "GET":
                return optimization.get_recommendation(event)
            if resource == "/optimization/recommendations/{recId}" and method == "DELETE":
                return optimization.delete_recommendation(event)
            if resource == "/optimization/recommendations/{recId}/apply" and method == "POST":
                return optimization.apply_recommendation(event)
            if resource == "/optimization/bundles" and method == "GET":
                return optimization.list_bundles(event)
            if resource == "/optimization/bundles" and method == "POST":
                return optimization.create_bundle(event)
            if resource == "/optimization/bundles/{bundleArn}" and method == "GET":
                return optimization.get_bundle_versions(event)
            if resource == "/optimization/bundles/{bundleArn}" and method == "DELETE":
                return optimization.delete_bundle(event)
            if resource == "/optimization/ab-tests" and method == "GET":
                return optimization.list_ab_tests(event)
            if resource == "/optimization/ab-tests" and method == "POST":
                return optimization.start_ab_test(event)
            if resource == "/optimization/ab-tests/{testId}" and method == "GET":
                return optimization.get_ab_test(event)
            if resource == "/optimization/ab-tests/{testId}/stop" and method == "POST":
                return optimization.stop_ab_test(event)
        except Exception as e:  # noqa: BLE001 — surface error with CORS so browser shows it
            logger.exception("Optimization handler error")
            return optimization._resp(500, {"error": type(e).__name__, "message": str(e)})

    return response(400, {"error": f"Unknown route: {method} {resource}"})
