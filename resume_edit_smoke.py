"""Manual resume-edit smoke test (SQLite, PDF renderer mocked, NO LLM).

Exercises the inline full-text editor for an already-tailored resume:
  - GET editor renders the current content as editable fields.
  - POST edit updates Resume.content in the DB (a one-word/bullet tweak),
    marks it manually_edited, and re-renders the PDF from the EDITED content
    (asserts the renderer is called with the edited bullet + pdf_path updated).
  - No LLM is invoked on the edit path.
  - Untouched structured metadata (skill years/evidence) is preserved.
  - Empty-content save is rejected gracefully (400, content unchanged, no 500).
  - A PDF-render failure still preserves the saved text (pdf_path unchanged).

Run with:  OPENAI_API_KEY=dummy .venv/bin/python resume_edit_smoke.py
"""
import json
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_resedit_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

import findmemyjob.llm as llm_mod  # noqa: E402
import findmemyjob.routes.jobs as jobs_routes  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.models import (  # noqa: E402
    Application, Job, Profile, Resume, ResumeKind,
)

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


CONTENT = {
    "summary": "Senior engineer with a focus on reliability.",
    "work_history": [{
        "company": "Acme", "title": "Staff Engineer", "location": "Remote",
        "start": "2019", "end": "2024",
        "bullets": ["Led migration to Kubernetes", "Reduced costs by 30%"],
        "skills": ["python"],
    }],
    "skills": [{"name": "Python", "category": "Languages", "years": 8,
                "evidence": "used daily at Acme"}],
    "education": [{"school": "MIT", "degree": "BS", "field": "CS",
                   "start": "2011", "end": "2015", "gpa": "3.9",
                   "highlights": ["Dean's list"]}],
    "keywords_targeted": ["kubernetes", "python"],
}

# Seed profile + job + tailored resume + application.
with Session(engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact={"name": "Grace Hopper"}, summary="eng",
                      work_history=[], skills=[], education=[], preferences={}))
    job = Job(source="pasted", source_id="edit|1", title="Staff Engineer",
              company="WideCo", description="Own backend systems.")
    s.add(job)
    s.commit()
    s.refresh(job)
    job_id = job.id
    resume = Resume(kind=ResumeKind.tailored, job_id=job_id, content=CONTENT,
                    keywords_targeted=CONTENT["keywords_targeted"],
                    pdf_path=f"{_tmpdir}/original.pdf")
    s.add(resume)
    s.commit()
    s.refresh(resume)
    resume_id = resume.id
    s.add(Application(job_id=job_id, tailored_resume_id=resume_id))
    s.commit()

# Mock the PDF renderer (avoids Playwright); capture the call.
pdf_calls = {"count": 0, "last": None}


def _fake_pdf(**kwargs):
    pdf_calls["count"] += 1
    pdf_calls["last"] = kwargs
    return f"{_tmpdir}/edited-{pdf_calls['count']}.pdf"


jobs_routes.save_resume_pdf = _fake_pdf

# LLM tripwire: the edit path must never call the model.
llm_calls = {"count": 0}
_orig_llm = llm_mod.llm.complete_with_cached_profile


def _tripwire(*args, **kwargs):
    llm_calls["count"] += 1
    raise AssertionError("LLM must not be called on the manual edit path")


client = TestClient(app, raise_server_exceptions=True)

# ---------------------------------------------------------------------------
# 1. GET editor renders current content
# ---------------------------------------------------------------------------
print("\n[GET editor]")
r = client.get(f"/jobs/{job_id}/resume/edit")
ok(r.status_code == 200, f"GET editor -> {r.status_code}")
ok('name="summary"' in r.text, "summary field rendered")
ok('name="wh_0_bullets"' in r.text, "bullets textarea rendered")
ok("Led migration to Kubernetes" in r.text, "existing bullet shown for editing")
ok('name="skill_0_name"' in r.text, "skill name field rendered")

# ---------------------------------------------------------------------------
# 2. POST a small manual edit -> saved, PDF regenerated from edits, no LLM
# ---------------------------------------------------------------------------
print("\n[POST edit]")
llm_mod.llm.complete_with_cached_profile = _tripwire
form = {
    "summary": "Senior engineer focused on reliability and cost.",  # word tweak
    "wh_0_title": "Staff Engineer", "wh_0_company": "Acme",
    "wh_0_location": "Remote", "wh_0_start": "2019", "wh_0_end": "2024",
    # edited first bullet (one-word change) + kept second, one per line
    "wh_0_bullets": "Led migration to GKE\nReduced costs by 30%",
    "edu_0_school": "MIT", "edu_0_degree": "BS", "edu_0_field": "CS",
    "edu_0_start": "2011", "edu_0_end": "2015", "edu_0_gpa": "3.9",
    "edu_0_highlights": "Dean's list",
    "skill_0_name": "Python", "skill_0_category": "Languages",
}
r = client.post(f"/jobs/{job_id}/resume/edit", data=form, follow_redirects=False)
llm_mod.llm.complete_with_cached_profile = _orig_llm  # restore
ok(r.status_code == 303, f"POST edit -> {r.status_code} (redirect)")
ok(llm_calls["count"] == 0, "no LLM call on the edit path")
ok(pdf_calls["count"] == 1, "PDF renderer invoked once")
# Renderer got the EDITED content.
last = pdf_calls["last"] or {}
edited_bullets = (last.get("work_history") or [{}])[0].get("bullets") or []
ok("Led migration to GKE" in edited_bullets, "PDF re-rendered from edited bullet")
ok(last.get("summary") == "Senior engineer focused on reliability and cost.",
   "PDF re-rendered from edited summary")

with Session(engine) as s:
    r2 = s.get(Resume, resume_id)
    ok(r2.content["summary"] == "Senior engineer focused on reliability and cost.",
       "edited summary persisted in DB")
    ok(r2.content["work_history"][0]["bullets"][0] == "Led migration to GKE",
       "edited bullet persisted in DB")
    ok(r2.manually_edited is True, "manually_edited flag set")
    ok(r2.pdf_path.endswith("edited-1.pdf"), "pdf_path updated to regenerated file")
    # Untouched structured metadata preserved.
    sk = r2.content["skills"][0]
    ok(sk.get("years") == 8 and sk.get("evidence") == "used daily at Acme",
       "skill years/evidence preserved (no data loss)")
    ok(r2.content["work_history"][0]["skills"] == ["python"],
       "per-role skills metadata preserved")

# ---------------------------------------------------------------------------
# 3. Empty content -> rejected gracefully, DB unchanged, no 500
# ---------------------------------------------------------------------------
print("\n[empty content rejected]")
empty_form = {"summary": "   ", "wh_0_title": "", "wh_0_company": "",
              "wh_0_location": "", "wh_0_start": "", "wh_0_end": "",
              "wh_0_bullets": "", "edu_0_school": "", "edu_0_degree": "",
              "edu_0_field": "", "edu_0_start": "", "edu_0_end": "",
              "edu_0_gpa": "", "edu_0_highlights": "",
              "skill_0_name": "", "skill_0_category": "Languages"}
pdf_before = pdf_calls["count"]
r = client.post(f"/jobs/{job_id}/resume/edit", data=empty_form, follow_redirects=False)
ok(r.status_code == 400, f"empty save -> {r.status_code} (rejected, not 500)")
ok("cannot be empty" in r.text, "friendly empty-content message shown")
ok(pdf_calls["count"] == pdf_before, "no PDF re-render on rejected empty save")
with Session(engine) as s:
    r3 = s.get(Resume, resume_id)
    ok(r3.content["summary"] == "Senior engineer focused on reliability and cost.",
       "DB content unchanged after rejected empty save")

# ---------------------------------------------------------------------------
# 4. PDF render failure -> text still saved, pdf_path unchanged
# ---------------------------------------------------------------------------
print("\n[PDF failure preserves text]")
with Session(engine) as s:
    pdf_before_path = s.get(Resume, resume_id).pdf_path


def _boom_pdf(**kwargs):
    raise RuntimeError("chromium down")


jobs_routes.save_resume_pdf = _boom_pdf
form["summary"] = "Reliability-focused staff engineer. Edited again."
r = client.post(f"/jobs/{job_id}/resume/edit", data=form, follow_redirects=False)
jobs_routes.save_resume_pdf = _fake_pdf  # restore
ok(r.status_code == 303, f"edit with failing PDF -> {r.status_code} (no 500)")
with Session(engine) as s:
    r4 = s.get(Resume, resume_id)
    ok(r4.content["summary"] == "Reliability-focused staff engineer. Edited again.",
       "text edit saved despite PDF failure")
    ok(r4.pdf_path == pdf_before_path, "pdf_path unchanged when regen fails")

# ---------------------------------------------------------------------------
# 5. PDF download route sends no-cache headers (so edits aren't stale-cached)
# ---------------------------------------------------------------------------
print("\n[download no-cache headers]")
real_pdf = os.path.join(_tmpdir, "served.pdf")
with open(real_pdf, "wb") as fh:
    fh.write(b"%PDF-1.4 dummy")
with Session(engine) as s:
    rr = s.get(Resume, resume_id)
    rr.pdf_path = real_pdf
    s.add(rr)
    s.commit()
r = client.get(f"/jobs/{job_id}/resume.pdf")
ok(r.status_code == 200, f"GET resume.pdf -> {r.status_code}")
cc = r.headers.get("cache-control", "")
ok("no-store" in cc and "no-cache" in cc and "must-revalidate" in cc and "max-age=0" in cc,
   f"Cache-Control no-store present ({cc!r})")
ok(r.headers.get("pragma") == "no-cache", "Pragma: no-cache present")
ok(r.headers.get("expires") == "0", "Expires: 0 present")
# Extra query param must still serve fine (cache-bust ?v=...).
r = client.get(f"/jobs/{job_id}/resume.pdf?v=abc123")
ok(r.status_code == 200, "resume.pdf still serves with cache-bust query param")

# ---------------------------------------------------------------------------
# 6. Legacy string content (json.dumps'd into the JSON column) edits cleanly
# ---------------------------------------------------------------------------
print("\n[string-content resume repairs on edit]")
with Session(engine) as s:
    job2 = Job(source="pasted", source_id="edit|2", title="Backend Engineer",
               company="StrCo", description="Own APIs.")
    s.add(job2)
    s.commit()
    s.refresh(job2)
    job2_id = job2.id
    # Store content as a JSON STRING (the bug): round-trips back as str.
    resume2 = Resume(kind=ResumeKind.tailored, job_id=job2_id,
                     content=json.dumps(CONTENT),
                     keywords_targeted=CONTENT["keywords_targeted"],
                     pdf_path=f"{_tmpdir}/original2.pdf")
    s.add(resume2)
    s.commit()
    s.refresh(resume2)
    resume2_id = resume2.id
    s.add(Application(job_id=job2_id, tailored_resume_id=resume2_id))
    s.commit()
    ok(isinstance(s.get(Resume, resume2_id).content, str),
       "seeded resume content is a JSON string (reproduces the bug)")

# GET editor must render the string content without erroring.
r = client.get(f"/jobs/{job2_id}/resume/edit")
ok(r.status_code == 200, f"GET editor on string content -> {r.status_code}")
ok("Led migration to Kubernetes" in r.text, "string content decoded for editing")

pdf_before = pdf_calls["count"]
form2 = {
    "summary": "Backend engineer, reliability focused.",
    "wh_0_title": "Staff Engineer", "wh_0_company": "Acme",
    "wh_0_location": "Remote", "wh_0_start": "2019", "wh_0_end": "2024",
    "wh_0_bullets": "Led migration to GKE\nReduced costs by 30%",
    "edu_0_school": "MIT", "edu_0_degree": "BS", "edu_0_field": "CS",
    "edu_0_start": "2011", "edu_0_end": "2015", "edu_0_gpa": "3.9",
    "edu_0_highlights": "Dean's list",
    "skill_0_name": "Python", "skill_0_category": "Languages",
}
r = client.post(f"/jobs/{job2_id}/resume/edit", data=form2, follow_redirects=False)
ok(r.status_code == 303, f"POST edit on string content -> {r.status_code} (no error)")
ok(pdf_calls["count"] == pdf_before + 1, "PDF regenerated for string-content resume")
with Session(engine) as s:
    r6 = s.get(Resume, resume2_id)
    ok(isinstance(r6.content, dict), "content is now a real dict (never a json string)")
    ok(r6.content["summary"] == "Backend engineer, reliability focused.",
       "edited summary persisted for repaired resume")
    ok(r6.content["work_history"][0]["bullets"][0] == "Led migration to GKE",
       "edited bullet persisted for repaired resume")
    ok(r6.manually_edited is True, "manually_edited flag set on repaired resume")
    ok(r6.pdf_path.endswith(".pdf") and "original2" not in r6.pdf_path,
       "pdf_path regenerated for repaired resume")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL RESUME-EDIT SMOKE TESTS PASSED.")
