"""pytest config — adds the agent dir to sys.path so tests can `import vision`."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("DISABLE_ADOT", "1")
