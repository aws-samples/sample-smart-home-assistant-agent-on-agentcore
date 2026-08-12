"""Holds the four copies of the A2A group convention byte-identical.

They cannot import each other: the orchestrator, each sub-agent and the admin
Lambda are each packaged from their own directory. The convention is an
authorization boundary, so a copy that drifts does not raise — it either denies a
granted user or offers the model a tool the platform will refuse. Only the module
docstring header differs, because each copy says why it exists.
"""
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CANONICAL = os.path.join(REPO, "shared", "a2a_groups.py")
COPIES = [
    os.path.join(REPO, "a2a-agent-registry", "common", "a2a_groups.py"),
    os.path.join(REPO, "agent", "a2a_groups.py"),
    os.path.join(REPO, "cdk", "lambda", "admin-api", "a2a_groups.py"),
]

# Everything from this line on must match the canonical file exactly. Above it each
# copy carries its own note about which deployment unit it serves.
BODY_ANCHOR = "A grant is a Cognito group."


def _body(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert BODY_ANCHOR in text, f"{path} no longer contains the body anchor"
    return text[text.index(BODY_ANCHOR):]


@pytest.mark.parametrize("copy_path", COPIES, ids=lambda p: os.path.relpath(p, REPO))
def test_copy_matches_canonical(copy_path):
    assert os.path.exists(copy_path), (
        f"{copy_path} is missing; a deployment unit that derives group names "
        "without this module will derive them differently")
    assert _body(copy_path) == _body(CANONICAL), (
        f"{os.path.relpath(copy_path, REPO)} has drifted from shared/a2a_groups.py. "
        "Copy the canonical file over it and re-apply only the header note.")


def test_every_copy_explains_why_it_is_a_copy():
    """A copy with no explanation invites someone to 'fix' the duplication."""
    for path in COPIES:
        with open(path, encoding="utf-8") as fh:
            header = fh.read().split(BODY_ANCHOR)[0]
        assert "COPY of shared/a2a_groups.py" in header, (
            f"{path} does not say it is a copy")
