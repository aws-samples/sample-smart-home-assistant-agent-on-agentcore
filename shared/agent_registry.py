"""AWS Agent Registry (GA) client and record shapes — one definition.

The service went GA on 2026-08-06 with a new namespace and a breaking API change.
The old `bedrock-agentcore` namespace stops serving Registry on **2026-09-17**;
after that, per the migration FAQ, "you lose read/write access to the service and
any remaining data in the old namespace". New accounts never had access to
Registry through the old namespace at all, so for a reference solution others
clone this was effectively already broken.

What changed, and why this module exists
----------------------------------------
The namespace change is mechanical but the API change is not, and both were
spread across eight files:

  - control-plane client: `bedrock-agentcore-control` -> `agent-registry-control`
  - IAM action prefix: `bedrock-agentcore:` -> `agent-registry:`
  - ARN service: `arn:aws:bedrock-agentcore:...` -> `arn:aws:agent-registry:...`
  - `descriptorType` (A2A / AGENT_SKILLS) is gone; there is a top-level
    `recordType` (AGENT / SKILL / MCP / CUSTOM) instead
  - `name` became `displayName`, and `name` is now the dedup key — unique within
    a registry, unique in combination with `recordVersion`
  - `inlineContent` -> `data`, `schemaVersion` -> `dataSchemaVersion`
  - `descriptors` went from a discriminated union to a flat keyed structure with
    exactly one primary descriptor, and the SKILL shape INVERTED: `skillMd` used
    to be a sibling of the definition and is now nested under
    `agentSkillsDefinition.additionalData`

Most Registry calls in this repo shared a boto3 client with Gateway, Runtime and
Policy calls, which do NOT move namespace. Renaming the client wholesale would
have broken those; every call site therefore needs the right client, which is what
`registry_client()` is for.

Everything here was read out of the botocore service model and then exercised
against the live GA service in a throwaway registry, because several constraints
appear in neither the docs nor the model:

  - the docs give the new endpoint as `agent-registry-control.{region}.api.aws`;
    boto3 resolves `agent-registry-control.{region}.amazonaws.com`. We pass no
    explicit endpoint_url and let boto3 resolve, so this cannot bite us.
  - `UpdateRegistryRecordStatus` requires `statusReason`. Omitting it is a
    ParamValidationError, not a service-side default.
  - **`CreateRegistryRecord` returns `recordArn`, not `recordId`.** Preview
    returned `recordId`. Code that keeps reading `resp["recordId"]` gets None and
    stores it, which then looks like a record that exists but cannot be found.
    Use `record_id_from_create()`.
  - **The AgentCard is validated against the real A2A schema.** A card missing
    `capabilities` / `defaultInputModes` / `securitySchemes` is rejected with
    "does not match any supported version" — a message that sounds like a
    versioning problem and is actually a completeness one.
  - **`skillMd` must begin with `---` frontmatter.** Plain markdown is rejected.
  - **The status machine is narrower than the enum suggests.** A new record is
    DRAFT, and the only transition out of DRAFT via UpdateRegistryRecordStatus is
    DEPRECATED. Approval goes DRAFT -> `SubmitRegistryRecordForApproval` ->
    PENDING_APPROVAL -> UpdateRegistryRecordStatus(APPROVED). Calling
    UpdateRegistryRecordStatus(APPROVED) on a DRAFT record fails with "Invalid
    status transition", and DEPRECATED is terminal.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

# The GA control-plane client. Both the control and data planes sign as
# `agent-registry`, hence the single IAM prefix below.
REGISTRY_CLIENT = "agent-registry-control"
REGISTRY_DATA_CLIENT = "agent-registry"

# IAM action prefix for every Registry action. The old `bedrock-agentcore:*`
# wildcard does NOT cover Registry after GA — a role relying on it loses Registry
# access silently, because the callers here treat a failed lookup as "no record".
IAM_PREFIX = "agent-registry"
ARN_SERVICE = "agent-registry"

# recordType values, from the model's enum.
RECORD_TYPE_AGENT = "AGENT"
RECORD_TYPE_SKILL = "SKILL"
RECORD_TYPE_MCP = "MCP"
RECORD_TYPE_CUSTOM = "CUSTOM"

# Status values the record state machine can hold (model enum), for callers that
# poll or filter.
STATUS_DRAFT = "DRAFT"
STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_DEPRECATED = "DEPRECATED"
STATUS_CREATING = "CREATING"
STATUS_UPDATING = "UPDATING"
STATUS_CREATE_FAILED = "CREATE_FAILED"
STATUS_UPDATE_FAILED = "UPDATE_FAILED"
SETTLED_STATUSES = frozenset({
    STATUS_DRAFT, STATUS_PENDING_APPROVAL, STATUS_APPROVED, STATUS_REJECTED,
    STATUS_DEPRECATED, STATUS_CREATE_FAILED, STATUS_UPDATE_FAILED,
})

# Status values a REGISTRY can hold. Deliberately separate from the record
# statuses above: they share no values, and conflating them is not a type error.
#
# A healthy registry is READY. There is no ACTIVE — `setup-agentcore.py` waited
# on "ACTIVE" for a while, which meant its wait loop could only ever time out,
# and a wait that always times out looks exactly like no wait at all. Names read
# out of the botocore model and confirmed against a live registry.
REGISTRY_STATUS_CREATING = "CREATING"
REGISTRY_STATUS_READY = "READY"
REGISTRY_STATUS_UPDATING = "UPDATING"
REGISTRY_STATUS_CREATE_FAILED = "CREATE_FAILED"
REGISTRY_STATUS_UPDATE_FAILED = "UPDATE_FAILED"
REGISTRY_STATUS_DELETING = "DELETING"
REGISTRY_STATUS_DELETE_FAILED = "DELETE_FAILED"
# The states a registry can still move out of on its own; anything else is final.
REGISTRY_PENDING_STATUSES = frozenset({
    REGISTRY_STATUS_CREATING, REGISTRY_STATUS_UPDATING,
})


# ---------------------------------------------------------------------------
# Grantability: may a record's skills be held by a user RIGHT NOW?
#
# One rule, two enforcement points, and they must not disagree.
#
#   - The sweep in the admin Lambda revokes `a2a-` Cognito GROUP memberships,
#     which is how per-user grants are enforced.
#   - The pre-token-generation trigger decides which groups to inject into
#     `cognito:groups`, which is how GLOBAL grants are enforced (they are not
#     memberships at all, so the sweep cannot see them).
#
# If the two used different rules, the same record in the same state would leave
# global users working and per-user users revoked — a split nobody would think to
# look for. Hence one function, here, next to the status names it reads.
#
# NOT used by the orchestrator. `tools/a2a.py` filters strictly on APPROVED when it
# builds the tool list, because "a non-approved agent must not be callable" is the
# requirement; the grace window below is about not CHURNING grants, not about
# widening what a model may call. During the window a user keeps the group while
# the orchestrator offers no tool for it.
# ---------------------------------------------------------------------------

# Statuses a record passes through on its way somewhere else. A record here has
# not been judged — it is mid-edit or waiting for a human.
IN_FLIGHT_STATUSES = frozenset({
    STATUS_DRAFT, STATUS_PENDING_APPROVAL, STATUS_CREATING, STATUS_UPDATING,
})

# How long an in-flight record keeps its grants. Sized for the fact that approval
# is a HUMAN step: `deploy.py` only submits for approval, and production approves
# in the AgentCore Registry console. Any edit to an APPROVED record resets it to
# DRAFT, so without a window every version bump would revoke every user's grants
# and then need a re-materialisation once someone clicked approve.
DEFAULT_GRANT_GRACE_SECONDS = 3600


def grant_grace_seconds(env: dict | None = None) -> int:
    """`A2A_GRANT_GRACE_SECONDS`, or the default. Never raises.

    A malformed value falls back rather than failing: this is read on the token
    path and in a scheduled sweep, and neither should break because someone typed
    a word into an env var. Negative is allowed and means "no window".
    """
    import os

    raw = (env if env is not None else os.environ).get("A2A_GRANT_GRACE_SECONDS")
    if raw in (None, ""):
        return DEFAULT_GRANT_GRACE_SECONDS
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_GRANT_GRACE_SECONDS


def _updated_at_epoch(record: dict) -> float | None:
    """`updatedAt` as a POSIX timestamp, or None if it cannot be read.

    boto3 returns a timezone-aware datetime; the console API layer re-serialises
    it as an ISO string. Both shapes arrive at this function depending on the
    caller, and a record whose timestamp cannot be read must not be treated as
    "changed just now" — that would keep a rejected record grantable forever.
    """
    value = record.get("updatedAt") or record.get("createdAt")
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        try:
            return value.timestamp()
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def grantable(record: dict | None, now: float, grace_seconds: int) -> tuple[bool, str]:
    """(may its skills be granted now, why). `record=None` means "no such record".

    Returns a reason string in both cases because every caller displays or logs
    one: an admin looking at why a user lost access to a specialist needs to be
    told "its record was rejected", not shown an empty column.

    APPROVED is grantable. REJECTED, DEPRECATED and a missing record are refused
    immediately — those are judgements, not transitions. In-flight statuses keep
    their grants for `grace_seconds` after the record last changed, then lose them.
    An unknown status is refused: a status this code has never heard of is not a
    licence to keep granting.

    In practice a deprecated record arrives here as `None` rather than as
    `status == DEPRECATED`: measured 2026-08-15, deprecating REMOVES the record from
    the API (`GetRegistryRecord` answers `ResourceNotFoundException`, and it lists
    under no status). The explicit DEPRECATED branch still stands for the window
    between the status change and the record disappearing.
    """
    if record is None:
        return False, "no such record in the registry"

    status = (record.get("status") or "").strip().upper()
    if status == STATUS_APPROVED:
        return True, "approved"
    if status not in IN_FLIGHT_STATUSES:
        # REJECTED / DEPRECATED / *_FAILED / anything unrecognised.
        return False, f"status is {status or 'unknown'}"

    updated = _updated_at_epoch(record)
    if updated is None:
        return False, f"status is {status} and its timestamp could not be read"
    age = now - updated
    if age <= grace_seconds:
        return True, (f"status is {status}, within the "
                      f"{grace_seconds}s re-approval window")
    return False, (f"status is {status} and it has been "
                   f"{int(age)}s, past the {grace_seconds}s window")


def grace_remaining_seconds(record: dict | None, now: float,
                            grace_seconds: int) -> int | None:
    """Seconds until an in-flight record loses its grants, or None.

    None for anything not on the clock — approved, already past the window, or
    unreadable. For the console, so an admin can see a revocation coming rather
    than discovering it.
    """
    if not record:
        return None
    status = (record.get("status") or "").strip().upper()
    if status not in IN_FLIGHT_STATUSES:
        return None
    updated = _updated_at_epoch(record)
    if updated is None:
        return None
    remaining = int(grace_seconds - (now - updated))
    return remaining if remaining > 0 else None


def registry_client(region: str | None = None):
    """A boto3 client for AWS Agent Registry's control plane.

    Use this for Registry calls ONLY. Gateway, Runtime, Identity, Policy and
    Memory stay on `bedrock-agentcore-control` — the namespace change is
    Registry-only, and workload identities and OAuth credential providers are
    deliberately left behind in the old namespace too.

    No explicit endpoint_url: boto3 resolves it, and its answer differs from the
    migration doc's (`.amazonaws.com` vs `.api.aws`). Trusting the SDK avoids
    betting on which one is right.
    """
    return boto3.client(
        REGISTRY_CLIENT,
        region_name=region or os.environ.get("AWS_REGION", "us-west-2"),
    )


def supports_ga(client=None) -> bool:
    """True when the installed boto3 knows the GA Registry service.

    Worth checking explicitly at deploy time: without it, `boto3.client(...)`
    raises `UnknownServiceError`, which reads like a code bug rather than "your
    boto3 is too old". Requires boto3 >= 1.43.67.
    """
    try:
        return REGISTRY_CLIENT in boto3.Session().get_available_services()
    except Exception:  # noqa: BLE001
        return False


def record_id_from_create(resp: dict) -> str:
    """The recordId out of a CreateRegistryRecord response.

    GA returns `recordArn` where preview returned `recordId`, so the id has to be
    parsed off the end of the ARN
    (`arn:aws:agent-registry:<region>:<acct>:registry/<rid>/record/<recordId>`).
    Reads `recordId` first so a preview-era response still works.
    """
    direct = resp.get("recordId")
    if direct:
        return direct
    arn = resp.get("recordArn", "")
    return arn.rsplit("/", 1)[-1] if arn else ""


class RecordNotApprovable(RuntimeError):
    """`approve_record` could not reach APPROVED, rather than quietly not trying."""


def approve_record(client, registry_id: str, record_id: str,
                   reason: str = "", timeout: int = 60) -> str:
    """Take a record to APPROVED, or raise `RecordNotApprovable`.

    Two calls, not one: a DRAFT record cannot be set APPROVED directly — the only
    UpdateRegistryRecordStatus transition out of DRAFT is DEPRECATED. It has to be
    submitted for approval first, which moves it to PENDING_APPROVAL, and only
    then can it be approved. Waits for each step to settle because the record sits
    in UPDATING in between and the next call would be rejected.

    DRAFT, PENDING_APPROVAL and REJECTED can all reach APPROVED. **DEPRECATED
    cannot — it is terminal.** Raising rather than returning the unchanged status,
    because the previous behaviour reported an approve on a deprecated record as a
    success.
    """
    import time

    def _wait() -> str:
        deadline = time.time() + timeout
        status = ""
        while time.time() < deadline:
            status = client.get_registry_record(
                registryId=registry_id, recordId=record_id).get("status", "")
            if status not in ("CREATING", "UPDATING"):
                return status
            time.sleep(2)
        return status

    status = _wait()
    if status == STATUS_APPROVED:
        return status
    if status == STATUS_DRAFT:
        client.submit_registry_record_for_approval(
            registryId=registry_id, recordId=record_id)
        status = _wait()
    # REJECTED goes straight to APPROVED — measured against a real record, and it
    # is what lets a reviewer change their mind without asking the author to
    # republish. Handled explicitly because this function previously fell through
    # for anything that was neither DRAFT nor PENDING_APPROVAL and RETURNED THAT
    # STATUS, so an approve on a rejected record reported "REJECTED" as though the
    # call had succeeded.
    if status in (STATUS_PENDING_APPROVAL, STATUS_REJECTED):
        set_record_status(client, registry_id, record_id, STATUS_APPROVED,
                          reason or "approved")
        status = _wait()
    if status != STATUS_APPROVED:
        # The same phantom-success bug the comment above describes, one status
        # later. DEPRECATED is TERMINAL — measured 2026-08-15:
        # `UpdateRegistryRecordStatus` answers "Cannot update registry record in
        # DEPRECATED status (terminal state)" for every target, including back to
        # APPROVED. Falling through and returning the unchanged status made an
        # approve on a deprecated record answer 200 with `status: DEPRECATED`, and
        # the caller had to notice the field to know nothing had happened.
        #
        # There is no recovery in place: the record must be recreated, which mints a
        # new recordId and therefore voids every grant keyed on the old one.
        raise RecordNotApprovable(
            f"record {record_id} is {status or 'in an unknown status'} and cannot be "
            f"approved" + (" — DEPRECATED is terminal, so it has to be recreated "
                           "(which mints a new recordId and voids grants keyed on "
                           "the old one)" if status == STATUS_DEPRECATED else ""))
    return status


def agent_record_descriptors(card: dict | str) -> dict:
    """`descriptors` for an AGENT record carrying an A2A AgentCard.

    Preview shape was
        {"a2a": {"agentCard": {"inlineContent": "<json>"}}}
    GA is one level shallower with a renamed field
        {"a2aAgentCard": {"data": "<json>"}}
    Reading the old path against a GA record yields None, and the A2A tool
    builder treats that as "card unavailable" and silently drops the tool.
    """
    data = card if isinstance(card, str) else json.dumps(card)
    return {"a2aAgentCard": {"data": data}}


def ensure_frontmatter(skill_md: str, name: str = "", description: str = "") -> str:
    """Give a skill's markdown the `---` frontmatter block GA requires.

    The service rejects a `skillMd` that does not start with `---`
    ("data must start with frontmatter delimited by '---'"), which is not in the
    docs or the model. Markdown that already has it is returned untouched.
    """
    text = (skill_md or "").strip()
    if text.startswith("---"):
        return skill_md
    lines = ["---"]
    if name:
        lines.append(f"name: {name}")
    if description:
        # Single line: a stray newline would end the frontmatter block early.
        lines.append(f"description: {description.replace(chr(10), ' ').strip()}")
    lines.append("---")
    return "\n".join(lines) + ("\n\n" + text if text else "\n")


def skill_record_descriptors(skill_definition: dict | str,
                             skill_md: str | None = None,
                             schema_version: str = "0.1.0",
                             name: str = "", description: str = "") -> dict:
    """`descriptors` for a SKILL record.

    Note the inversion: in preview, `skillDefinition` and `skillMd` were siblings.
    In GA the definition is the parent and the markdown moves into
    `additionalData.skillMd`. Renaming preview's fields in place produces a record
    the API rejects.

    The markdown gets frontmatter added when it lacks it — see
    `ensure_frontmatter`.
    """
    data = (skill_definition if isinstance(skill_definition, str)
            else json.dumps(skill_definition))
    descriptor: dict[str, Any] = {
        "data": data,
        "dataSchemaVersion": schema_version,
    }
    if skill_md:
        descriptor["additionalData"] = {
            "skillMd": {
                "data": ensure_frontmatter(skill_md, name, description),
                "dataSchemaVersion": schema_version,
            },
        }
    return {"agentSkillsDefinition": descriptor}


def as_update_descriptors(descriptors: dict) -> dict:
    """Rewrite a Create-shaped descriptor tree into UpdateRegistryRecord's shape.

    `CreateRegistryRecord` takes descriptors bare. `UpdateRegistryRecord` wraps
    EVERY level in `optionalValue` — the union, each descriptor, and each field:

        create  {"a2aAgentCard": {"data": "<json>"}}
        update  {"optionalValue": {"a2aAgentCard": {"optionalValue":
                    {"data": {"optionalValue": "<json>"}}}}}

    Measured from the botocore service model rather than guessed:

        c = boto3.client("agent-registry-control")
        c.meta.service_model.operation_model("UpdateRegistryRecord") \\
         .input_shape.members["descriptors"]                     # -> optionalValue
         ...members["optionalValue"].members["a2aAgentCard"]     # -> optionalValue
         ...members["optionalValue"].members["data"]             # -> optionalValue

    Why a function rather than wrapping by hand at each call site: wrapping only
    the outer level is a fix that LOOKS right and is not. `a2a-agent-registry/
    deploy.py` carries the same logic (it cannot import this module — the deploy
    scripts run from their own directory), and the two are held identical by
    shared/tests/test_update_descriptor_shape.py. Getting it wrong there caused a
    redeploy to fall back to delete-and-recreate, minting a NEW recordId while
    reporting success — and `a2aGrants` is keyed by recordId, so every specialist
    granted yesterday had no tools today, with nothing in any log.

    A partially-wrapped payload does not fail cleanly everywhere, either: passing
    `{"optionalValue": <bare tree>}` to a SKILL update raises ParamValidationError
    naming the inner fields, which is at least loud. Passing a bare value where one
    is optional can be accepted and ignored.
    """
    def wrap(value, depth: int):
        if isinstance(value, dict):
            return {"optionalValue": {k: wrap(v, depth + 1) for k, v in value.items()}}
        return {"optionalValue": value}

    return {"optionalValue": {
        name: wrap(fields, 0) for name, fields in descriptors.items()
    }}


def read_agent_card(record: dict) -> str:
    """Pull the AgentCard JSON string out of a GA record.

    Falls back to the preview path so a record created before the migration still
    reads. Returns "" when neither is present — callers already treat an empty
    card as "skip this record".
    """
    descriptors = record.get("descriptors") or {}
    ga = (descriptors.get("a2aAgentCard") or {}).get("data")
    if ga:
        return ga
    preview = ((descriptors.get("a2a") or {}).get("agentCard") or {}).get("inlineContent")
    return preview or ""


def read_skill_definition(record: dict) -> tuple[str, str]:
    """Return (definition_json, skill_md) from a GA record, preview as fallback."""
    descriptors = record.get("descriptors") or {}
    ga = descriptors.get("agentSkillsDefinition") or {}
    if ga:
        md = ((ga.get("additionalData") or {}).get("skillMd") or {}).get("data", "")
        return ga.get("data", ""), md
    preview = descriptors.get("agentSkills") or {}
    definition = (preview.get("skillDefinition") or {}).get("inlineContent", "")
    md = (preview.get("skillMd") or {}).get("inlineContent", "")
    return definition, md


def dedup_name(*parts: str) -> str:
    """Build a `name` — the GA dedup key, unique within a registry.

    Distinct from `displayName`, which is what a human reads and which preview
    called `name`. Keeping them separate is what lets a record be renamed for
    display without creating a second record.
    """
    joined = "-".join(p.strip() for p in parts if p and p.strip())
    return joined or "record"


def list_records(client, registry_id: str, record_type: str | None = None,
                 status: str | None = None) -> list[dict]:
    """List a registry's records, paginating, with the GA `filters` shape.

    GA replaced ad-hoc query parameters with a list of
    {"name": <field>, "values": [...]} where field is one of name / status /
    recordType. `descriptorType` is gone entirely.
    """
    filters = []
    if record_type:
        filters.append({"name": "recordType", "values": [record_type]})
    if status:
        filters.append({"name": "status", "values": [status]})

    out: list[dict] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"registryId": registry_id}
        if filters:
            kwargs["filters"] = filters
        if token:
            kwargs["nextToken"] = token
        resp = client.list_registry_records(**kwargs)
        out.extend(resp.get("registryRecords") or resp.get("records") or [])
        token = resp.get("nextToken")
        if not token:
            return out


def set_record_status(client, registry_id: str, record_id: str, status: str,
                      reason: str = "") -> dict:
    """Update a record's status.

    `statusReason` is required by the service model even though the docs read as
    though it is optional, so a default is supplied rather than letting the call
    fail on a missing parameter.
    """
    return client.update_registry_record_status(
        registryId=registry_id,
        recordId=record_id,
        status=status,
        statusReason=reason or f"set to {status}",
    )
