import os
import sys
import tempfile
from pathlib import Path

# decision logs (logs/{agent}.log) go to a temp dir during tests, never into the repo's logs/
os.environ.setdefault("DESK_LOG_DIR", tempfile.mkdtemp(prefix="desk-test-logs-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
