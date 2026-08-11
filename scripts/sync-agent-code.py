#!/usr/bin/env python3
"""Refresh the agentcore CodeZip source from `agent/`. Run BEFORE `agentcore deploy`.

`.agentcore-project/smarthome/app/smarthome/` is a COPY of `agent/`, not a link.
`setup-agentcore.py` makes that copy (`shutil.copytree`) as one step of a full
provision, and `agentcore deploy` packages whatever is sitting there. So a deploy
run on its own — which is the normal way to ship an agent change — ships the copy
from the last full provision and silently ignores every edit made since.

That is exactly how it failed on 2026-08-11. Two features were written, tested,
committed and "deployed": the S2 delegation brief (`device_brief.py`, plus the
change to `tools/a2a.py` that appends it) and S3 prompt caching (`cache_config` in
`create_agent`). The deploy succeeded, the runtime went READY on a new version, and
neither feature was in it — the copy was five hours old and did not even contain
the new files. Nothing warned: `agentcore deploy` has no opinion about whether the
directory it is packaging matches the repo.

The symptoms were indistinguishable from the features not working:

  - The specialists kept calling `discover_devices`, which read as a prompt that
    the model was ignoring — the same signature as a genuine prompt-tuning failure,
    which had ALSO happened an hour earlier for a different reason.
  - CloudWatch showed CacheReadInputTokenCount at zero for the live turns while a
    local test against the same model cached 166k tokens, which read as "caching
    does not work on Bedrock through AgentCore".

Both hypotheses were wrong and both were plausible. Hence this script, and hence
`--check`, so CI or a deploy wrapper can fail rather than ship a stale copy.

    ./venv/bin/python scripts/sync-agent-code.py          # copy, report what changed
    ./venv/bin/python scripts/sync-agent-code.py --check   # exit 1 if out of date
    ./venv/bin/python scripts/sync-agent-code.py --voice   # the voice runtime too

`tests/` and `__pycache__/` are excluded, matching setup-agentcore.py: the CodeZip
should not carry pytest imports into production.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_SRC = ROOT / "agent"
PROJECTS = {
    "text": ROOT / ".agentcore-project" / "smarthome" / "app" / "smarthome",
    "voice": ROOT / ".agentcore-project" / "smarthomevoice" / "app" / "smarthomevoice",
}
IGNORE = shutil.ignore_patterns("tests", "__pycache__", "*.pyc")
# Files the CLI or a provision step writes INTO the copy, which have no counterpart
# in agent/. Reported as "extra" rather than deleted, because deleting one would
# break the very deploy this script exists to make correct.
EXPECTED_EXTRA = {"Dockerfile", "pyproject.toml", "requirements.txt", "__init__.py"}


def _differences(src: Path, dst: Path) -> tuple[list[str], list[str], list[str]]:
    """(changed, missing_in_dst, extra_in_dst) as repo-relative paths."""
    changed: list[str] = []
    missing: list[str] = []
    extra: list[str] = []

    def walk(rel: Path) -> None:
        s, d = src / rel, dst / rel
        cmp = filecmp.dircmp(str(s), str(d), ignore=["tests", "__pycache__"])
        for name in cmp.left_only:
            if name.endswith(".pyc"):
                continue
            missing.append(str(rel / name))
        for name in cmp.right_only:
            if name in EXPECTED_EXTRA or name.endswith(".pyc") or name == "__pycache__":
                continue
            extra.append(str(rel / name))
        for name in cmp.diff_files:
            changed.append(str(rel / name))
        for name in cmp.common_dirs:
            walk(rel / name)

    if not dst.exists():
        return [], ["(entire directory)"], []
    walk(Path("."))
    return changed, missing, extra


def _report(label: str, changed, missing, extra) -> bool:
    stale = bool(changed or missing)
    if not stale and not extra:
        print(f"  {label}: up to date")
        return False
    for path in changed:
        print(f"  {label}: CHANGED  {path}")
    for path in missing:
        print(f"  {label}: MISSING  {path}  <- would not be deployed at all")
    for path in extra:
        # Not an error: the CLI legitimately writes some files here. Worth
        # printing, because an unexpected one means a stale build artifact is
        # being shipped.
        print(f"  {label}: extra    {path}")
    return stale


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report only; exit 1 if a copy is out of date")
    ap.add_argument("--voice", action="store_true",
                    help="also sync the voice runtime's copy")
    args = ap.parse_args()

    if not AGENT_SRC.is_dir():
        sys.exit(f"{AGENT_SRC} not found")

    wanted = ["text"] + (["voice"] if args.voice else [])
    stale_any = False
    for label in wanted:
        dst = PROJECTS[label]
        if not dst.parent.parent.exists():
            print(f"  {label}: no agentcore project at {dst.parent.parent} — skipped")
            continue
        changed, missing, extra = _differences(AGENT_SRC, dst)
        stale = _report(label, changed, missing, extra)
        stale_any = stale_any or stale
        if stale and not args.check:
            # Copy file-by-file rather than rmtree + copytree: the destination
            # holds Dockerfile / requirements.txt / pyproject.toml that a
            # provision step wrote and `agent/` has no copy of, and wiping those
            # would break the deploy.
            for rel in changed + [m for m in missing if m != "(entire directory)"]:
                source = AGENT_SRC / rel
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, ignore=IGNORE,
                                    dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            print(f"  {label}: synced {len(changed) + len(missing)} path(s)")

    if args.check and stale_any:
        print("\nThe CodeZip source is STALE. `agentcore deploy` would package the "
              "old code and report success.\n"
              "Run: ./venv/bin/python scripts/sync-agent-code.py")
        return 1
    if not args.check:
        print("\nCodeZip source matches agent/. Safe to run `agentcore deploy`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
