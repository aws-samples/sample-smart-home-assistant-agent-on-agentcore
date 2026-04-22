"""Skill ERP API Lambda — user-scoped CRUD for skill records in AgentCore Registry.

Each authenticated Cognito user can:
  - List their own skill records
  - Create a skill record (auto-submits to Registry for curator approval)
  - Update (for records that are still pending / rejected / approved — triggers
    a new approval if the content changed)
  - Delete one of their records

Records are stored in AgentCore Registry as `agentSkills` descriptors. Ownership
is tracked in DynamoDB (`userId=__erp_record_{recordId}__`, `skillName=<sub>`) so
a user cannot see or modify someone else's records. The registry's own ACL is
global per-registry, so we layer per-user ownership checks on top.
"""

import json
import os
import re
import logging
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
REGION = os.environ.get("AWS_REGION", "us-west-2")
REGISTRY_ID = os.environ.get("REGISTRY_ID", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)

# Strands SDK skill name pattern: lowercase alphanumeric + hyphens, 1-64 chars
SKILL_NAME_RE = re.compile(r"^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$")

OWNERSHIP_USER_PREFIX = "__erp_owner__"  # DynamoDB partition key for owner rows


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body),
    }


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_caller(event):
    """Return (sub, email) for the authenticated Cognito user."""
    claims = (event.get("requestContext", {}).get("authorizer", {}) or {}).get("claims", {}) or {}
    return claims.get("sub", ""), claims.get("email", "") or claims.get("cognito:username", "")


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _build_skill_md(name, description, instructions, allowed_tools, metadata):
    """Render a SKILL.md document with YAML frontmatter (Agent Skills spec)."""
    lines = ["---", f"name: {name}"]
    # Description is quoted to preserve punctuation
    safe_desc = description.replace('"', '\\"')
    lines.append(f'description: "{safe_desc}"')
    if allowed_tools:
        lines.append(f"allowed_tools: [{', '.join(allowed_tools)}]")
    if metadata:
        for k, v in metadata.items():
            safe_v = str(v).replace('"', '\\"')
            lines.append(f'x-{k}: "{safe_v}"')
    lines.append("---")
    lines.append("")
    lines.append(instructions or f"# {name}\n\n{description}")
    return "\n".join(lines)


def _build_skill_definition(license_name, compatibility):
    """Build the schemaVersion 0.1.0 skill definition JSON."""
    definition = {}
    meta = {}
    if license_name:
        meta["license"] = license_name
    if compatibility:
        meta["compatibility"] = compatibility
    if meta:
        definition["_meta"] = meta
    return json.dumps(definition) if definition else "{}"


def _record_to_owner_row(record_id, owner_sub):
    return {
        "userId": OWNERSHIP_USER_PREFIX,
        "skillName": record_id,
        "ownerSub": owner_sub,
        "updatedAt": now_iso(),
    }


def _load_owner_sub(record_id):
    resp = table.get_item(Key={"userId": OWNERSHIP_USER_PREFIX, "skillName": record_id})
    item = resp.get("Item")
    return item.get("ownerSub", "") if item else ""


def _extract_record_id_from_arn(arn):
    """recordArn format: arn:aws:bedrock-agentcore:<region>:<account>:registry/<regId>/record/<recordId>"""
    if not arn:
        return ""
    parts = arn.split("/")
    return parts[-1] if parts else ""


def _parse_skill_md(skill_md_text):
    """Extract description + instructions body from a SKILL.md string."""
    if not skill_md_text:
        return "", "", [], {}
    lines = skill_md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", skill_md_text, [], {}
    # Find closing --- of frontmatter
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
            # Expect [a, b, c]
            inner = value.strip("[]")
            allowed_tools = [t.strip() for t in inner.split(",") if t.strip()]
        elif key.startswith("x-"):
            metadata[key[2:]] = value
    return description, body, allowed_tools, metadata


def _fetch_record_detail(record_id):
    """GetRegistryRecord + extract user-facing fields from descriptors."""
    r = agentcore_control.get_registry_record(
        registryId=REGISTRY_ID, recordId=record_id
    )
    descriptors = r.get("descriptors", {}) or {}
    agent_skills = descriptors.get("agentSkills", {}) or {}
    skill_md = (agent_skills.get("skillMd") or {}).get("inlineContent", "")
    skill_def_raw = (agent_skills.get("skillDefinition") or {}).get("inlineContent", "")

    description, instructions, allowed_tools, metadata = _parse_skill_md(skill_md)
    license_name = ""
    compatibility = ""
    try:
        skill_def = json.loads(skill_def_raw) if skill_def_raw else {}
        meta = skill_def.get("_meta") or {}
        license_name = meta.get("license", "") or ""
        compatibility = meta.get("compatibility", "") or ""
    except Exception:
        pass

    return {
        "recordId": r.get("recordId", ""),
        "name": r.get("name", ""),
        "description": r.get("description") or description,
        "status": r.get("status", ""),
        "createdAt": _iso(r.get("createdAt")),
        "updatedAt": _iso(r.get("updatedAt")),
        "instructions": instructions,
        "allowedTools": allowed_tools,
        "license": license_name,
        "compatibility": compatibility,
        "metadata": metadata,
    }


def _iso(ts):
    if not ts:
        return ""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def list_my_records(event):
    caller_sub, _ = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    # Scan the DynamoDB ownership rows to find my records
    my_record_ids = []
    scan_params = {
        "FilterExpression": Attr("userId").eq(OWNERSHIP_USER_PREFIX) & Attr("ownerSub").eq(caller_sub),
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            my_record_ids.append(item["skillName"])
        if "LastEvaluatedKey" not in resp:
            break
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    records = []
    for rid in my_record_ids:
        try:
            records.append(_fetch_record_detail(rid))
        except agentcore_control.exceptions.ResourceNotFoundException:
            # Owner row is stale — registry record gone. Clean up and skip.
            try:
                table.delete_item(Key={"userId": OWNERSHIP_USER_PREFIX, "skillName": rid})
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to load record %s: %s", rid, e)

    records.sort(key=lambda r: r.get("updatedAt", ""), reverse=True)
    return response(200, {"records": records})


def get_my_record(event):
    caller_sub, _ = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})

    path_params = event.get("pathParameters") or {}
    record_id = path_params.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId is required"})

    owner = _load_owner_sub(record_id)
    if owner != caller_sub:
        return response(403, {"error": "You do not own this record"})

    try:
        return response(200, _fetch_record_detail(record_id))
    except agentcore_control.exceptions.ResourceNotFoundException:
        return response(404, {"error": f"Record '{record_id}' not found"})


def create_my_record(event):
    caller_sub, caller_email = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    body = json.loads(event.get("body") or "{}")
    skill_name = body.get("skillName", "")
    description = body.get("description", "")
    instructions = body.get("instructions", "")
    allowed_tools = body.get("allowedTools", []) or []
    license_name = body.get("license", "") or ""
    compatibility = body.get("compatibility", "") or ""
    metadata = body.get("metadata", {}) or {}

    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return response(400, {"error": f"Invalid skillName '{skill_name}'"})
    if not description:
        return response(400, {"error": "description is required"})
    if compatibility and len(compatibility) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if not isinstance(metadata, dict):
        return response(400, {"error": "metadata must be a key-value object"})
    if not isinstance(allowed_tools, list):
        return response(400, {"error": "allowedTools must be a list"})

    # Ownership watermark so the curator can see who published this
    owner_metadata = dict(metadata)
    owner_metadata.setdefault("submitted-by", caller_email or caller_sub)

    skill_md = _build_skill_md(
        name=skill_name,
        description=description,
        instructions=instructions,
        allowed_tools=allowed_tools,
        metadata=owner_metadata,
    )
    skill_def = _build_skill_definition(license_name, compatibility)

    # Record names must be unique within a registry. Suffix with a short random
    # hash on collision so two users can pick the same skill name.
    attempt_name = skill_name
    for attempt in range(3):
        try:
            create_resp = agentcore_control.create_registry_record(
                registryId=REGISTRY_ID,
                name=attempt_name,
                description=description,
                descriptorType="AGENT_SKILLS",
                descriptors={
                    "agentSkills": {
                        "skillMd": {"inlineContent": skill_md},
                        "skillDefinition": {
                            "schemaVersion": "0.1.0",
                            "inlineContent": skill_def,
                        },
                    }
                },
                recordVersion="0.1.0",
                clientToken=str(uuid.uuid4()),
            )
            break
        except agentcore_control.exceptions.ConflictException:
            attempt_name = f"{skill_name}-{uuid.uuid4().hex[:6]}"
            continue
    else:
        return response(409, {"error": "Record name collision; please retry"})

    record_arn = create_resp.get("recordArn", "")
    record_id = _extract_record_id_from_arn(record_arn)

    # Save ownership row
    table.put_item(Item=_record_to_owner_row(record_id, caller_sub))

    # Auto-submit for approval so the admin sees it in the pending queue.
    # SubmitRegistryRecordForApproval rejects records that are still in
    # CREATING state with a ConflictException that boto3 surfaces silently;
    # the record then lingers in DRAFT forever. Poll GetRegistryRecord until
    # the record leaves CREATING (usually <1s) before submitting.
    for _ in range(20):  # up to ~10s total
        try:
            rec = agentcore_control.get_registry_record(
                registryId=REGISTRY_ID, recordId=record_id
            )
            if rec.get("status") != "CREATING":
                break
        except Exception as e:
            logger.info("GetRegistryRecord during wait returned: %s", e)
        time.sleep(0.5)

    try:
        agentcore_control.submit_registry_record_for_approval(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except Exception as e:
        logger.warning("Submit-for-approval failed for %s: %s", record_id, e)

    return response(201, {
        "recordId": record_id,
        "recordArn": record_arn,
        "name": attempt_name,
    })


def update_my_record(event):
    caller_sub, caller_email = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    record_id = path_params.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId is required"})

    owner = _load_owner_sub(record_id)
    if owner != caller_sub:
        return response(403, {"error": "You do not own this record"})

    body = json.loads(event.get("body") or "{}")
    if "compatibility" in body and body["compatibility"] and len(body["compatibility"]) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if "metadata" in body and not isinstance(body["metadata"], dict):
        return response(400, {"error": "metadata must be a key-value object"})

    try:
        existing = agentcore_control.get_registry_record(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except agentcore_control.exceptions.ResourceNotFoundException:
        return response(404, {"error": f"Record '{record_id}' not found"})

    # Reuse existing SKILL.md to fill in missing fields
    current = _fetch_record_detail(record_id)
    name = existing.get("name", current["name"])
    description = body.get("description", current["description"])
    instructions = body.get("instructions", current.get("instructions", ""))
    allowed_tools = body.get("allowedTools", current.get("allowedTools", []))
    license_name = body.get("license", current.get("license", ""))
    compatibility = body.get("compatibility", current.get("compatibility", ""))
    metadata = body.get("metadata", current.get("metadata", {}))

    merged_metadata = dict(metadata or {})
    merged_metadata.setdefault("submitted-by", caller_email or caller_sub)

    skill_md = _build_skill_md(name, description, instructions, allowed_tools, merged_metadata)
    skill_def = _build_skill_definition(license_name, compatibility)

    agentcore_control.update_registry_record(
        registryId=REGISTRY_ID,
        recordId=record_id,
        description={"optionalValue": description},
        descriptors={
            "optionalValue": {
                "agentSkills": {
                    "skillMd": {"inlineContent": skill_md},
                    "skillDefinition": {
                        "schemaVersion": "0.1.0",
                        "inlineContent": skill_def,
                    },
                }
            }
        },
    )

    # Re-submit for approval (any edit resets the curator flow)
    try:
        agentcore_control.submit_registry_record_for_approval(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except Exception as e:
        logger.info("Re-submit-for-approval returned: %s", e)

    return response(200, {"message": f"Record '{record_id}' updated", "recordId": record_id})


def delete_my_record(event):
    caller_sub, _ = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    record_id = path_params.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId is required"})

    owner = _load_owner_sub(record_id)
    if owner != caller_sub:
        return response(403, {"error": "You do not own this record"})

    try:
        agentcore_control.delete_registry_record(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except agentcore_control.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        return response(500, {"error": f"Failed to delete record: {str(e)}"})

    table.delete_item(Key={"userId": OWNERSHIP_USER_PREFIX, "skillName": record_id})
    return response(200, {"message": f"Record '{record_id}' deleted"})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))

    method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    try:
        if resource == "/my-skills" and method == "GET":
            return list_my_records(event)
        if resource == "/my-skills" and method == "POST":
            return create_my_record(event)
        if resource == "/my-skills/{recordId}" and method == "GET":
            return get_my_record(event)
        if resource == "/my-skills/{recordId}" and method == "PUT":
            return update_my_record(event)
        if resource == "/my-skills/{recordId}" and method == "DELETE":
            return delete_my_record(event)
    except Exception as e:
        logger.exception("Unhandled error")
        return response(500, {"error": f"{type(e).__name__}: {str(e)}"})

    return response(400, {"error": f"Unknown route: {method} {resource}"})
