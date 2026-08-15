"""Every path the published manifest names must exist in this repo.

The manifest is the contract handed to a third-party agent team. A path in it that
does not exist is worse than an absent entry: the reader cannot tell "not built yet"
from "you gave me the wrong path", and the first thing they do with a contract is try
to follow it.

Caught exactly that — a `preflight` key pointing at `scripts/a2a-preflight.py`, which
was never written.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_manifest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CASES = sorted(a2a_manifest.DOCS.items())


@pytest.mark.parametrize("key,rel_path", CASES, ids=[k for k, _ in CASES])
def test_documented_path_exists(key, rel_path):
    assert os.path.exists(os.path.join(REPO, rel_path)), (
        f"the manifest publishes docs.{key} = {rel_path!r}, which does not exist. "
        "Either build it or remove the entry — a dangling path in the contract sends "
        "an agent team looking for something that was never written.")


def test_the_paths_are_repo_relative():
    """An absolute or `../` path would be meaningless to whoever receives this."""
    for key, rel in a2a_manifest.DOCS.items():
        assert not os.path.isabs(rel) and ".." not in rel, \
            f"docs.{key} = {rel!r} is not a plain repo-relative path"
