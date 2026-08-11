"""Guards the registry status names, and the wait loop that reads them.

A registry reports READY. It never reports ACTIVE — the enum is
CREATING / READY / UPDATING / CREATE_FAILED / UPDATE_FAILED / DELETING /
DELETE_FAILED, which is a different set from the RECORD statuses in the same
service (DRAFT / PENDING_APPROVAL / APPROVED / ...). The two sets share no values,
so mixing them up is not a type error and nothing raises.

`setup-agentcore.py` polled `get_registry` until the status was "ACTIVE". Because
no registry ever returns that, the loop could only ever exhaust its 15 attempts
and fall through — 30s of sleeping on every deploy that did nothing, and, worse,
identical in behaviour to having no wait at all. A CREATE_FAILED registry sailed
straight through it, and every Lambda was then patched to point at it.

That is the same shape as the other bugs in this repo: the guard reported success.
The deploy printed no warning, the registry id looked fine, and the symptom
surfaced much later and somewhere else, as an empty Integration Registry tab.

So two things are locked down here:
  - the status names, against the vendored botocore service model where available,
    so a future enum change is a red test rather than a silent hang
  - that the wait loop breaks on terminal states and never on a hardcoded
    "ACTIVE"
"""

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "shared"))

import agent_registry as ar  # noqa: E402

SETUP = os.path.join(REPO, "scripts", "setup-agentcore.py")


def _setup_src():
    if not os.path.exists(SETUP):
        pytest.skip("setup-agentcore.py not present")
    return open(SETUP, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# The status names themselves
# ---------------------------------------------------------------------------

def test_ready_is_the_healthy_registry_status():
    assert ar.REGISTRY_STATUS_READY == "READY"


def test_there_is_no_active_registry_status():
    """The exact bug. ACTIVE is a plausible guess and it is wrong."""
    values = {v for k, v in vars(ar).items()
              if k.startswith("REGISTRY_STATUS_") and isinstance(v, str)}
    assert "ACTIVE" not in values, (
        "ACTIVE is not a registry status; a wait loop comparing against it can "
        "only time out")


def test_pending_statuses_are_the_ones_that_can_still_change():
    assert ar.REGISTRY_PENDING_STATUSES == {"CREATING", "UPDATING"}


def test_ready_is_not_treated_as_pending():
    """Otherwise the wait loop spins on a registry that is already usable."""
    assert ar.REGISTRY_STATUS_READY not in ar.REGISTRY_PENDING_STATUSES


def test_registry_and_record_statuses_are_kept_apart():
    """They overlap on CREATING/UPDATING and diverge everywhere else, which is
    exactly the condition under which conflating them goes unnoticed."""
    registry = {v for k, v in vars(ar).items()
                if k.startswith("REGISTRY_STATUS_") and isinstance(v, str)}
    assert "APPROVED" not in registry
    assert "DRAFT" not in registry
    assert ar.STATUS_APPROVED not in registry


def test_status_names_match_the_service_model():
    """Reads the enum out of botocore when a GA-aware one is importable.

    Skipped rather than vendored-in: the assertion is only meaningful against a
    botocore that knows the GA service, and the point is to catch a real enum
    change, not to restate the constants.
    """
    try:
        import boto3
        session = boto3.Session()
        if ar.REGISTRY_CLIENT not in session.get_available_services():
            pytest.skip("installed botocore predates Agent Registry GA")
        model = session.client(
            ar.REGISTRY_CLIENT, region_name="us-west-2").meta.service_model
        shape = model.operation_model("GetRegistry").output_shape
        enum = set(shape.members["status"].enum)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"service model unavailable: {e}")

    declared = {v for k, v in vars(ar).items()
                if k.startswith("REGISTRY_STATUS_") and isinstance(v, str)}
    assert declared == enum, (
        f"registry status constants drifted from the service model.\n"
        f"  only in code:  {sorted(declared - enum)}\n"
        f"  only in model: {sorted(enum - declared)}")


# ---------------------------------------------------------------------------
# The wait loop in setup-agentcore.py
# ---------------------------------------------------------------------------

def test_the_wait_loop_does_not_compare_against_active():
    """Regression. This is the line that made the wait a no-op."""
    src = _setup_src()
    offenders = [ln.strip() for ln in src.splitlines()
                 if re.search(r'get_registry\b', ln) is None
                 and 'status' in ln
                 and re.search(r'==\s*["\']ACTIVE["\']', ln)
                 and 'reg_' in ln]
    assert not offenders, (
        "setup-agentcore.py compares a registry status against ACTIVE, which no "
        "registry returns:\n  " + "\n  ".join(offenders))


def test_the_wait_loop_breaks_on_terminal_states():
    """Break on 'no longer CREATING/UPDATING' rather than on one hoped-for value.

    Enumerating the good states means a new one hangs the loop; enumerating the
    transient ones means a new terminal state exits it, which is the safe
    direction.
    """
    src = _setup_src()
    assert re.search(r'reg_status\s+not in\s*\(\s*["\']CREATING["\']\s*,\s*'
                     r'["\']UPDATING["\']\s*\)', src), (
        "the registry wait should exit once the status leaves CREATING/UPDATING")


def test_a_registry_that_is_not_ready_is_reported():
    """Silence here is what let a broken registry be wired into every Lambda."""
    src = _setup_src()
    block = src[src.find("Wait for the registry to settle"):]
    block = block[:block.find("Patch admin + skill-erp Lambdas")]
    assert block, "the registry wait block moved; this test needs updating"
    assert 'REGISTRY_STATUS_READY' in block or '!= "READY"' in block, (
        "nothing checks whether the settled status is actually READY")
    assert "Warning" in block, (
        "a non-READY registry must produce a visible warning, not a silent pass")


def test_get_registry_failures_are_not_swallowed_silently():
    """`except Exception: pass` inside the poll hid AccessDenied for 30s.

    Matched on the token sequence rather than on an exactly-indented string: the
    first version of this test hardcoded the old file's indentation, so it passed
    against the very code it was written to reject.
    """
    src = _setup_src()
    block = src[src.find("Wait for the registry to settle"):]
    block = block[:block.find("Patch admin + skill-erp Lambdas")]
    assert block, "the registry wait block moved; this test needs updating"
    assert not re.search(r"except\s+Exception[^:]*:\s*\n\s*pass\b", block), (
        "a failing get_registry inside the wait loop must say so, not pass")
