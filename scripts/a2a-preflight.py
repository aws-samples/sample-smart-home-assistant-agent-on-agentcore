#!/usr/bin/env python3
"""Check an AgentCard against a platform manifest, before deploying anything.

    # in YOUR ci, with no access to the platform's account
    python scripts/a2a-preflight.py --card card.json --manifest manifest.json
    python scripts/a2a-preflight.py --card card.json --manifest manifest.json \
        --authorizer authorizer.json
    python scripts/a2a-preflight.py --card card.json --manifest manifest.json \
        --print-authorizer > authorizer.json

Offline. No AWS call, no credentials, no repo state. The manifest is the JSON from the
Admin Console's **Platform manifest** button (or
`GET /registry/records?action=a2a-manifest`), saved to a file; the card is yours.

Why this exists next to the other two checks
--------------------------------------------
`scripts/a2a-authorizer-contract.py` tells you what to configure and
`?action=a2a-conformance` tells you whether a DEPLOYED runtime matches its card. Both
run inside the platform's account. Neither answers the question an agent team actually
has first: *is what I am about to ship going to be accepted?*

Everything checked here otherwise surfaces later and in a misleading form:

  - an incomplete card is rejected as "does not match any supported version", which
    sounds like a version problem and is a completeness problem
  - a name or skill id outside the group-name pattern makes grants SILENTLY skip: an
    admin sees the user as granted, the user is refused at the door
  - a skill id that pushes the group name past 128 characters cannot be granted, and
    nothing says so until someone tries
  - `allowedClients` instead of `allowedAudience` refuses every fully granted user with
    a message about `client_id`
  - a missing `customClaims` means authorization is not happening at all, and the
    production symptom is that everything works

The rules come from the MANIFEST, not from this repo (see `shared/a2a_preflight.py`), so
this runs anywhere and so a rule the manifest fails to publish is reported as a manifest
bug rather than silently defaulted.

Exit status: 0 when nothing blocks, 1 on any `block` or `risk`, 2 on bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Two locations, because there are two ways to run this. In this repo the rule lives in
# `shared/`; an agent team copies THIS FILE and `a2a_preflight.py` into their own CI, and
# will naturally drop them side by side. Failing in that second case would defeat the
# point — a checker only we can run is a checker that keeps us in their debugging loop.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "shared", _HERE):
    if (_candidate / "a2a_preflight.py").exists():
        sys.path.insert(0, str(_candidate))
        break

try:
    import a2a_preflight as pf  # noqa: E402
except ImportError:
    raise SystemExit(
        "cannot find a2a_preflight.py. Put it next to this script, or run this script "
        "from a checkout where it sits in shared/.")

_LABEL = {pf.BLOCK: "BLOCK", pf.RISK: "RISK ", pf.NOTE: "note "}


def _load(path: str, what: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{what} not found: {path}")
    except ValueError as exc:
        raise SystemExit(f"{what} is not valid JSON ({path}): {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", required=True, help="your card.json")
    ap.add_argument("--manifest", required=True,
                    help="the platform manifest JSON (Admin Console -> Integration "
                         "Registry -> A2A Agents -> Platform manifest)")
    ap.add_argument("--authorizer",
                    help="the authorizerConfiguration you are about to deploy")
    ap.add_argument("--print-authorizer", action="store_true",
                    help="print the authorizer the manifest says to deploy, and stop")
    ap.add_argument("--json", action="store_true", help="findings as JSON, for CI")
    args = ap.parse_args(argv)

    card = _load(args.card, "the card")
    manifest = _load(args.manifest, "the manifest")

    if args.print_authorizer:
        try:
            print(json.dumps(pf.expected_authorizer(card, manifest), indent=2))
        except KeyError as exc:
            raise SystemExit(f"cannot print the authorizer: {exc}")
        return 0

    authorizer = _load(args.authorizer, "the authorizer") if args.authorizer else None
    findings = pf.check(card, manifest, authorizer)

    if args.json:
        print(json.dumps({
            "card": card.get("name"),
            "manifestVersion": manifest.get("manifestVersion"),
            "authorizerChecked": authorizer is not None,
            "blocking": pf.is_blocking(findings),
            "worstSeverity": pf.worst_severity(findings),
            "findings": findings,
        }, indent=2, ensure_ascii=False))
        return 1 if pf.is_blocking(findings) else 0

    print(f"\ncard     : {card.get('name') or '<no name>'}")
    print(f"manifest : version {manifest.get('manifestVersion')} "
          f"(registry {(manifest.get('deployment') or {}).get('registryId')})")
    print(f"authorizer: {'checked' if authorizer else 'NOT CHECKED (pass --authorizer)'}")
    print()

    if not findings:
        print("  [ok] nothing to fix — this card is ready to deploy and register")
    for f in findings:
        print(f"  [{_LABEL.get(f['severity'], f['severity'])}] {f['code']}")
        # Wrapped by hand rather than by textwrap: the details carry code identifiers and
        # quoted values that a wrapper happily breaks across lines.
        for line in _wrap(f["detail"], 78):
            print(f"         {line}")

    blocking = pf.is_blocking(findings)
    print()
    if blocking:
        print("BLOCKED — fix the block/risk findings above before deploying.")
        if not authorizer:
            print("Also: pass --authorizer to have the door checked. Without it this run "
                  "says nothing about whether ungranted callers are refused.")
    else:
        notes = len(findings)
        print(f"OK to deploy{f' ({notes} note(s) above)' if notes else ''}.")
        if not authorizer:
            print("NOTE: the authorizer was not checked. `--print-authorizer` emits the "
                  "one the manifest says to deploy; a missing customClaims lets every "
                  "user of the pool reach every skill, with no error anywhere.")
    return 1 if blocking else 0


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
