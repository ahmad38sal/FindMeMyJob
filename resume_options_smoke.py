"""Per-resume tailor options smoke test (SQLite, LLM + PDF mocked).

Exercises the two tailor-screen options — summary toggle (default ON) and
page-length control (Automatic default / 1 page / 2 pages):

  - _options_block: default (summary on, auto) -> "" so the prompt is
    byte-for-byte unchanged; summary-off adds an omit instruction; "1"/"2"
    add the length guidance.
  - tailor_resume: default path prompt carries NO "OUTPUT OPTIONS" block
    (no regression); include_summary=False -> output summary forced empty +
    prompt instructs omission; include_summary=True keeps a summary.
  - page_length="1" trims content (fewer bullets/roles) vs "auto"; "auto"
    is unchanged.
  - routes: the tailor screen renders the checkbox (checked) + page select
    (Automatic default); POST /jobs/{id}/tailor with options persists
    include_summary + page_length on the Resume; defaults map to on + auto.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python resume_options_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_resopt_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import findmemyjob.tailoring as tailoring  # noqa: E402
import findmemyjob.routes.jobs as jobs_routes  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.models import Job, Profile, Resume  # noqa: E402

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


PROFILE = {
    "summary": "Backend engineer, 8 years.",
    "work_history": [{"company": "Navy", "title": "Senior Engineer",
                      "bullets": ["Built compilers"], "skills": ["python"]}],
    "skills": [{"name": "python"}],
    "education": [],
    "contact": {"name": "Grace Hopper"},
}

# A tailored reply with a summary + 3 roles x 6 bullets so trimming is visible.
_ROLE = lambda co, ti: {
    "company": co, "title": ti,
    "bullets": [f"Bullet {i} for {co}" for i in range(6)],
    "skills": ["python"],
}
_FULL_JSON = (
    '{"summary":"Seasoned backend engineer who ships reliable systems.",'
    '"work_history":['
    + ",".join(
        __import__("json").dumps(_ROLE(co, ti))
        for co, ti in [("Navy", "Senior Engineer"), ("Acme", "Engineer"),
                       ("Beta", "Engineer"), ("Gamma", "Engineer"),
                       ("Delta", "Engineer")]
    )
    + '],"skills":[{"name":"python"}],"education":[],'
    '"keywords_targeted":["python","distributed"]}'
)

captured = {}


def _fake_complete(*, profile, instructions, user_prompt, model, max_tokens, temperature):
    captured["user_prompt"] = user_prompt
    return _FULL_JSON


tailoring.llm.complete_with_cached_profile = _fake_complete

job = Job(id=1, source="pasted", source_id="ro|1", title="Staff Engineer",
          company="WideCo", description="Own backend systems. Python, distributed.")

# ---------------------------------------------------------------------------
# 1. _options_block (pure)
# ---------------------------------------------------------------------------
print("\n[_options_block]")
ok(tailoring._options_block(True, "auto") == "",
   "default (summary on, auto) -> empty block (prompt unchanged)")
off = tailoring._options_block(False, "auto")
ok("Do NOT include a professional summary" in off, "summary-off adds omit instruction")
one = tailoring._options_block(True, "1")
ok("ONE-PAGE" in one, "page_length=1 adds one-page guidance")
two = tailoring._options_block(True, "2")
ok("TWO PAGES" in two, "page_length=2 adds two-page guidance")

# ---------------------------------------------------------------------------
# 2. tailor_resume: default path unchanged, summary toggle enforced
# ---------------------------------------------------------------------------
print("\n[tailor_resume options]")
captured.clear()
res_default = tailoring.tailor_resume(PROFILE, job)  # defaults: summary on, auto
ok("OUTPUT OPTIONS" not in captured["user_prompt"],
   "default path prompt has NO options block (no regression)")
ok(res_default.summary, "default keeps a summary")
ok(len(res_default.work_history) == 5, "auto keeps all 5 roles")
ok(len(res_default.work_history[0]["bullets"]) == 6, "auto keeps all bullets")

captured.clear()
res_nosum = tailoring.tailor_resume(PROFILE, job, include_summary=False)
ok("Do NOT include a professional summary" in captured["user_prompt"],
   "summary-off prompt instructs omission")
ok(res_nosum.summary == "", "summary-off forces empty summary in output")

captured.clear()
res_sum = tailoring.tailor_resume(PROFILE, job, include_summary=True)
ok(res_sum.summary != "", "summary-on keeps the summary")

# ---------------------------------------------------------------------------
# 3. page_length trimming: "1" produces shorter content than "auto"
# ---------------------------------------------------------------------------
print("\n[page_length trimming]")
captured.clear()
res_one = tailoring.tailor_resume(PROFILE, job, page_length="1")
ok("ONE-PAGE" in captured["user_prompt"], "one-page prompt carries brevity guidance")
ok(len(res_one.work_history) <= 4, "one-page caps number of roles (<=4)")
ok(len(res_one.work_history[0]["bullets"]) <= 4, "one-page caps first-role bullets (<=4)")
auto_bullets = sum(len(r["bullets"]) for r in res_default.work_history)
one_bullets = sum(len(r["bullets"]) for r in res_one.work_history)
ok(one_bullets < auto_bullets, f"one-page has fewer bullets than auto ({one_bullets} < {auto_bullets})")

captured.clear()
res_two = tailoring.tailor_resume(PROFILE, job, page_length="2")
ok(len(res_two.work_history) == 5, "two-page does not trim roles")

# Invalid page_length falls back to auto (no trim, no options block).
captured.clear()
res_bad = tailoring.tailor_resume(PROFILE, job, page_length="7")
ok("OUTPUT OPTIONS" not in captured["user_prompt"], "invalid length -> treated as auto")
ok(len(res_bad.work_history) == 5, "invalid length -> no trimming")

# ---------------------------------------------------------------------------
# 4. Routes: controls render + options persist on the Resume
# ---------------------------------------------------------------------------
print("\n[routes]")
# Avoid Playwright: stub the PDF renderer to a fake path.
jobs_routes.save_resume_pdf = lambda **kwargs: f"{_tmpdir}/fake.pdf"
# Keep cover-letter generation deterministic (shares the llm singleton mock).

with Session(engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact=PROFILE["contact"], summary=PROFILE["summary"],
                      work_history=PROFILE["work_history"], skills=PROFILE["skills"],
                      education=[], preferences={}))
    rj = Job(source="pasted", source_id="ro|route", title="Staff Engineer",
             company="WideCo", description="Own backend systems. Python, distributed.")
    s.add(rj)
    s.commit()
    s.refresh(rj)
    route_job_id = rj.id

client = TestClient(app, raise_server_exceptions=True)

# Tailor screen renders the controls with correct defaults.
r = client.get(f"/jobs/{route_job_id}")
ok(r.status_code == 200, f"GET job page -> {r.status_code}")
ok('name="include_summary"' in r.text and "checked" in r.text,
   "tailor screen renders summary checkbox (checked)")
ok('name="page_length"' in r.text and '<option value="auto" selected' in r.text,
   "tailor screen renders page-length select (Automatic default)")

# Tailor with summary OFF + 1 page -> persisted on the Resume.
r = client.post(f"/jobs/{route_job_id}/tailor",
                data={"page_length": "1"},  # checkbox omitted -> summary off
                follow_redirects=False)
ok(r.status_code == 303, f"POST tailor (summary off, 1pg) -> {r.status_code}")
with Session(engine) as s:
    resume = s.exec(select(Resume).where(Resume.job_id == route_job_id)
                    .order_by(Resume.id.desc())).first()
    ok(resume is not None, "resume row created")
    ok(resume.include_summary is False, "include_summary=False persisted")
    ok(resume.page_length == "1", "page_length='1' persisted")
    ok(resume.content.get("summary", "") == "", "stored content has empty summary")

# Tailor with defaults (checkbox on, auto) -> summary on + auto persisted.
r = client.post(f"/jobs/{route_job_id}/tailor",
                data={"include_summary": "1", "page_length": "auto"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST tailor (defaults) -> {r.status_code}")
with Session(engine) as s:
    resume = s.exec(select(Resume).where(Resume.job_id == route_job_id)
                    .order_by(Resume.id.desc())).first()
    ok(resume.include_summary is True, "default include_summary=True persisted")
    ok(resume.page_length == "auto", "default page_length='auto' persisted")
    ok(resume.content.get("summary", "") != "", "default keeps a summary in content")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL RESUME-OPTIONS SMOKE TESTS PASSED.")
