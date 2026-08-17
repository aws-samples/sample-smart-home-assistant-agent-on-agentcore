"""The demo tarball must not carry a previous run's artifacts.

`export-deploy-demo.py` tars whatever is in the bundle directory, and the bundle
directory is where the demo is RUN from. So a `deploy_runtime.py` run leaves
`demo-state.json` sitting next to the scripts, and the next export ships it.

That one file is the dangerous one. It holds a real `runtimeArn` and `recordId`, and
`register_record.py` reads it: whoever unpacks that tarball and runs the register step
does not create their own record, they **update someone else's** — a version bump
against an agent they have never seen, which knocks it out of APPROVED and takes it
away from every user granted it. Nothing errors.

Found by leaving two artifacts behind while editing the docs. The tarball went from 43
files to 45 and looked perfectly fine, which is the whole reason this file exists.
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "_export_demo", REPO / "scripts" / "export-deploy-demo.py")
export_demo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_demo)

BUNDLE = Path("logs/agentcore-deploy-demo/demo-agent-air-quality")


@pytest.mark.parametrize("name", [
    "demo-state.json",     # runtimeArn + recordId of whoever exported it
    "rendered-card.json",  # derived; stale the moment card.json changes
    "manifest.json",       # the platform contract, per deployment
    "authorizer.json",     # derived from the manifest
])
def test_run_artifacts_are_not_shipped(name):
    assert not export_demo._ship_path(BUNDLE / name)


@pytest.mark.parametrize("part", ["__pycache__", ".venv", ".agentcore-project"])
def test_local_directories_are_not_shipped(part):
    assert not export_demo._ship_path(BUNDLE / part / "anything.py")


@pytest.mark.parametrize("name", [
    "deploy_runtime.py", "register_record.py", "invoke_local.py", "teardown.py",
    "demo-config.json", "requirements.txt", "README.md", "agent_registry.py",
    # The offline pre-flight pair, which is the point of shipping it at all.
    "a2a-preflight.py", "a2a_preflight.py",
])
def test_the_bundle_itself_is_shipped(name):
    """The exclusion list is a denylist, so the risk runs both ways: too wide a rule
    silently drops the demo instead of shipping stale state."""
    assert export_demo._ship_path(BUNDLE / name)


def test_the_payload_is_shipped():
    for name in ("main.py", "card.json", "system_prompt.md", "memory_actor.py",
                 "common/server.py", "common/card.py"):
        assert export_demo._ship_path(BUNDLE / "air-quality" / name), name


def test_the_tarfile_filter_and_the_path_check_agree():
    """Two call sites — the tar filter and the file count — and the count is what tells
    an operator what shipped. They disagreed before: the count included `__pycache__`
    while the tar excluded it, so the log over-reported by nine files."""
    class _TarInfo:
        def __init__(self, name):
            self.name = name

    for rel in ("demo-agent-air-quality/demo-state.json",
                "demo-agent-air-quality/__pycache__/x.pyc",
                "demo-agent-air-quality/.venv/bin/python",
                "demo-agent-air-quality/deploy_runtime.py",
                "demo-agent-air-quality/air-quality/main.py"):
        by_filter = export_demo._ship(_TarInfo(rel)) is not None
        by_path = export_demo._ship_path(Path(rel))
        assert by_filter == by_path, rel


@pytest.mark.skipif(not (REPO / "logs" / "agentcore-deploy-demo.tar.gz").exists(),
                    reason="no tarball exported in this checkout")
def test_the_current_tarball_carries_no_artifacts():
    """The rule applied to the artifact that actually exists, since that is what gets
    handed to an agent team."""
    import tarfile

    with tarfile.open(REPO / "logs" / "agentcore-deploy-demo.tar.gz") as tar:
        names = tar.getnames()
    leaked = [n for n in names
              if os.path.basename(n) in export_demo.NOT_SHIPPED
              or (export_demo.NOT_SHIPPED_DIRS & set(Path(n).parts))]
    assert not leaked, f"the shipped tarball carries run artifacts: {leaked}"
