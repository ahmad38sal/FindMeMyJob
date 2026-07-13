"""Extension job-picker smoke test (SQLite, no LLM).

Covers GET /api/ext/jobs — the tracked-job list backing the extension's manual
job picker:
  - token gate: 503 when FINDMEMYJOB_EXT_TOKEN is unset, 401 on a bad token,
    200 with the right bearer.
  - each item has {job_id, title, company, url, match_score,
    tailored_resume_available, status}.
  - match_score / tailored_resume_available / status come from the job's
    Application + Resume, matching match-by-url's shapes.
  - ?q= is a case-insensitive substring filter over title + company.
  - ordering is most-recently-touched first.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python ext_jobs_smoke.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

_tmpdir = tempfile.mkdtemp(prefix="fmj_extjobs_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite
os.environ["FINDMEMYJOB_EXT_TOKEN"] = "test-token"  # enable /api/ext

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from findmemyjob.config import settings  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine as db_engine  # noqa: E402
from findmemyjob.models import (  # noqa: E402
    Application,
    ApplicationStatus,
    Job,
    Resume,
    ResumeKind,
)

settings.ext_token = "test-token"
init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


# ---------------------------------------------------------------------------
# Seed: three jobs with different Application/Resume states.
# ---------------------------------------------------------------------------
now = datetime.utcnow()
with Session(db_engine) as s:
    # Job A: has a tailored resume with a pdf_path + a scored Application.
    job_a = Job(source="extension", source_id="a", title="Senior Backend Engineer",
                company="Acme Corp", url="https://acme.example.com/jobs/a",
                fetched_at=now - timedelta(days=2))
    s.add(job_a)
    s.commit()
    s.refresh(job_a)
    resume_a = Resume(kind=ResumeKind.tailored, job_id=job_a.id, content={},
                      pdf_path=os.path.join(_tmpdir, "a.pdf"))
    s.add(resume_a)
    s.commit()
    s.refresh(resume_a)
    app_a = Application(job_id=job_a.id, status=ApplicationStatus.ready,
                        match_score=0.82, tailored_resume_id=resume_a.id,
                        last_status_change=now - timedelta(hours=1))
    s.add(app_a)

    # Job B: Application exists (scored) but no tailored resume.
    job_b = Job(source="extension", source_id="b", title="Frontend Developer",
                company="Globex", url="https://globex.example.com/jobs/b",
                fetched_at=now - timedelta(days=1))
    s.add(job_b)
    s.commit()
    s.refresh(job_b)
    app_b = Application(job_id=job_b.id, status=ApplicationStatus.pending,
                        match_score=0.55, last_status_change=now - timedelta(hours=3))
    s.add(app_b)

    # Job C: no Application at all — bare tracked job.
    job_c = Job(source="extension", source_id="c", title="Data Scientist",
                company="Initech", url="https://initech.example.com/jobs/c",
                fetched_at=now - timedelta(days=5))
    s.add(job_c)
    s.commit()
    ids = {"a": job_a.id, "b": job_b.id, "c": job_c.id}

client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer test-token"}

# ---------------------------------------------------------------------------
# 1. Token gate
# ---------------------------------------------------------------------------
print("\n[GET /api/ext/jobs — auth]")
r = client.get("/api/ext/jobs")
ok(r.status_code == 401, f"no token -> 401 (got {r.status_code})")

r = client.get("/api/ext/jobs", headers={"Authorization": "Bearer wrong"})
ok(r.status_code == 401, f"bad token -> 401 (got {r.status_code})")

# 503 when the token is unset entirely.
_saved = settings.ext_token
settings.ext_token = ""
try:
    r = client.get("/api/ext/jobs", headers=H)
    ok(r.status_code == 503, f"unset token -> 503 (got {r.status_code})")
finally:
    settings.ext_token = _saved

# ---------------------------------------------------------------------------
# 2. Fields + shapes
# ---------------------------------------------------------------------------
print("\n[GET /api/ext/jobs — fields]")
r = client.get("/api/ext/jobs", headers=H)
ok(r.status_code == 200, f"with token -> 200 (got {r.status_code})")
body = r.json()
ok(isinstance(body.get("jobs"), list), "response has a jobs list")
jobs = body["jobs"]
ok(len(jobs) == 3, f"all three tracked jobs returned (got {len(jobs)})")

by_id = {j["job_id"]: j for j in jobs}
for key in ("job_id", "title", "company", "url", "match_score",
            "tailored_resume_available", "status"):
    ok(all(key in j for j in jobs), f"every item has '{key}'")

a = by_id[ids["a"]]
ok(a["tailored_resume_available"] is True, "job A: tailored_resume_available True (resume has pdf_path)")
ok(abs(a["match_score"] - 0.82) < 1e-6, "job A: match_score from Application")
ok(a["status"] == "ready", "job A: status from Application")
ok(a["title"] == "Senior Backend Engineer" and a["company"] == "Acme Corp", "job A: title/company")
ok(a["url"] == "https://acme.example.com/jobs/a", "job A: url")

b = by_id[ids["b"]]
ok(b["tailored_resume_available"] is False, "job B: no tailored resume -> False")
ok(abs(b["match_score"] - 0.55) < 1e-6, "job B: match_score from Application")
ok(b["status"] == "pending", "job B: status pending")

c = by_id[ids["c"]]
ok(c["match_score"] is None, "job C: no Application -> match_score None")
ok(c["status"] is None, "job C: no Application -> status None")
ok(c["tailored_resume_available"] is False, "job C: no resume -> False")

# ---------------------------------------------------------------------------
# 3. ?q= substring filter (case-insensitive over title + company)
# ---------------------------------------------------------------------------
print("\n[GET /api/ext/jobs — ?q=]")
r = client.get("/api/ext/jobs", headers=H, params={"q": "backend"})
titles = [j["title"] for j in r.json()["jobs"]]
ok(titles == ["Senior Backend Engineer"], f"q=backend matches title only (got {titles})")

r = client.get("/api/ext/jobs", headers=H, params={"q": "GLOBEX"})
titles = [j["title"] for j in r.json()["jobs"]]
ok(titles == ["Frontend Developer"], f"q=GLOBEX matches company case-insensitively (got {titles})")

r = client.get("/api/ext/jobs", headers=H, params={"q": "engineer"})
ok(len(r.json()["jobs"]) == 1, "q=engineer matches one")

r = client.get("/api/ext/jobs", headers=H, params={"q": "zzzznope"})
ok(r.json()["jobs"] == [], "q with no match -> empty list")

# ---------------------------------------------------------------------------
# 4. Ordering — most recently touched first.
# ---------------------------------------------------------------------------
print("\n[GET /api/ext/jobs — ordering]")
order = [j["job_id"] for j in client.get("/api/ext/jobs", headers=H).json()["jobs"]]
# A (app 1h ago) > B (app 3h ago) > C (no app -> fetched_at 5d ago).
ok(order == [ids["a"], ids["b"], ids["c"]], f"most-recent-first ordering (got {order})")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL EXT-JOBS SMOKE TESTS PASSED.")
