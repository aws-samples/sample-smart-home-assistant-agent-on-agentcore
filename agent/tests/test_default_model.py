"""The default model id is written in four places. This holds them in agreement.

They cannot import each other: the agent runs in its own CodeZip, the admin Lambda
is packaged from its own directory, and the two scripts are run from the repo root
by shell wrappers without the agent tree on the path. So the constant is repeated,
and repetition is only safe if something fails when a copy drifts.

Drift is not loud. `restore-text-runtime-config.py` runs after every deploy and
overwrites the runtime's MODEL_ID; if its copy were stale it would silently
downgrade the model on each deploy, and the only symptom would be answers that got
slightly worse. The same script exists because `agentcore deploy` strips env vars,
so this file guards the guard.
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

AGENT_PY = REPO / "agent" / "agent.py"
MODEL_CATALOG_PY = REPO / "cdk" / "lambda" / "admin-api" / "model_catalog.py"
SETUP_PY = REPO / "scripts" / "setup-agentcore.py"
RESTORE_PY = REPO / "scripts" / "restore-text-runtime-config.py"

EXPECTED = "us.anthropic.claude-sonnet-4-6"


def _env_default(path: Path, var: str) -> str:
    """The fallback in `os.environ.get("<var>", "<default>")`."""
    match = re.search(
        rf'os\.environ\.get\(\s*["\']{re.escape(var)}["\']\s*,\s*["\']([^"\']+)["\']',
        path.read_text(encoding="utf-8"))
    assert match, f"no os.environ.get({var!r}, <default>) found in {path.name}"
    return match.group(1)


def _assigned(path: Path, name: str) -> str:
    """The string a module-level `NAME = "..."` assignment holds."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value.value
    raise AssertionError(f"no module-level {name} in {path.name}")


def test_agent_default_model():
    assert _env_default(AGENT_PY, "MODEL_ID") == EXPECTED


def test_admin_lambda_default_model():
    assert _env_default(MODEL_CATALOG_PY, "MODEL_ID") == EXPECTED


def test_setup_script_default_model():
    assert _assigned(SETUP_PY, "DEFAULT_MODEL_ID") == EXPECTED


def test_restore_script_default_model():
    """The `or "<default>"` fallback, used when agentcore-state.json has no model."""
    match = re.search(r'"MODEL_ID":\s*ac_state\.get\("modelId"\)\s*or\s*"([^"]+)"',
                      RESTORE_PY.read_text(encoding="utf-8"))
    assert match, "restore-text-runtime-config.py no longer sets MODEL_ID"
    assert match.group(1) == EXPECTED


def test_setup_script_does_not_hardcode_the_model_id_inline():
    """Both env writes must go through the constant, or one gets missed."""
    source = SETUP_PY.read_text(encoding="utf-8")
    inline = re.findall(r'"MODEL_ID":\s*"([^"]+)"', source)
    assert not inline, (
        f"setup-agentcore.py hardcodes MODEL_ID at {inline}; use DEFAULT_MODEL_ID "
        "so both env writes stay in agreement")
    assert source.count('"MODEL_ID": DEFAULT_MODEL_ID') >= 1
    assert 'existing_env["MODEL_ID"] = DEFAULT_MODEL_ID' in source


def test_create_agent_still_routes_through_model_provider():
    """The seam that made a second endpoint possible, and will again.

    Bedrock Mantle is not integrated right now, but the routing seam is kept so
    re-adding it is a change in one module rather than in the request path.
    """
    source = AGENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "create_agent")
    body = ast.get_source_segment(source, fn) or ""
    assert "model_provider.build_model" in body, (
        "create_agent no longer routes through model_provider; a Mantle-only "
        "default model cannot be reached with BedrockModel")
    assert "model_provider.resolve_endpoint" in body
