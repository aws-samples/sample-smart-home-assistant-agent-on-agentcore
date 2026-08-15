"""The generated copies under cdk/lambda/*/ must match shared/.

These copies are gitignored and written by scripts/01-install-deps.sh. CDK packages
the COPY, not the original, so a stale one ships silently: the Lambda runs code the
repo no longer contains, and the symptom appears somewhere else entirely. That has
already happened once — an `UpdateRegistryRecord` fix sat in shared/agent_registry.py
while the admin API kept shipping the old payload shape.

Absent copies SKIP rather than fail: a fresh checkout has none until the install
script runs, and this test's job is to catch drift, not to police setup order.
A present-but-different copy is always a failure.
"""
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (shared module, the Lambda dirs 01-install-deps.sh copies it into)
GENERATED = [
    ("agent_registry.py", ["admin-api", "skill-erp-api", "pre-token"]),
    ("subagent_policy.py", ["admin-api", "pre-token"]),
    ("memory_actor.py", ["admin-api"]),
]

CASES = [(module, d) for module, dirs in GENERATED for d in dirs]


@pytest.mark.parametrize("module,lambda_dir", CASES,
                         ids=[f"{d}/{m}" for m, d in CASES])
def test_copy_matches_shared(module, lambda_dir):
    copy_path = os.path.join(REPO, "cdk", "lambda", lambda_dir, module)
    if not os.path.exists(os.path.dirname(copy_path)):
        pytest.skip(f"cdk/lambda/{lambda_dir} does not exist in this checkout")
    if not os.path.exists(copy_path):
        pytest.skip(f"{lambda_dir}/{module} not generated yet — "
                    "run scripts/01-install-deps.sh")

    with open(os.path.join(REPO, "shared", module), encoding="utf-8") as fh:
        canonical = fh.read()
    with open(copy_path, encoding="utf-8") as fh:
        copy = fh.read()
    assert copy == canonical, (
        f"cdk/lambda/{lambda_dir}/{module} is stale. CDK packages this copy, so a "
        f"deploy would ship code that differs from shared/{module}. "
        "Run scripts/01-install-deps.sh.")
