"""pytest config — adds the Lambda dir to sys.path so tests can `import index`."""
import sys
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("SKILLS_TABLE_NAME", "smarthome-skills-test")
os.environ.setdefault("REGISTRY_ID", "test-registry-id")
os.environ.setdefault("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test-text")
os.environ.setdefault("VOICE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test-voice")

# Credentials exist in the dev environment these tests run in, so a code path that
# reaches AWS does not fail — it SUCCEEDS, against the live account, and the test then
# asserts against live data. `dashboard._registry_runtime_arns` made that reachable
# from any dashboard test: it lists the real Registry and resolves real gateway
# targets. The symptom was four dashboard tests that passed alone and failed in the
# suite, depending on which file had last set REGISTRY_ID.
#
# So every test starts with the Registry-derived allowlist disabled, and the tests that
# want that path (test_dashboard_registry_allowlist.py) opt in AND patch the seams.
@pytest.fixture(autouse=True)
def _no_live_registry_in_unit_tests(monkeypatch):
    monkeypatch.setenv("REGISTRY_ID", "PLACEHOLDER_SET_BY_SETUP_SCRIPT")
