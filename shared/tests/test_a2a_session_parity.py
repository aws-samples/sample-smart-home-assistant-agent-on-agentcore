"""Holds the copies of the A2A session-propagation convention byte-identical.

Same arrangement, and the same reason, as test_a2a_groups_parity.py: the
orchestrator and each sub-agent are packaged from their own directory and cannot
import each other, so the convention exists as copies.

Drift here does not raise. The client would send a value the server discarded, or
put it under a key the server never reads — and the symptom is a summary namespace
that never resolves and a delegated turn that still cannot be joined to its
parent, which reads as a memory or observability problem rather than a mismatched
string.
"""
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CANONICAL = os.path.join(REPO, "shared", "a2a_session.py")
COPIES = [
    os.path.join(REPO, "agent", "a2a_session.py"),
    os.path.join(REPO, "a2a-agent-registry", "common", "a2a_session.py"),
]

# Everything from this line on must match the canonical file exactly. Above it each
# copy carries its own note about which deployment unit it serves.
BODY_ANCHOR = "A delegated turn used to be unjoinable"


def _body(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert BODY_ANCHOR in text, f"{path} no longer contains the body anchor"
    return text[text.index(BODY_ANCHOR):]


@pytest.mark.parametrize("copy_path", COPIES, ids=lambda p: os.path.relpath(p, REPO))
def test_copy_matches_canonical(copy_path):
    assert os.path.exists(copy_path), (
        f"{copy_path} is missing; a deployment unit that decides for itself how to "
        "carry the session id will carry it differently")
    assert _body(copy_path) == _body(CANONICAL), (
        f"{os.path.relpath(copy_path, REPO)} has drifted from "
        "shared/a2a_session.py. Copy the canonical file over it and re-apply only "
        "the header note.")


def test_every_copy_explains_why_it_is_a_copy():
    """A copy with no explanation invites someone to 'fix' the duplication."""
    for path in COPIES:
        with open(path, encoding="utf-8") as fh:
            header = fh.read().split(BODY_ANCHOR)[0]
        assert "COPY of shared/a2a_session.py" in header, (
            f"{path} does not say it is a copy")
