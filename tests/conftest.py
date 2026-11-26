import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# tests use a throwaway SQLite file (or TEST_DATABASE_URL for a scratch Postgres), never the real database
os.environ.pop("DATABASE_URL", None)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["SQLITE_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["FORWARD_AUTOKICK"] = "0"  # no background forward-test downloads during tests
