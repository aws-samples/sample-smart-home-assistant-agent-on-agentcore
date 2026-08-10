"""pytest config for the scenario runner.

Every Lambda's entry module is named `index`, so a plain `import index` gets
whichever one pytest loaded first — these tests would then exercise the admin
API's module and fail on the wrong code. Loaded by absolute path under a unique
name instead, at import time, because the tests patch attributes on it at module
scope.

`scenarios.py` and `device_catalog.py` are build outputs here, copied from
shared/ by scripts/01-install-deps.sh so `Code.fromAsset` packages them. When
they are missing (a clean checkout with no build run) the import below fails
loudly rather than these tests silently not covering anything.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

# The Lambda imports `scenarios` as a sibling. In the packaged artifact it is a
# copy; for the tests, shared/ is the source of truth, so a stale copy in the
# build directory cannot make a passing test lie.
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("USER_POOL_CLIENT_ID", "test-client-id")


def _load(name, filename="index.py"):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(LAMBDA_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Registered ONLY under a unique name. Claiming the bare `index` too would break
# the admin API's tests, whose own conftest imports it by that name — the
# collision is symmetric, so each side has to use a name of its own rather than
# race for the shared one.
scenario_runner_index = _load("scenario_runner_index")
