import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import time, so configure the environment first.
_tmp = tempfile.mkdtemp(prefix="contraste-tests-")
os.environ["DATA_DIR"] = _tmp
os.environ["INGEST_ENABLED"] = "false"
os.environ["GOOGLE_NEWS_ENABLED"] = "false"
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
