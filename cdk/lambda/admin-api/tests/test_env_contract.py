"""The admin Lambda's env vars come from two places, and CDK silently wins.

CDK declares 15 variables (8 in the inline `environment:` map, 7 via
`addEnvironment`). Twelve more are patched in afterwards by
`scripts/setup-agentcore.py` — GATEWAY_ID, MEMORY_ID, VOICE_AGENT_RUNTIME_ARN,
DASHBOARD_EXTRA_RUNTIME_ARNS, KB_ID/KB_DATA_SOURCE_ID, the OPTIMIZATION_* /
*_ONLINE_EVAL_ARN / AB_TEST_ROLE_ARN set — because they name resources that do not
exist at synth time. 27 in total on the current deployment.

`environment:` on a CfnFunction is the WHOLE map, so any `cdk deploy` resets the
function to CDK's 15 and drops the other 12. Nothing errors. The symptoms are
remote from the cause: `/tools` quietly returns built-ins only (so the Tool Policy
modal shows no gateway tools and an admin cannot grant `control_device` at all),
and `/optimization/*` answers ConfigurationError.

Do NOT verify this by counting. Two of the variables — REGISTRY_ID and
AGENT_RUNTIME_ARN — are declared BY CDK with the literal value
`PLACEHOLDER_SET_BY_SETUP_SCRIPT`, so a reset leaves them present with a
placeholder and the total unchanged. A count check sees nothing. (The admin manual
and README both carried "expect 28" for a while; the real number was 27, and it
moves whenever a variable is added.) Check the VALUES of the vars you care about,
and for the Registry chain use `scripts/check-registry-wiring.py`.

A wrong value is worse than a missing one, because the code cannot tell. A
REGISTRY_ID pointing at a registry in the LEGACY `bedrock-agentcore` namespace
reads as a perfectly valid configuration and simply returns a shorter catalog —
which on 2026-08-10 was misdiagnosed as a stale botocore and a missing IAM grant
before anyone checked the id itself.

This has now bitten twice. These tests do not stop it — only re-running the setup
script does — but they name the contract, so the next person who adds a variable
knows which side owns it and that a bare `cdk deploy` is not the last step.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
# .../cdk/lambda/admin-api/tests -> the repo root is four levels above the Lambda
# dir. Asserted, because an off-by-one yields a path that does not exist and every
# check below would fail for the wrong reason (or, with a laxer read, pass
# vacuously).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
STACK = os.path.join(ROOT, "cdk", "lib", "smarthome-stack.ts")
SETUP = os.path.join(ROOT, "scripts", "setup-agentcore.py")
assert os.path.isfile(STACK), STACK
assert os.path.isfile(SETUP), SETUP


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# Variables the setup script must patch in after every `cdk deploy`, because
# they name resources CDK does not create.
PATCHED_BY_SETUP = {
    "GATEWAY_ID",
    "MEMORY_ID",
    "REGISTRY_ID",
    "VOICE_AGENT_RUNTIME_ARN",
    "DASHBOARD_EXTRA_RUNTIME_ARNS",
}

# Variables the admin Lambda's code reads. Anything here must be set by one side
# or the other, or the feature that reads it is dead on arrival.
def _env_reads(source: str) -> set[str]:
    return set(re.findall(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]+)["']""",
                          source)) | set(
        re.findall(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]""", source))


def test_the_setup_script_still_patches_every_variable_cdk_cannot_set():
    """If one of these is dropped from the setup script, a `cdk deploy` becomes
    permanently destructive for that variable instead of temporarily."""
    setup = _read(SETUP)
    missing = [name for name in PATCHED_BY_SETUP
               if f'"{name}"' not in setup]
    assert not missing, (
        f"{missing} are not set by scripts/setup-agentcore.py — a cdk deploy will "
        f"drop them and nothing will restore them")


def test_the_setup_script_merges_onto_the_current_env_rather_than_replacing_it():
    """`admin_env = dict(current_admin_env)` is what makes the script a repair
    tool. Rewriting it as a fresh literal would make the script itself drop the
    variables CDK owns."""
    setup = _read(SETUP)
    assert "admin_env = dict(current_admin_env)" in setup, (
        "the setup script no longer merges onto the existing env; it would now "
        "drop whatever CDK set")


def test_the_scenario_variables_are_declared_in_cdk():
    """These name resources CDK DOES create, so CDK is the right owner — putting
    them in the setup script instead would mean a deploy that works only after a
    second, unrelated script runs."""
    stack = _read(STACK)
    for name in ("SCENARIOS_TABLE_NAME", "SCENARIO_RUNNER_ARN",
                 "SCENARIO_SCHEDULER_ROLE_ARN", "SCENARIO_SCHEDULE_GROUP"):
        assert name in stack, f"{name} is not set by the CDK stack"


def test_every_env_var_the_code_reads_is_set_by_someone():
    """A variable read but never set is a feature that silently does nothing.
    Both sources count; the point is that nobody has to guess."""
    code = _read(os.path.join(LAMBDA_DIR, "index.py"))
    stack = _read(STACK)
    setup = _read(SETUP)

    # Read with a default and harmless if absent, or set by the platform.
    EXEMPT = {
        "AWS_REGION", "AWS_REGION_OVERRIDE", "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_DEFAULT_REGION", "PATH", "HOME", "TZ",
    }
    unset = sorted(name for name in _env_reads(code) - EXEMPT
                   if name not in stack and f'"{name}"' not in setup)
    assert not unset, (
        f"read by index.py but set by neither the CDK stack nor "
        f"setup-agentcore.py: {unset}")


def test_the_gateway_id_absence_is_what_empties_the_tool_list():
    """Pins the causal link the symptom hides: without GATEWAY_ID, /tools returns
    built-ins only and the Tool Policy modal offers no gateway tools — so an
    administrator cannot grant or revoke control_device, and the Cedar policies
    that the scheduled runner depends on cannot be edited at all."""
    code = _read(os.path.join(LAMBDA_DIR, "index.py"))
    assert "if not GATEWAY_ID:" in code
    assert 'return response(200, {"tools": tools})' in code
