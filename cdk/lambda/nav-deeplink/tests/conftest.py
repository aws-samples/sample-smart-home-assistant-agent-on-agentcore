"""pytest config for nav-deeplink.

Every Lambda's entry module is named `index`, and so is every tests/conftest.py.
Importing either by name lets whichever pytest loaded first win for the rest of
the session, so these tests would silently exercise the OTHER Lambda's module.
The fixture below loads index.py by absolute path under a unique module name.
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)


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
