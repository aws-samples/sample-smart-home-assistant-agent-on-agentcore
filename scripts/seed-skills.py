#!/usr/bin/env python3
"""Seed DynamoDB skills table from existing SKILL.md files.

Reads the 4 built-in skills from agent/skills/ and writes them to
DynamoDB as __global__ skills.  Safe to re-run — uses put_item which
overwrites existing items with the same key.
"""

import json
import os
import sys
from datetime import datetime, timezone

import boto3
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, "agent", "skills")
STACK_NAME = "SmartHomeAssistantStack"
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))


def get_table_name():
    """Read SkillsTableName from CDK stack outputs."""
    cf = boto3.client("cloudformation", region_name=REGION)
    resp = cf.describe_stacks(StackName=STACK_NAME)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    return outputs.get("SkillsTableName", "smarthome-skills")


def parse_skill_md(filepath):
    """Parse a SKILL.md file into (frontmatter_dict, body_str)."""
    with open(filepath) as f:
        content = f.read().strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid SKILL.md format in {filepath}")
    frontmatter = yaml.safe_load(parts[1])
    body = parts[2].strip()
    return frontmatter, body


def main():
    table_name = get_table_name()
    print(f"Seeding skills to DynamoDB table: {table_name}")

    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(table_name)
    now = datetime.now(timezone.utc).isoformat()

    count = 0
    for skill_dir in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
        if not os.path.isfile(skill_path):
            continue

        fm, body = parse_skill_md(skill_path)
        skill_name = fm.get("name", skill_dir)
        allowed_tools_raw = fm.get("allowed-tools", "")
        if isinstance(allowed_tools_raw, str):
            allowed_tools = [t for t in allowed_tools_raw.split() if t]
        elif isinstance(allowed_tools_raw, list):
            allowed_tools = allowed_tools_raw
        else:
            allowed_tools = []

        table.put_item(Item={
            "userId": "__global__",
            "skillName": skill_name,
            "description": fm.get("description", ""),
            "instructions": body,
            "allowedTools": allowed_tools,
            "createdAt": now,
            "updatedAt": now,
        })
        print(f"  Seeded: {skill_name}")
        count += 1

    print(f"Done. {count} skills seeded as __global__.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
