"""Smoke test — no real LLM API calls required.

Run with: OPENAI_API_KEY=dummy python smoke_test.py
(or set the env var before running)
"""
import os
import sys
import tempfile

# Set env vars BEFORE any findmemyjob imports so config/db pick them up.
_tmpdir = tempfile.mkdtemp(prefix="fmj_smoke_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")

import importlib

# Force reload config/db so they pick up the env vars we just set.
# (Needed when running after a prior import in the same process.)
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402

# Now import app — config.py and db.py will read the fresh env vars.
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db  # noqa: E402

# Manually call init_db so tables exist (startup event may not fire in TestClient)
init_db()

client = TestClient(app, raise_server_exceptions=True)

failures = []

def check(method, path, expected_status=200):
    resp = getattr(client, method)(path)
    if resp.status_code != expected_status:
        failures.append(f"{method.upper()} {path}: expected {expected_status}, got {resp.status_code}\n  body: {resp.text[:300]}")
        return False
    print(f"  OK  {method.upper()} {path} -> {resp.status_code}")
    return True

print(f"Data dir: {_tmpdir}")
print(f"Testing {app.title} ...")
check("get", "/")
check("get", "/profile")
check("get", "/jobs")
check("get", "/applications")

if failures:
    print("\nFAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("\nAll smoke tests passed.")
