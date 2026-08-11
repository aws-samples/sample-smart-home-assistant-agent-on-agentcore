"""Tests for the Memory actor-id rule.

This function is boring and its failure mode is not. Nine containers compute an
actor id independently and use it as a namespace path component; if two of them
disagree, nothing errors — each gets a private, half-empty memory, and the symptom
is "the sub-agent doesn't remember what I told the main agent". So what is pinned
here is the exact output, not merely that it is valid: an equivalent-looking
rewrite that maps `.` to `-` instead of `_` would silently orphan every memory
already written.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_actor as ma  # noqa: E402

# What the Memory API accepts. Anything this rejects is not a namespace.
VALID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_/]*$")


def test_an_email_maps_to_the_id_already_in_use():
    """The orchestrator has written memories under this exact form since before
    the sub-agents existed. Changing it abandons them."""
    assert ma.sanitize_actor_id("user@example.com") == "user_example_com"


def test_output_is_always_a_valid_actor_id():
    for raw in ("user@example.com", "UPPER@Example.COM", "a+b@c.d",
                "9starts-with-digit@x.y", "已经中文@example.com", "x"):
        assert VALID.match(ma.sanitize_actor_id(raw)), raw


def test_an_id_that_would_not_start_alphanumeric_is_prefixed():
    # A leading `_` is rejected by the API, and the sanitizer can produce one from
    # any address beginning with a symbol.
    assert ma.sanitize_actor_id("_leading@example.com").startswith("u_")
    assert VALID.match(ma.sanitize_actor_id("_leading@example.com"))


def test_email_wins_over_sub():
    """Both identifiers are verified off the same idToken, so this is a choice
    rather than a trust question — and it has to be the same choice everywhere."""
    actor = ma.memory_actor_id(email="user@example.com", sub="88c1a3e0-b041")
    assert actor == "user_example_com"


def test_sub_is_the_fallback_when_a_token_carries_no_email():
    assert ma.memory_actor_id(email="", sub="88c1a3e0-b041") == "88c1a3e0-b041"
    assert ma.memory_actor_id(sub="88c1a3e0-b041") == "88c1a3e0-b041"


def test_no_identity_yields_no_actor_rather_than_a_shared_default():
    """The caller must treat "" as "no memory this request". A default like
    "default" or "anonymous" would pool unrelated users into one namespace, which
    is a cross-user leak that reads as a working feature."""
    assert ma.memory_actor_id() == ""
    assert ma.memory_actor_id(email="", sub="") == ""
    assert ma.memory_actor_id(email="   ", sub="  ") == ""


def test_the_agent_copy_is_identical_to_the_source():
    """scripts/01-install-deps.sh copies this module into agent/. A hand-edited
    copy is how the two sides drift, so compare bytes when the copy exists."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo = os.path.dirname(root)
    copy = os.path.join(repo, "agent", "memory_actor.py")
    if not os.path.exists(copy):
        return  # build step has not run; nothing to compare
    with open(os.path.join(root, "memory_actor.py"), "rb") as f:
        source = f.read()
    with open(copy, "rb") as f:
        assert f.read() == source, (
            "agent/memory_actor.py differs from shared/memory_actor.py — re-run "
            "scripts/01-install-deps.sh rather than editing the copy")
