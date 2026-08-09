"""Skill ERP API Lambda — user-scoped CRUD for records in AWS Agent Registry.

Each authenticated Cognito user can:
  - List their own skill records
  - Create a skill record (auto-submits to Registry for curator approval)
  - Update (for records that are still pending / rejected / approved — triggers
    a new approval if the content changed)
  - Delete one of their records

Records live in AWS Agent Registry: skills as SKILL records carrying an
`agentSkillsDefinition` descriptor, A2A agents as AGENT records carrying an
`a2aAgentCard`. Ownership is tracked in DynamoDB
(`userId=__erp_record_{recordId}__`, `skillName=<sub>`) so a user cannot see or
modify someone else's records. The registry's own ACL is global per-registry, so
we layer per-user ownership checks on top.
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

from a2a_helpers import (
    A2A_NAME_RE,
    build_card_definition,
    parse_card_definition,
    validate_form as validate_a2a_form,
)

# Copied in beside this file at build time by scripts/01-install-deps.sh, the same
# way device_catalog.py reaches the IoT Lambdas — CDK packages each Lambda with
# Code.fromAsset(<dir>), so a module outside the directory is not deployed.
import agent_registry as registry_ns

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
REGION = os.environ.get("AWS_REGION", "us-west-2")
REGISTRY_ID = os.environ.get("REGISTRY_ID", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# AWS Agent Registry GA namespace. Every AgentCore call in this file is a Registry
# call, so the whole module moves — unlike admin-api, which mixes Registry with
# Gateway and Policy calls that stay on `bedrock-agentcore-control`.
#
# The old namespace stops serving Registry on 2026-09-17, and a brand-new account
# never had Registry access through it at all, so a fresh deploy of this reference
# solution would already fail at create_registry on the old client.
#
# The variable keeps its name so the ~20 call sites below read unchanged; only the
# service it points at differs.
agentcore_control = boto3.client(registry_ns.REGISTRY_CLIENT, region_name=REGION)

# Strands SDK skill name pattern: lowercase alphanumeric + hyphens, 1-64 chars
SKILL_NAME_RE = re.compile(r"^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$")

OWNERSHIP_USER_PREFIX = "__erp_owner__"  # DynamoDB partition key for owner rows

A2A_SK_PREFIX = "a2a:"  # Sort-key prefix for A2A ownership rows


def _a2a_owner_key(record_id):
    return {"userId": OWNERSHIP_USER_PREFIX, "skillName": f"{A2A_SK_PREFIX}{record_id}"}


def _iso_or_empty(ts):
    if not ts:
        return ""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


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
    """recordArn format, GA namespace:
    arn:aws:agent-registry:<region>:<account>:registry/<regId>/record/<recordId>

    Parsing the last segment works for both namespaces, so this needed no change
    beyond the comment — but the ARN service really did change at GA.
    """
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
    # GA: descriptors.agentSkillsDefinition.data plus
    # .additionalData.skillMd.data. The helper falls back to the preview paths so a
    # record written before the migration still renders in the UI.
    skill_def_raw, skill_md = registry_ns.read_skill_definition(r)

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
# Shared create/submit helpers (reused by Skills and A2A routes)
# ---------------------------------------------------------------------------

def _create_record_with_collision_fallback(
    name, record_type, descriptors, description, record_version="0.1.0", max_attempts=3
):
    """Call CreateRegistryRecord; on ConflictException retry with a 6-hex suffix.

    Returns (recordArn, final_name). Raises ValueError if all attempts collide.

    `record_type` replaces the preview `descriptor_type`: GA dropped
    `descriptorType` for a top-level `recordType` (AGENT / SKILL / MCP / CUSTOM),
    and the descriptor key now has to agree with it.

    `name` is the GA dedup key — unique within the registry, and unique in
    combination with `recordVersion` — which is why the collision retry matters
    more than it did in preview, where `name` was just a label. `displayName`
    carries the human-readable value that `name` used to hold.
    """
    attempt_name = name
    for attempt in range(max_attempts):
        try:
            resp = agentcore_control.create_registry_record(
                registryId=REGISTRY_ID,
                name=attempt_name,
                displayName=name,
                description=description,
                recordType=record_type,
                descriptors=descriptors,
                recordVersion=record_version,
                clientToken=str(uuid.uuid4()),
            )
            return resp.get("recordArn", ""), attempt_name
        except agentcore_control.exceptions.ConflictException:
            attempt_name = f"{name}-{uuid.uuid4().hex[:6]}"
            continue
    raise ValueError("record name collision after retries")


def _poll_until_out_of_creating(record_id, timeout_seconds=10):
    """Poll GetRegistryRecord until status leaves CREATING or timeout. Best-effort."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            rec = agentcore_control.get_registry_record(
                registryId=REGISTRY_ID, recordId=record_id
            )
            if rec.get("status") != "CREATING":
                return
        except Exception as e:
            logger.info("GetRegistryRecord during wait returned: %s", e)
        time.sleep(0.5)


def _submit_for_approval(record_id):
    """Submit record for curator approval.

    Deliberately does NOT raise: by the time this runs the record exists and its
    ownership row is committed, so a 5xx here would tell the user their skill
    failed to save and invite a retry that creates a duplicate record.

    It does report, though. Returning the failure lets the caller put it in the
    201 body — silently swallowing it left the record parked in DRAFT while the
    UI claimed success, so the user waited for an approval that was never
    coming. Returns None on success, or the error string to surface.
    """
    try:
        agentcore_control.submit_registry_record_for_approval(
            registryId=REGISTRY_ID, recordId=record_id
        )
        return None
    except Exception as e:
        logger.warning("Submit-for-approval failed for %s: %s", record_id, e)
        return str(e)


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
    #
    # The GA SKILL descriptor INVERTED the preview shape: `skillMd` and
    # `skillDefinition` used to be siblings under `agentSkills`; now the definition
    # is the parent (`agentSkillsDefinition`) and the markdown hangs off its
    # `additionalData`. The helper also prepends the `---` frontmatter block GA
    # requires of `skillMd` — plain markdown is rejected, and that constraint is in
    # neither the docs nor the API model.
    descriptors = registry_ns.skill_record_descriptors(
        skill_def, skill_md=skill_md, name=skill_name, description=description)
    try:
        record_arn, attempt_name = _create_record_with_collision_fallback(
            name=skill_name,
            record_type=registry_ns.RECORD_TYPE_SKILL,
            descriptors=descriptors,
            description=description,
        )
    except ValueError:
        return response(409, {"error": "Record name collision; please retry"})

    record_id = _extract_record_id_from_arn(record_arn)

    # Save ownership row
    table.put_item(Item=_record_to_owner_row(record_id, caller_sub))

    # Auto-submit for approval so the admin sees it in the pending queue.
    # Poll GetRegistryRecord until the record leaves CREATING (usually <1s)
    # before submitting, otherwise SubmitRegistryRecordForApproval fails silently.
    _poll_until_out_of_creating(record_id)
    submit_error = _submit_for_approval(record_id)

    body = {
        "recordId": record_id,
        "recordArn": record_arn,
        "name": attempt_name,
    }
    if submit_error:
        body["submitWarning"] = submit_error
    return response(201, body)


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

    # No `optionalValue` wrapper. The two update call sites in this repo disagreed
    # about it — this one wrapped, a2a-agent-registry/deploy.py did not — so the
    # shape was settled by reading the GA service model: `description` is a plain
    # string and `descriptors` a plain structure, with no wrapper anywhere.
    agentcore_control.update_registry_record(
        registryId=REGISTRY_ID,
        recordId=record_id,
        description=description,
        descriptors=registry_ns.skill_record_descriptors(
            skill_def, skill_md=skill_md, name=name, description=description),
    )

    # Re-submit for approval (any edit resets the curator flow)
    submit_error = _submit_for_approval(record_id)

    body = {"message": f"Record '{record_id}' updated", "recordId": record_id}
    if submit_error:
        body["submitWarning"] = submit_error
    return response(200, body)


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
# A2A handlers
# ---------------------------------------------------------------------------

def _a2a_fetch_detail(record_id):
    r = agentcore_control.get_registry_record(registryId=REGISTRY_ID, recordId=record_id)
    # GA: descriptors.a2aAgentCard.data — one level shallower than preview's
    # descriptors.a2a.agentCard.inlineContent. The helper falls back to the preview
    # path so records written before the migration still render.
    card_def_raw = registry_ns.read_agent_card(r)
    form = parse_card_definition(card_def_raw)
    return {
        "recordId": r.get("recordId", ""),
        "name": r.get("name", "") or form.get("name", ""),
        "description": r.get("description", "") or form.get("description", ""),
        "status": r.get("status", ""),
        "createdAt": _iso_or_empty(r.get("createdAt")),
        "updatedAt": _iso_or_empty(r.get("updatedAt")),
        "card": form,
    }


def list_my_a2a(event):
    caller_sub, _ = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    my_record_ids = []
    scan_params = {
        "FilterExpression": (
            Attr("userId").eq(OWNERSHIP_USER_PREFIX)
            & Attr("ownerSub").eq(caller_sub)
            & Attr("recordType").eq("a2a")
        ),
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            sk = item.get("skillName", "")
            if sk.startswith(A2A_SK_PREFIX):
                my_record_ids.append(sk[len(A2A_SK_PREFIX):])
        if "LastEvaluatedKey" not in resp:
            break
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    records = []
    for rid in my_record_ids:
        try:
            records.append(_a2a_fetch_detail(rid))
        except agentcore_control.exceptions.ResourceNotFoundException:
            try:
                table.delete_item(Key=_a2a_owner_key(rid))
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to load A2A record %s: %s", rid, e)

    records.sort(key=lambda r: r.get("updatedAt", ""), reverse=True)
    return response(200, {"records": records})


def create_my_a2a(event):
    caller_sub, caller_email = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    form = json.loads(event.get("body") or "{}")
    ok, err = validate_a2a_form(form)
    if not ok:
        return response(400, {"error": err})

    # GA flattened this: preview was descriptors.a2a.agentCard.inlineContent,
    # GA is descriptors.a2aAgentCard.data. No dataSchemaVersion — the card's own
    # protocolVersion (from build_card_definition) is what the service validates
    # against, and it validates the WHOLE A2A schema: a card missing capabilities
    # or securitySchemes is rejected as "does not match any supported version",
    # which sounds like a version problem and is a completeness one.
    descriptors = registry_ns.agent_record_descriptors(build_card_definition(form))
    try:
        record_arn, attempt_name = _create_record_with_collision_fallback(
            name=form["name"],
            record_type=registry_ns.RECORD_TYPE_AGENT,
            descriptors=descriptors,
            description=form["description"],
        )
    except ValueError:
        return response(409, {"error": "Record name collision; please retry"})

    record_id = _extract_record_id_from_arn(record_arn)

    table.put_item(Item={
        **_a2a_owner_key(record_id),
        "recordType": "a2a",
        "ownerSub": caller_sub,
        "ownerEmail": caller_email,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    })

    _poll_until_out_of_creating(record_id)
    submit_error = _submit_for_approval(record_id)

    body = {
        "recordId": record_id,
        "recordArn": record_arn,
        "name": attempt_name,
    }
    if submit_error:
        body["submitWarning"] = submit_error
    return response(201, body)


def update_my_a2a(event):
    caller_sub, caller_email = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    record_id = path_params.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId is required"})

    owner_row = table.get_item(Key=_a2a_owner_key(record_id)).get("Item") or {}
    if owner_row.get("ownerSub") != caller_sub:
        return response(403, {"error": "You do not own this record"})

    form = json.loads(event.get("body") or "{}")
    ok, err = validate_a2a_form(form)
    if not ok:
        return response(400, {"error": err})

    try:
        existing = agentcore_control.get_registry_record(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except agentcore_control.exceptions.ResourceNotFoundException:
        return response(404, {"error": f"Record '{record_id}' not found"})

    # Force the form's name to match the existing record — the Registry does not
    # allow renaming records, and the UI disables the name field on edit.
    form["name"] = existing.get("name", form.get("name", ""))

    # Plain values, no `optionalValue` wrapper — see the note on the skill update
    # above for why that wrapper is gone.
    agentcore_control.update_registry_record(
        registryId=REGISTRY_ID,
        recordId=record_id,
        description=form["description"],
        descriptors=registry_ns.agent_record_descriptors(
            build_card_definition(form)),
    )
    table.update_item(
        Key=_a2a_owner_key(record_id),
        UpdateExpression="SET updatedAt = :t, ownerEmail = :e",
        ExpressionAttributeValues={":t": now_iso(), ":e": caller_email},
    )
    submit_error = _submit_for_approval(record_id)
    body = {"message": f"A2A record '{record_id}' updated", "recordId": record_id}
    if submit_error:
        body["submitWarning"] = submit_error
    return response(200, body)


def delete_my_a2a(event):
    caller_sub, _ = get_caller(event)
    if not caller_sub:
        return response(401, {"error": "Unauthorized"})
    if not REGISTRY_ID:
        return response(500, {"error": "REGISTRY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    record_id = path_params.get("recordId", "")
    if not record_id:
        return response(400, {"error": "recordId is required"})

    owner_row = table.get_item(Key=_a2a_owner_key(record_id)).get("Item") or {}
    if owner_row.get("ownerSub") != caller_sub:
        return response(403, {"error": "You do not own this record"})

    try:
        agentcore_control.delete_registry_record(
            registryId=REGISTRY_ID, recordId=record_id
        )
    except agentcore_control.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        return response(500, {"error": f"Failed to delete record: {str(e)}"})

    table.delete_item(Key=_a2a_owner_key(record_id))
    return response(200, {"message": f"A2A record '{record_id}' deleted"})


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
        if resource == "/my-a2a-agents" and method == "GET":
            return list_my_a2a(event)
        if resource == "/my-a2a-agents" and method == "POST":
            return create_my_a2a(event)
        if resource == "/my-a2a-agents/{recordId}" and method == "GET":
            # Single-record GET: same shape as list item
            path_params = event.get("pathParameters") or {}
            record_id = path_params.get("recordId", "")
            caller_sub, _ = get_caller(event)
            if not caller_sub:
                return response(401, {"error": "Unauthorized"})
            owner_row = table.get_item(Key=_a2a_owner_key(record_id)).get("Item") or {}
            if owner_row.get("ownerSub") != caller_sub:
                return response(403, {"error": "You do not own this record"})
            try:
                return response(200, _a2a_fetch_detail(record_id))
            except agentcore_control.exceptions.ResourceNotFoundException:
                return response(404, {"error": "not found"})
        if resource == "/my-a2a-agents/{recordId}" and method == "PUT":
            return update_my_a2a(event)
        if resource == "/my-a2a-agents/{recordId}" and method == "DELETE":
            return delete_my_a2a(event)
    except Exception as e:
        logger.exception("Unhandled error")
        return response(500, {"error": f"{type(e).__name__}: {str(e)}"})

    return response(400, {"error": f"Unknown route: {method} {resource}"})
