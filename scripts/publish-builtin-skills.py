#!/usr/bin/env python3
"""Publish the built-in skills to AWS Agent Registry as approved SKILL records.

Why this exists. `Admin Console → Integration Registry → Skills` lists every
APPROVED SKILL record in the registry and marks the ones nobody has imported. That
page was correct and almost empty: the registry held exactly ONE SKILL record
(`nightly-air-check`, published through the Skill ERP), while the nine built-in
skills under `agent/skills/` went straight into DynamoDB by `seed-skills.py` and
were never published at all. So the page answered "what does this registry hold"
truthfully and looked broken.

The two stores mean different things and both are wanted:

  DynamoDB `smarthome-skills`   what the agent LOADS at invocation time
  Agent Registry SKILL records  what a curator can SEE, review and approve

This script fills the second from the same files that fill the first, so the
overview matches what is actually deployed.

Idempotent by dedup name: `name` is the GA dedup key and is unique per registry,
so a re-run finds the existing record and updates its descriptors rather than
creating a second copy. That matters because the record id is what
`importedFromRegistry` rows point at — churning ids would orphan every import.

Records are published and then APPROVED, because an unapproved record does not
appear on the page at all, and a script whose output is invisible reads as a
script that did not run.

Usage:
    ./venv/bin/python scripts/publish-builtin-skills.py
    ./venv/bin/python scripts/publish-builtin-skills.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import boto3
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from shared import agent_registry as registry_ns  # noqa: E402

SKILLS_DIR = ROOT / "agent" / "skills"
STATE_FILE = ROOT / "agentcore-state.json"
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))

# Marks a record as published by this script rather than by a human through the
# Skill ERP. Without it a curator cannot tell a built-in from a submission, and
# the honest answer to "who published this" is "the deploy did".
PUBLISHER = "publish-builtin-skills.py"


def parse_skill_md(path: Path) -> tuple[dict, str]:
    """(frontmatter, body) from a SKILL.md. Mirrors seed-skills.py exactly.

    Deliberately the same parse as the DynamoDB seeder rather than a second
    implementation: the point of this script is that the two stores describe the
    same skills, and two parsers is how they stop doing that.
    """
    content = path.read_text(encoding="utf-8").strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"invalid SKILL.md format in {path}")
    return yaml.safe_load(parts[1]) or {}, parts[2].strip()


def discover_skills() -> list[dict]:
    """Every built-in skill on disk, as {name, description, body, path}."""
    out = []
    for entry in sorted(SKILLS_DIR.iterdir()):
        md = entry / "SKILL.md"
        if not entry.is_dir() or not md.is_file():
            continue
        try:
            front, body = parse_skill_md(md)
        except Exception as exc:  # noqa: BLE001
            # Loud, and keep going: one malformed skill must not stop the other
            # eight from becoming visible.
            print(f"  ! {entry.name}: could not parse SKILL.md ({exc}) — skipped")
            continue
        out.append({
            "dir": entry.name,
            "name": front.get("name") or entry.name,
            "description": front.get("description", ""),
            "body": body,
            "front": front,
        })
    return out


def existing_skill_records(client, registry_id: str) -> dict[str, dict]:
    """{dedup name: record} for every SKILL record, at ANY status.

    Any status, not just APPROVED: a record left in DRAFT or REJECTED by an
    earlier run still occupies its dedup name, so filtering to APPROVED here
    would make this script try to create a duplicate and fail on conflict
    forever.
    """
    records = registry_ns.list_records(
        client, registry_id, record_type=registry_ns.RECORD_TYPE_SKILL)
    return {r.get("name", ""): r for r in records if r.get("name")}


def publish(client, registry_id: str, skill: dict, existing: dict, dry_run: bool):
    """Create or update one SKILL record, then approve it. Returns a status word."""
    dedup = registry_ns.dedup_name("builtin", skill["dir"])
    definition = {
        "name": skill["name"],
        "description": skill["description"],
        "_meta": {
            "license": "MIT-0",
            "compatibility": "smarthome-orchestrator",
            "publishedBy": PUBLISHER,
            "source": f"agent/skills/{skill['dir']}/SKILL.md",
        },
    }
    descriptors = registry_ns.skill_record_descriptors(
        definition, skill_md=skill["body"],
        name=skill["name"], description=skill["description"])

    record = existing.get(dedup)
    if dry_run:
        return "would-update" if record else "would-create"

    if record:
        record_id = record.get("recordId", "")
        # UpdateRegistryRecord wraps EVERY level in `optionalValue`, not just the
        # outer one. Wrapping only the outer level is a fix that looks right and is
        # rejected with an error naming the inner fields — see
        # `agent_registry.as_update_descriptors`, which is where that shape lives so
        # nobody re-derives it.
        client.update_registry_record(
            registryId=registry_id,
            recordId=record_id,
            descriptors=registry_ns.as_update_descriptors(descriptors),
            description={"optionalValue": skill["description"]},
        )
        action = "updated"
    else:
        resp = client.create_registry_record(
            registryId=registry_id,
            name=dedup,
            displayName=skill["name"],
            description=skill["description"],
            recordType=registry_ns.RECORD_TYPE_SKILL,
            descriptors=descriptors,
            recordVersion="1.0.0",
            clientToken=str(uuid.uuid4()),
        )
        record_id = registry_ns.record_id_from_create(resp)
        action = "created"

    status = registry_ns.approve_record(
        client, registry_id, record_id,
        reason="built-in skill shipped with the deployment")
    if status != registry_ns.STATUS_APPROVED:
        # An unapproved record is invisible on the Skills page, so reporting
        # success here would describe a result nobody can see.
        return f"{action}-but-{status}"
    return action


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    if not STATE_FILE.exists():
        sys.exit(f"{STATE_FILE} not found — deploy first")
    registry_id = json.loads(STATE_FILE.read_text()).get("registryId", "")
    if not registry_id:
        sys.exit("no registryId in agentcore-state.json")

    client = boto3.client(registry_ns.REGISTRY_CLIENT, region_name=REGION)
    skills = discover_skills()
    if not skills:
        sys.exit(f"no skills found under {SKILLS_DIR}")

    print(f"Registry {registry_id} in {REGION}")
    print(f"Found {len(skills)} built-in skill(s) under agent/skills/\n")

    existing = existing_skill_records(client, registry_id)
    counts: dict[str, int] = {}
    for skill in skills:
        try:
            result = publish(client, registry_id, skill, existing, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            result = f"failed: {exc}"
        counts[result.split(":")[0]] = counts.get(result.split(":")[0], 0) + 1
        print(f"  {skill['dir']:22s} {result}")

    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    # A failure here leaves the page short of records, which is exactly the
    # condition this script exists to fix — so it must not exit 0.
    return 1 if any(k.startswith("failed") for k in counts) else 0


if __name__ == "__main__":
    sys.exit(main())
