"""Paste-a-job smoke test (SQLite, LLM mocked).

Exercises end-to-end:
  - shared extractor build_job_from_text(text, url=None) builds a Job from raw
    text (source="pasted"), reusing the generic_url LLM-extract path
  - too-short / empty text -> ValueError (friendly 400), never a 500
  - POST /jobs/paste extracts, saves (ORM autoincrement id), auto-scores, and
    redirects to the job detail page
  - re-pasting the same posting dedupes (no duplicate row)
  - add-by-url still works (shared extractor refactor didn't break it)

Run with:  OPENAI_API_KEY=dummy .venv/bin/python paste_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_paste_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import findmemyjob.sources.generic_url as generic_url  # noqa: E402
import findmemyjob.routes.jobs as jobs_routes  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.matching import ScoreResult  # noqa: E402
from findmemyjob.models import Application, Job, Profile  # noqa: E402

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


SAMPLE = """\
Senior Backend Engineer
Acme Corp — Remote (US)

We're hiring a Senior Backend Engineer to build distributed systems in Python
and Go. You'll own services end to end, mentor engineers, and work with
Kubernetes. Compensation: $170,000–$200,000 USD. Fully remote.

Requirements: 5+ years backend, Python, Go, Kubernetes, distributed systems.
"""

# Deterministic extractor output (mock the shared LLM extract).
_EXTRACTED = {
    "title": "Senior Backend Engineer",
    "company": "Acme Corp",
    "team": "Platform",
    "location": "Remote (US)",
    "work_mode": "remote",
    "salary_min": 170000,
    "salary_max": 200000,
    "currency": "USD",
    "seniority": "Senior",
    "description": "Build distributed systems in Python and Go.",
}
generic_url._llm_extract = lambda text, url=None: dict(_EXTRACTED)

# Seed a profile so auto-scoring has something to match against.
with Session(engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(
            id=1,
            contact={"name": "Grace Hopper", "email": "grace@example.com"},
            summary="Backend engineer, distributed systems.",
            work_history=[{"company": "Navy", "title": "Senior Backend Engineer",
                           "bullets": ["Built compilers"], "skills": ["python", "go"]}],
            skills=[{"name": "python"}, {"name": "go"}, {"name": "kubernetes"}],
            preferences={"work_modes": ["remote"], "salary_target": 180000},
        ))
        s.commit()

# ---------------------------------------------------------------------------
# 1. Shared extractor (pure)
# ---------------------------------------------------------------------------
print("\n[build_job_from_text]")
job = generic_url.build_job_from_text(SAMPLE, url=None)
ok(job.source == "pasted", "pasted job has source='pasted'")
ok(job.title == "Senior Backend Engineer", "title extracted")
ok(job.company == "Acme Corp", "company extracted")
ok(job.work_mode == "remote", "work_mode extracted")
ok(job.salary_max == 200000, "salary extracted")
ok(job.url is None, "no url when none pasted")
ok(job.source_id == "senior backend engineer|acme corp", "dedup key = title|company")

job_with_url = generic_url.build_job_from_text(SAMPLE, url="https://linkedin.com/jobs/123")
ok(job_with_url.url == "https://linkedin.com/jobs/123", "pasted url kept")
ok(job_with_url.source_id == "https://linkedin.com/jobs/123", "url is dedup key when given")

# Too-short input -> ValueError (no crash)
print("\n[validation]")
try:
    generic_url.build_job_from_text("too short", url=None)
    ok(False, "short text raises ValueError")
except ValueError:
    ok(True, "short text raises ValueError")

# ---------------------------------------------------------------------------
# 2. Route: POST /jobs/paste
# ---------------------------------------------------------------------------
print("\n[POST /jobs/paste]")
# Mock scoring at the route module so auto-score is deterministic.
jobs_routes.score_job = lambda profile_dict, job: ScoreResult(
    score=88.0, reasoning="strong python/go match", gaps=[],
    stretch_required=False, matched_skills=["python", "go"])
jobs_routes.prefilter = lambda profile_dict, job: None

client = TestClient(app, raise_server_exceptions=True)

r = client.post("/jobs/paste", data={"text": SAMPLE, "url": ""}, follow_redirects=False)
ok(r.status_code == 303, f"POST /jobs/paste -> {r.status_code} (redirect)")
loc = r.headers.get("location", "")
ok(loc.startswith("/jobs/"), f"redirects to job detail ({loc})")

with Session(engine) as s:
    saved = s.exec(select(Job).where(Job.source == "pasted")).all()
    ok(len(saved) == 1, f"exactly one pasted job saved (got {len(saved)})")
    pj = saved[0]
    ok(pj.id is not None, "DB assigned an id (no manual id)")
    ok(pj.title == "Senior Backend Engineer", "saved job has extracted title")
    appn = s.exec(select(Application).where(Application.job_id == pj.id)).first()
    ok(appn is not None and appn.match_score == 88.0, "auto-scored on paste")

# Detail page renders
r = client.get(loc)
ok(r.status_code == 200, f"GET {loc} -> {r.status_code}")
ok("Senior Backend Engineer" in r.text, "detail page shows pasted job")

# Idempotency: re-paste same posting -> no duplicate
r = client.post("/jobs/paste", data={"text": SAMPLE, "url": ""}, follow_redirects=False)
ok(r.status_code == 303, "re-paste also redirects")
with Session(engine) as s:
    again = s.exec(select(Job).where(Job.source == "pasted")).all()
    ok(len(again) == 1, f"re-paste dedupes, still one row (got {len(again)})")

# Empty / too-short text -> friendly 400, not 500
r = client.post("/jobs/paste", data={"text": "nope", "url": ""}, follow_redirects=False)
ok(r.status_code == 400, f"short paste -> {r.status_code} (validation, not 500)")

# ---------------------------------------------------------------------------
# 3. add-by-url still works (shared refactor didn't break it)
# ---------------------------------------------------------------------------
print("\n[add-by-url regression]")
jobs_routes._fetch_one_by_url = lambda url: Job(
    source="generic", source_id=url, title="URL Job", company="Beta",
    description="x", url=url)
r = client.post("/jobs/add-by-url", data={"url": "https://example.com/job/1"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST /jobs/add-by-url -> {r.status_code}")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL PASTE SMOKE TESTS PASSED.")
