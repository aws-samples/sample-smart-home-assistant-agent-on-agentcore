"""Holds the Lambda copies of `agent_registry.py` byte-identical to shared/.

`scripts/01-install-deps.sh` copies `shared/agent_registry.py` into every Lambda
that talks to the Registry (`Code.fromAsset(<dir>)` only packages that directory,
so the module has to be there). Nothing verified the copies afterwards, and they
went stale in the one way that matters: `as_update_descriptors` was added to the
canonical file, the copies were never refreshed, and both edit routes in
skill-erp-api were then written to pass plain values instead — with a comment
asserting the model required plain values, which it does not.

The result was a guaranteed ParamValidationError on every "edit my published
record", invisible in tests because the route tests mock boto3 with a MagicMock
that accepts any shape. Hence two guards, this one and
`cdk/lambda/skill-erp-api/tests/test_registry_update_payload.py`: this file catches
the copy going stale, that one catches a caller building the wrong shape even from
a fresh copy.

Unlike `a2a_groups.py`, these copies are byte-for-byte identical — the copy step is
a plain `cp` with no per-copy header — so this compares whole files. They are also
gitignored build artefacts, so a missing copy is a skip and only a stale one fails:
CDK packages whatever is in the directory, which is what makes a stale copy
deployable.
"""
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CANONICAL = os.path.join(REPO, "shared", "agent_registry.py")
COPIES = [
    os.path.join(REPO, "cdk", "lambda", "skill-erp-api", "agent_registry.py"),
    os.path.join(REPO, "cdk", "lambda", "admin-api", "agent_registry.py"),
]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("copy_path", COPIES, ids=lambda p: os.path.relpath(p, REPO))
def test_copy_matches_canonical(copy_path):
    if not os.path.exists(copy_path):
        # These copies are gitignored build artefacts, so a fresh clone has none —
        # skipping is right, failing would just punish checking the repo out.
        # The drift that matters is a copy that EXISTS and is stale, because that is
        # the one CDK packages.
        pytest.skip(f"{os.path.relpath(copy_path, REPO)} not generated yet — "
                    "run scripts/01-install-deps.sh")
    assert _read(copy_path) == _read(CANONICAL), (
        f"{os.path.relpath(copy_path, REPO)} has drifted from "
        "shared/agent_registry.py, and CDK packages the copy, not the canonical "
        "file. Do not patch the copy: edit shared/agent_registry.py and re-run "
        "`cp shared/agent_registry.py cdk/lambda/<dir>/` (or scripts/01-install-deps.sh). "
        "skill-erp-api's handlers call `as_update_descriptors`, which a stale copy "
        "does not have — deploying one is an AttributeError on every edit.")


def test_the_install_script_still_copies_every_one_of_them():
    """A new Registry Lambda added to COPIES but not to the copy step would pass the
    test above only until someone cleaned the working tree."""
    script = _read(os.path.join(REPO, "scripts", "01-install-deps.sh"))
    assert "shared/agent_registry.py" in script
    for copy_path in COPIES:
        lambda_dir = os.path.basename(os.path.dirname(copy_path))
        assert lambda_dir in script, (
            f"scripts/01-install-deps.sh does not copy the helper into "
            f"{lambda_dir}; the file only exists because someone did it by hand")
