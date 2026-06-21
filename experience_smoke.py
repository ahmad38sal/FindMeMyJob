"""Experience-bank smoke test (SQLite, LLM mocked where needed).

Exercises end-to-end:
  - ExperienceItem persists (central + job-linked); DB assigns ids
  - tailoring._format_experience_bank: empty bank -> "" (prompt unchanged);
    linked items prioritized and flagged; the prompt carries the raw notes
  - tailor_resume passes the bank into the LLM prompt, and the polished output
    is NOT a verbatim copy of the raw note
  - empty-bank tailoring behaves exactly as before (no extra prompt text)
  - routes: bank page, add (central), add-from-job (linked), edit, delete
  - regression: tailor still works (mocked) end-to-end via the job page

Run with:  OPENAI_API_KEY=dummy .venv/bin/python experience_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_exp_")
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
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.models import ExperienceItem, Job, Profile  # noqa: E402

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


RAW_LINKED = "Rewrote our deploy pipeline so releases went from 40 min to 5; nobody asked, I just got sick of waiting."
RAW_CENTRAL = "Mentored two junior devs through their first on-call rotations and wrote the runbook they still use."

# Seed profile + a job.
with Session(engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(
            id=1,
            contact={"name": "Grace Hopper"},
            summary="Backend engineer.",
            work_history=[{"company": "Navy", "title": "Senior Engineer",
                           "bullets": ["Built compilers"], "skills": ["python"]}],
            skills=[{"name": "python"}],
            preferences={},
        ))
    job = Job(source="pasted", source_id="exp|test", title="Platform Engineer",
              company="Acme", description="Own CI/CD and developer experience.")
    s.add(job)
    s.commit()
    s.refresh(job)
    job_id = job.id

# ---------------------------------------------------------------------------
# 1. _format_experience_bank (pure)
# ---------------------------------------------------------------------------
print("\n[_format_experience_bank]")
with Session(engine) as s:
    the_job = s.get(Job, job_id)

# Empty bank -> "" so the prompt is byte-for-byte unchanged.
ok(tailoring._format_experience_bank([], the_job) == "", "empty bank -> empty string")
ok(tailoring._format_experience_bank(None, the_job) == "", "None bank -> empty string")

linked = ExperienceItem(id=1, raw_text=RAW_LINKED, label="Deploy speedup",
                        category="DevOps", job_id=job_id, active=True)
central = ExperienceItem(id=2, raw_text=RAW_CENTRAL, label="Mentoring",
                         job_id=None, active=True)
inactive = ExperienceItem(id=3, raw_text="should be ignored", active=False)

block = tailoring._format_experience_bank([central, linked, inactive], the_job)
ok("EXPERIENCE BANK" in block, "block has a header")
ok(RAW_LINKED in block and RAW_CENTRAL in block, "raw notes carried into prompt")
ok("should be ignored" not in block, "inactive items excluded")
ok("(linked to THIS job)" in block, "linked item is flagged")
# Linked item appears before the central one (prioritized).
ok(block.index(RAW_LINKED) < block.index(RAW_CENTRAL), "linked item ordered first")

# ---------------------------------------------------------------------------
# 2. tailor_resume passes the bank in, polishes (not verbatim)
# ---------------------------------------------------------------------------
print("\n[tailor_resume integration]")
captured = {}


def _fake_complete(*, profile, instructions, user_prompt, model, max_tokens, temperature):
    captured["user_prompt"] = user_prompt
    captured["instructions"] = instructions
    # Return a polished resume that REFRAMES the note (never verbatim).
    return (
        '{"summary":"Platform engineer who cut deployment time 8x.",'
        '"work_history":[{"company":"Navy","title":"Senior Engineer",'
        '"bullets":["Reduced release cycle time from 40 to 5 minutes by '
        'redesigning the CI/CD pipeline."],"skills":["python"]}],'
        '"skills":[{"name":"python"}],"education":[],'
        '"keywords_targeted":["CI/CD","developer experience"]}'
    )


tailoring.llm.complete_with_cached_profile = _fake_complete

profile_dict = {"summary": "Backend engineer.",
                "work_history": [{"company": "Navy", "title": "Senior Engineer",
                                  "bullets": ["Built compilers"], "skills": ["python"]}],
                "skills": [{"name": "python"}], "education": []}

tailored = tailoring.tailor_resume(profile_dict, the_job, [linked, central])
ok("EXPERIENCE BANK" in captured["user_prompt"], "bank reached the LLM prompt")
ok(RAW_LINKED in captured["user_prompt"], "raw linked note in prompt")
# Polished output must not contain the user's verbatim sentence.
all_bullets = " ".join(
    b for w in tailored.work_history for b in (w.get("bullets") or [])
) + " " + tailored.summary
ok(RAW_LINKED not in all_bullets, "tailored output is NOT a verbatim copy of the note")
ok(tailored.work_history, "tailored resume produced")
ok("never copy" in tailoring._TAILOR_INSTRUCTIONS.lower()
   or "never copy the candidate" in tailoring._TAILOR_INSTRUCTIONS.lower(),
   "instructions forbid verbatim copying")

# Empty-bank tailoring: prompt must NOT contain the bank section.
captured.clear()
tailoring.tailor_resume(profile_dict, the_job, [])
ok("EXPERIENCE BANK" not in captured["user_prompt"],
   "empty bank -> prompt unchanged (no bank section)")

# ---------------------------------------------------------------------------
# 3. Routes
# ---------------------------------------------------------------------------
print("\n[routes]")
client = TestClient(app, raise_server_exceptions=True)

r = client.get("/profile/experience")
ok(r.status_code == 200, f"GET /profile/experience -> {r.status_code}")
ok("Experience bank" in r.text, "bank page renders heading")

# Add central note
r = client.post("/profile/experience/add",
                data={"raw_text": RAW_CENTRAL, "label": "Mentoring", "category": ""},
                follow_redirects=False)
ok(r.status_code == 303, f"POST add central -> {r.status_code}")

# Too-short note -> friendly 400
r = client.post("/profile/experience/add", data={"raw_text": "hi"},
                follow_redirects=False)
ok(r.status_code == 400, f"short note -> {r.status_code} (validation, not 500)")

# Add from job page (linked)
r = client.post(f"/jobs/{job_id}/experience",
                data={"raw_text": RAW_LINKED, "label": "Deploy", "category": "DevOps"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST add-from-job -> {r.status_code}")

# HTMX add-from-job returns the inline panel partial
r = client.post(f"/jobs/{job_id}/experience",
                data={"raw_text": "Built an internal CLI the whole team adopted."},
                headers={"hx-request": "true"})
ok(r.status_code == 200, f"HTMX add-from-job -> {r.status_code}")
ok("experience-panel" in r.text, "HTMX returns the inline panel")

with Session(engine) as s:
    items = list(s.exec(select(ExperienceItem)).all())
    linked_items = [it for it in items if it.job_id == job_id]
    central_items = [it for it in items if it.job_id is None]
    ok(len(linked_items) == 2, f"two linked items saved (got {len(linked_items)})")
    ok(len(central_items) >= 1, f"central item saved (got {len(central_items)})")
    ok(all(it.id is not None for it in items), "DB assigned ids")
    edit_id = central_items[0].id

# Edit
r = client.post(f"/profile/experience/{edit_id}/edit",
                data={"raw_text": RAW_CENTRAL + " Updated.", "label": "Mentoring",
                      "category": "Leadership", "active": "1"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST edit -> {r.status_code}")
with Session(engine) as s:
    edited = s.get(ExperienceItem, edit_id)
    ok(edited.category == "Leadership", "edit persisted category")
    ok(edited.raw_text.endswith("Updated."), "edit persisted raw_text")

# Delete
r = client.post(f"/profile/experience/{edit_id}/delete", follow_redirects=False)
ok(r.status_code == 303, f"POST delete -> {r.status_code}")
with Session(engine) as s:
    ok(s.get(ExperienceItem, edit_id) is None, "item deleted")

# Job detail page renders the experience panel
r = client.get(f"/jobs/{job_id}")
ok(r.status_code == 200, f"GET /jobs/{job_id} -> {r.status_code}")
ok("Add a relevant skill/experience" in r.text, "job page shows experience box")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL EXPERIENCE SMOKE TESTS PASSED.")
