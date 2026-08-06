"""pytest config for iot-discovery.

Every Lambda's entry module is named `index`, and so is every tests/conftest.py.
Importing either by name lets whichever pytest loaded first win for the rest of
the session, so these tests would silently exercise the OTHER Lambda's module.
The fixture below loads index.py by absolute path under a unique module name,
and is exposed as a fixture rather than an importable helper so pytest resolves
it per-directory instead of by module name.
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.abspath(os.path.join(LAMBDA_DIR, "..", "..", ".."))

# device_catalog is copied in beside index.py by scripts/01-install-deps.sh;
# fall back to the source of truth so a fresh clone can run the tests.
for path in (LAMBDA_DIR, os.path.join(REPO_ROOT, "shared")):
    if path not in sys.path:
        sys.path.append(path)

os.environ.setdefault("AWS_REGION", "us-west-2")


@pytest.fixture
def lambda_module():
    """Load THIS directory's index.py under a collision-proof module name."""
    def _load(name):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(LAMBDA_DIR, "index.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    return _load
