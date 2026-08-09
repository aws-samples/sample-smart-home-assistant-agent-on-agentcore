"""Guards that every Registry caller uses the GA namespace.

The migration's failure mode is not a crash. `agent/tools/a2a.py` treats a
Registry lookup it cannot complete as "no card available", warns, and continues —
so a caller left on the old namespace after 2026-09-17, or a role still relying on
the `bedrock-agentcore:*` wildcard that no longer covers Registry, makes the A2A
tools quietly stop appearing. Nothing fails; the capability just goes away.

These tests read the source of each caller. That is coarse, but it catches the one
thing that matters here — a `bedrock-agentcore` client being used for a Registry
call — which no unit test of behaviour would notice, because the mock does not care
which namespace it was built from.
"""

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "shared"))

import agent_registry as ar

# Every Registry operation this repo calls. If a client built from the old
# namespace is used for any of these, the call stops working at the cutoff.
REGISTRY_OPS = (
    "create_registry", "get_registry", "update_registry", "delete_registry",
    "list_registries",
    "create_registry_record", "get_registry_record", "update_registry_record",
    "delete_registry_record", "list_registry_records",
    "update_registry_record_status", "submit_registry_record_for_approval",
)

# Files that talk to the Registry, and the client variable each uses for it.
CALLERS = {
    "agent/tools/a2a.py": None,                      # builds its own per call
    "scripts/setup-agentcore.py": "registry_control",
    "scripts/teardown-agentcore.py": "registry_control",
    "cdk/lambda/admin-api/index.py": "registry_control",
    "cdk/lambda/skill-erp-api/index.py": "registry_control",
    "a2a-agent-registry/deploy.py": "registry_control",
    "a2a-agent-registry/teardown.py": "registry_control",
    "a2a-agent-registry/demo_reset.py": "registry_control",
    "a2a-agent-registry/approve_records.py": "registry_control",
}


def _read(rel):
    path = os.path.join(REPO, rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} not present")
    return open(path, encoding="utf-8").read()


def _code_lines(src):
    """Source lines with comments and string literals blanked out.

    The preview field names legitimately appear in comments that explain what
    changed at GA, and in the fallback readers that keep pre-migration records
    working. Only executable code should be searched for them — otherwise the
    guard fires on its own documentation.

    Uses `tokenize` rather than hand-rolled stripping so triple-quoted strings and
    `#` inside string literals are handled properly.
    """
    import io
    import tokenize

    lines = src.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return lines  # unparseable: fall back to raw, better a false alarm than a miss

    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            idx = row - 1
            if idx >= len(blanked):
                continue
            line = blanked[idx]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            blanked[idx] = line[:start] + " " * (end - start) + line[end:]
    return blanked


@pytest.mark.parametrize("rel", sorted(CALLERS))
def test_registry_calls_do_not_use_the_old_namespace_client(rel):
    """A Registry op invoked on a `bedrock-agentcore-control` client is the bug.

    Checked by walking each line that performs a Registry op and confirming the
    receiver is not a client built from the old namespace.
    """
    src = _read(rel)
    # Which variables were built from the OLD namespace in this file?
    old_vars = set(re.findall(
        r"(\w+)\s*=\s*boto3\.client\(\s*[\"']bedrock-agentcore-control[\"']", src))
    old_vars |= set(re.findall(
        r"(\w+)\s*=\s*boto3\.client\(\s*[\"']bedrock-agentcore[\"']", src))
    if not old_vars:
        return

    offenders = []
    for line in _code_lines(src):
        for op in REGISTRY_OPS:
            for var in old_vars:
                if f"{var}.{op}(" in line:
                    offenders.append(line.strip()[:100])
    assert not offenders, (
        f"{rel} calls Registry operations on an old-namespace client — these stop "
        f"working when bedrock-agentcore drops Registry:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("rel", sorted(CALLERS))
def test_each_caller_builds_a_ga_registry_client(rel):
    """Every file that performs a Registry op must build a GA client somewhere."""
    src = _read(rel)
    does_registry = any(f".{op}(" in src for op in REGISTRY_OPS)
    if not does_registry:
        return
    assert ("agent-registry-control" in src
            or "registry_client(" in src
            or "REGISTRY_CLIENT" in src), (
        f"{rel} performs Registry operations but never builds an "
        f"agent-registry-control client")


def test_the_agent_runtime_copy_agrees_with_the_shared_module():
    """agent/tools/a2a.py duplicates the namespace constant and the card reader
    because only agent/ is packaged into its CodeZip. Duplication is acceptable;
    silent divergence is not."""
    src = _read("agent/tools/a2a.py")
    assert f'REGISTRY_CLIENT = "{ar.REGISTRY_CLIENT}"' in src

    # Load the runtime module for real and compare behaviour, rather than exec'ing
    # a slice of its source — the slice approach broke on the module's own imports.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "a2a_under_namespace_test", os.path.join(REPO, "agent", "tools", "a2a.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"agent/tools/a2a.py not importable here: {exc}")

    for record in (
        {"descriptors": {"a2aAgentCard": {"data": "GA"}}},
        {"descriptors": {"a2a": {"agentCard": {"inlineContent": "PREVIEW"}}}},
        {"descriptors": {"a2aAgentCard": {}}},
        {"descriptors": {}},
        {},
    ):
        assert module.read_agent_card(record) == ar.read_agent_card(record), record


def test_no_caller_still_sends_descriptor_type():
    """`descriptorType` was removed at GA in favour of a top-level `recordType`.
    A leftover use is a ValidationException at the first create."""
    for rel in sorted(CALLERS):
        for line in _code_lines(_read(rel)):
            assert "descriptorType" not in line, (
                f"{rel} still sends descriptorType: {line.strip()[:90]}")


def test_no_caller_still_reads_inline_content():
    """`inlineContent` became `data`. Reading the old key yields None, which the
    A2A tool builder treats as a missing card and skips silently."""
    for rel in sorted(CALLERS):
        for line in _code_lines(_read(rel)):
            if "inlineContent" not in line:
                continue
            # READING it is allowed only on the preview-compatibility fallback,
            # which keeps pre-migration records working. WRITING it never is.
            assert ".get(" in line, (
                f"{rel} writes inlineContent: {line.strip()[:90]}")
