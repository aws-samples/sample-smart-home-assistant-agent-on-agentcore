#!/usr/bin/env python3
"""Regenerate the admin Lambda's copy of the A2A sub-agents' default prompts.

The admin Lambda is packaged from `cdk/lambda/admin-api/` alone, so it cannot
read `a2a-agent-registry/<agent>/system_prompt.md` at runtime — but the Prompt
editor needs those strings to render "Revert to Default" and to show an admin
what they are overriding. Hence a mirror, generated rather than hand-copied.

`test_prompt_defaults_mirror.py` fails when the mirror and the source files
disagree, so a prompt edit that forgets this script is caught by the suite rather
than shipping a "default" that no agent has ever used.

Usage:  ./venv/bin/python scripts/gen-prompt-defaults.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
A2A = ROOT / "a2a-agent-registry"
TARGET = ROOT / "cdk" / "lambda" / "admin-api" / "a2a_prompt_defaults.py"

HEADER = '''"""Default system prompts for the A2A sub-agents — GENERATED, do not edit.

Mirrors `a2a-agent-registry/<agent>/system_prompt.md`, keyed by AgentCard name
(the identifier the console, the Registry record and the running sub-agent each
derive independently).

Regenerate with:  ./venv/bin/python scripts/gen-prompt-defaults.py
Enforced by:      cdk/lambda/admin-api/tests/test_prompt_defaults_mirror.py
"""

# fmt: off
A2A_DEFAULTS: dict[str, str] = {
'''

FOOTER = "}\n# fmt: on\n"


def main() -> int:
    sys.path.insert(0, str(A2A))
    from common.agents import AGENTS  # noqa: PLC0415

    parts = [HEADER]
    for directory, (long_name, _slug) in sorted(AGENTS.items()):
        src = A2A / directory / "system_prompt.md"
        if not src.exists():
            print(f"error: {src} is missing", file=sys.stderr)
            return 1
        body = src.read_text(encoding="utf-8")
        # A repr keeps the file valid whatever the prompt contains — these are
        # multi-line, quote-bearing and non-ASCII, so a triple-quoted literal
        # would be a guess about the content.
        parts.append(f"    {long_name!r}: {body!r},\n")
    parts.append(FOOTER)

    TARGET.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(AGENTS)} agents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
