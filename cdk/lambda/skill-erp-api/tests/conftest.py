"""pytest config — adds the Lambda dir to sys.path so tests can `import index`."""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("SKILLS_TABLE_NAME", "smarthome-skills-test")
os.environ.setdefault("REGISTRY_ID", "test-registry-id")
