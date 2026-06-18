"""Extended local smoke test (SQLite, no real LLM calls).

Exercises the new structured profile editor, application tracker status/notes
changes, and jobs search/pagination — verifying no route 500s on good OR bad
input.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python local_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_local_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.models import Application, ApplicationStatus, Job, Profile  # noqa: E402
from sqlmodel import Session  # noqa: E402

init_db()
client = TestClient(app, raise_server_exceptions=True)

failures = []


def check(desc, resp, expected):
    statuses = expected if isinstance(expected, (list, tuple)) else [expected]
    if resp.status_code not in statuses:
        failures.append(f"{desc}: expected {statuses}, got {resp.status_code}\n  {resp.text[:300]}")
        print(f"  FAIL {desc} -> {resp.status_code}")
        return False
    print(f"  OK   {desc} -> {resp.status_code}")
    return True


print(f"Data dir: {_tmpdir}\n")

# --- Basic pages ---
print("[basic pages]")
for path in ["/", "/profile", "/jobs", "/applications"]:
    check(f"GET {path}", client.get(path), 200)

# --- Structured profile save: GOOD input ---
print("\n[profile save - good]")
good = {
    "contact_name": "Ada Lovelace",
    "contact_email": "ada@example.com",
    "contact_location": "London",
    "summary": "Pioneering programmer.",
    "work_title": ["Analyst", "Engineer"],
    "work_company": ["Analytical Engine Co", "Babbage Labs"],
    "work_location": ["London", ""],
    "work_start": ["1842-01", "1840"],
    "work_end": ["", "1842"],
    "work_skills": ["math, algorithms", "logic"],
    "work_bullets": ["Wrote first algorithm\nDescribed looping", "Built prototypes"],
    "edu_school": ["Home tutoring"],
    "edu_degree": ["Mathematics"],
    "edu_field": ["Math"],
    "edu_start": ["1828"],
    "edu_end": ["1835"],
    "edu_gpa": ["4.0"],
    "skill_name": ["Algorithms", "Mathematics"],
    "skill_category": ["core", "core"],
    "skill_years": ["10", ""],
    "skill_evidence": ["Bernoulli numbers", ""],
    "cert_name": ["Fellow"],
    "cert_issuer": ["Royal Society"],
    "cert_earned": ["1843"],
    "cert_expires": [""],
    "pref_salary_min": "100000",
    "pref_salary_target": "150,000",
    "pref_currency": "GBP",
    "pref_seniority": "senior, staff",
    "pref_industries": "tech",
    "pref_locations": "London\nRemote",
    "pref_exclude": "BadCorp",
    "pref_work_modes": ["remote", "hybrid"],
    "pref_stretch": "55",
}
check("POST /profile/save (good)", client.post("/profile/save", data=good, follow_redirects=False), 303)

# Verify it persisted correctly
with Session(engine) as s:
    p = s.get(Profile, 1)
    assert p.contact["name"] == "Ada Lovelace", p.contact
    assert len(p.work_history) == 2, p.work_history
    assert p.work_history[0]["bullets"] == ["Wrote first algorithm", "Described looping"], p.work_history[0]
    assert p.preferences["salary_target"] == 150000, p.preferences
    assert p.preferences["stretch_slider"] == 55
    assert set(p.preferences["work_modes"]) == {"remote", "hybrid"}
    assert len(p.skills) == 2
    print("  OK   profile persisted with correct structure")

# --- Structured profile save: BAD / messy input (must NOT 500) ---
print("\n[profile save - bad input must not 500]")
# Missing name -> friendly 400, not 500
r = client.post("/profile/save", data={"contact_name": ""}, follow_redirects=False)
check("POST /profile/save (no name)", r, 400)
# Garbage numerics, empty rows, partial rows -> should be accepted (303), bad values dropped
messy = {
    "contact_name": "Test User",
    "pref_salary_min": "not-a-number",
    "pref_stretch": "abc",
    "skill_name": ["", "Python"],   # first row empty -> dropped
    "skill_years": ["xx", "five"],  # unparseable -> None
    "work_title": [""],             # empty row -> dropped
    "work_company": [""],
    "edu_gpa": ["bad"],
    "edu_school": ["X"],
    "edu_degree": ["Y"],
}
check("POST /profile/save (messy)", client.post("/profile/save", data=messy, follow_redirects=False), 303)
with Session(engine) as s:
    p = s.get(Profile, 1)
    assert p.preferences["salary_min"] is None
    assert p.preferences["stretch_slider"] == 30  # fell back to default
    assert [sk["name"] for sk in p.skills] == ["Python"], p.skills
    assert p.skills[0]["years"] is None
    assert p.work_history == []  # empty row dropped
    print("  OK   messy input sanitized, no 500")

# Verify source-config prefs preserved across structured save
print("\n[source prefs preserved]")
client.post("/profile/external/save", data={
    "external_companies": "greenhouse:stripe\nlever:cresta",
    "enable_remoteok": "1",
}, follow_redirects=False)
client.post("/profile/save", data={"contact_name": "Test User"}, follow_redirects=False)
with Session(engine) as s:
    p = s.get(Profile, 1)
    assert p.preferences.get("external_companies") == ["greenhouse:stripe", "lever:cresta"], p.preferences
    assert p.preferences.get("enable_remoteok") is True
    print("  OK   external_companies + enable_remoteok survived structured save")

# --- Applications tracker ---
print("\n[applications tracker]")
with Session(engine) as s:
    job = Job(source="manual", source_id="t1", title="SRE", company="Acme", description="x")
    s.add(job); s.commit(); s.refresh(job)
    appn = Application(job_id=job.id, status=ApplicationStatus.pending, match_score=72.0,
                       match_reasoning="Good fit", gaps=["k8s"], notes="")
    s.add(appn); s.commit(); s.refresh(appn)
    app_id = appn.id

check("GET /applications (with data)", client.get("/applications"), 200)
# Inline status change via HTMX
r = client.post(f"/applications/{app_id}/status", data={"status": "interview"},
                headers={"hx-request": "true"})
check("POST status change (htmx)", r, 200)
assert "interview" in r.text.lower()
# Non-htmx status change -> redirect
check("POST status change (redirect)", client.post(f"/applications/{app_id}/status",
      data={"status": "offer"}, follow_redirects=False), 303)
# Bad status -> 400, not 500
check("POST bad status", client.post(f"/applications/{app_id}/status",
      data={"status": "nonsense"}), 400)
# Notes save
r = client.post(f"/applications/{app_id}/notes", data={"notes": "Called recruiter"},
                headers={"hx-request": "true"})
check("POST notes (htmx)", r, 200)
with Session(engine) as s:
    a = s.get(Application, app_id)
    assert a.status == ApplicationStatus.offer
    assert a.submitted_at is None  # never went through submitted
    assert a.notes == "Called recruiter"
    print("  OK   status + notes persisted")
# Status on missing app -> 404
check("POST status missing app", client.post("/applications/99999/status",
      data={"status": "ready"}), 404)

# --- Jobs search / filter / pagination ---
print("\n[jobs search & pagination]")
with Session(engine) as s:
    for i in range(120):
        s.add(Job(source="greenhouse" if i % 2 else "lever", source_id=f"j{i}",
                  title=f"Engineer {i}", company=f"Co{i}", description="python role"))
    s.commit()
check("GET /jobs?q=engineer", client.get("/jobs?q=engineer"), 200)
check("GET /jobs?source=lever", client.get("/jobs?source=lever"), 200)
check("GET /jobs?page=2", client.get("/jobs?page=2&q=engineer"), 200)
check("GET /jobs?page=9999 (clamped)", client.get("/jobs?page=9999"), 200)
check("GET /jobs sort=salary_desc", client.get("/jobs?sort=salary_desc"), 200)
r = client.get("/jobs?q=engineer&page=1")
assert "Page 1 of" in r.text, "pagination footer missing"
assert "Next →" in r.text
print("  OK   pagination footer present")

print("\n" + ("=" * 40))
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL LOCAL SMOKE TESTS PASSED.")
