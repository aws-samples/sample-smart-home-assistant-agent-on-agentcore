"""Registry event notifications — the pure half.

AWS Agent Registry publishes record state transitions to the account's default
EventBridge bus (source `aws.agent-registry`). The event carries only two facts —
`registryRecordId` and `registryId` — so it cannot be shown to anyone as-is. The
handler in index.py enriches it with one GetRegistryRecord and stores the result;
this module decides what counts as an event, how it is keyed, and which stored rows
an admin still has to act on.

Pure on purpose, the same way `skill_scan` and `a2a_conformance` are: no boto3, so
every rule here runs in a unit test without credentials, and the console route and
the tests share one definition of "needs review".
"""

from __future__ import annotations

SOURCE = "aws.agent-registry"

# detail-type → the status the record just entered. Registry lifecycle events
# ("Registry Ready" and friends) are deliberately absent: there is one long-lived
# registry in this deployment and those events carry no action for an admin.
TRANSITIONS = {
    "Registry Record State changed to Draft": "DRAFT",
    "Registry Record State changed to Pending Approval": "PENDING_APPROVAL",
    "Registry Record State changed to Approved": "APPROVED",
    "Registry Record State changed to Rejected": "REJECTED",
    "Registry Record State changed to Deprecated": "DEPRECATED",
}

# The one transition that asks a human for something.
ACTIONABLE = "PENDING_APPROVAL"

# Where each transition sits in the Registry's approval workflow. Used only to break
# ties between events stamped in the same second (see `_order`).
_STATE_RANK = {"DRAFT": 0, "PENDING_APPROVAL": 1, "APPROVED": 2, "REJECTED": 2,
               "DEPRECATED": 3}


def parse_event(evt: dict) -> dict | None:
    """The record transition an EventBridge event describes, or None.

    None means "not ours": another source, a registry lifecycle event, or a detail
    block without the two ids. The caller logs and drops it; nothing here raises,
    because a malformed event must not make EventBridge retry it.
    """
    if not isinstance(evt, dict) or evt.get("source") != SOURCE:
        return None
    transition = TRANSITIONS.get(evt.get("detail-type", ""))
    if not transition:
        return None
    detail = evt.get("detail") or {}
    record_id = detail.get("registryRecordId", "")
    registry_id = detail.get("registryId", "")
    if not record_id or not registry_id:
        return None
    resources = evt.get("resources") or []
    return {
        "eventId": evt.get("id", ""),
        "detailType": evt["detail-type"],
        "transition": transition,
        "recordId": record_id,
        "registryId": registry_id,
        "recordArn": resources[0] if resources else "",
        "occurredAt": evt.get("time", ""),
    }


def event_key(occurred_at: str, event_id: str) -> str:
    """Sort key: ISO time first so a descending query is newest-first, then the
    EventBridge id so two events in the same second do not collide and a
    redelivered event overwrites itself instead of appearing twice."""
    return f"{occurred_at}#{event_id}"


def summarize_record(detail: dict) -> dict:
    """The display fields of a GetRegistryRecord response.

    GA made `name` the dedup key and `displayName` the label; preview-era records
    only have `name`, so it is the fallback. Every field defaults to "" rather than
    None so the row is safe to render and to store without an attribute-type dance.
    """
    detail = detail or {}
    return {
        "recordType": detail.get("recordType", "") or "",
        "name": detail.get("displayName") or detail.get("name", "") or "",
        "recordVersion": str(detail.get("recordVersion", "") or ""),
        "description": detail.get("description", "") or "",
        "statusReason": detail.get("statusReason", "") or "",
    }


def _order(row: dict) -> tuple:
    """Newest-last sort key for stored rows.

    EventBridge stamps `time` to the second, and the Skill ERP's create-and-submit
    emits a Draft and a Pending Approval inside the same second (measured
    2026-09-19). Sorting those by event id is a coin toss, and when the Draft wins
    the record's "latest" state reads as Draft and the submission is never
    highlighted. Within one second the only plausible sequence is the workflow's
    own order, so that breaks the tie; arrival time and the id come after, for
    determinism only.
    """
    occurred = row.get("occurredAt") or row.get("eventKey", "").split("#", 1)[0]
    return (
        occurred,
        _STATE_RANK.get(row.get("transition", ""), -1),
        row.get("receivedAt", ""),
        row.get("eventKey", ""),
    )


def fold(items: list[dict]) -> dict:
    """Mark which stored rows still need a decision.

    A record's current state is its newest event. A row is actionable only when it IS
    that newest event and the transition is Pending Approval — so an approval,
    rejection, deprecation or a fresh draft on the same record silently retires the
    highlight, and an author who resubmits twice produces one highlight, not two. No
    manual "mark read" exists; the Registry's own next transition is what clears it.
    """
    rows = sorted(items, key=_order, reverse=True)
    seen: set[str] = set()
    events = []
    pending = 0
    for row in rows:
        record_id = row.get("recordId", "")
        latest = record_id not in seen
        seen.add(record_id)
        actionable = latest and row.get("transition") == ACTIONABLE
        pending += actionable
        events.append({**row, "actionable": actionable})
    return {"events": events, "pendingCount": pending}
