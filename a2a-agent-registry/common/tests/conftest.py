"""pytest config for the shared A2A agent code.

`common` is imported as a package (``from common.server import ...``) exactly as
the deployed container does, so the parent directory goes on sys.path.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.dirname(HERE)
REGISTRY_DIR = os.path.dirname(COMMON_DIR)
if REGISTRY_DIR not in sys.path:
    sys.path.insert(0, REGISTRY_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("COGNITO_REGION", "us-west-2")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-west-2_TESTPOOL")
