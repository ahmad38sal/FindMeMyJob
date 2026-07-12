"""Skill Growth Engine smoke test (SQLite, LLM mocked / forced-down).

Covers:
  - extract_skills heuristic + analyze_skill_gaps weighting (frequency, fit/
    applied target boost, gap boost) with a HEURISTIC fallback when the LLM is
    unavailable (rationales still attached).
  - Re-analyze route persists ranked SkillInsight rows; page reads them.
  - learning path + practice generation with deterministic fallback when the
    LLM is down; quiz grading is deterministic.
  - tutor session create + turn append (HTMX exchange); reply degrades to a
    clear "unavailable" message on LLM failure without 500ing.
  - mark-fluent triggers a resume suggestion; Accept writes a REAL dict entry
    into the master Profile.skills (never a JSON string).

Run with:  OPENAI_API_KEY=dummy .venv/bin/python skills_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_skills_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import findmemyjob.skills as engine  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine as db_engine  # noqa: E402
from findmemyjob.models import (  # noqa: E402
    Application, ApplicationStatus, Job, Profile, SkillInsight, SkillProficiency,
    SkillProgress, SkillTutorSession, SkillTutorTurn,
)

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


def _boom(**kwargs):
    raise RuntimeError("LLM down")


# ---------------------------------------------------------------------------
# 1. Heuristic extraction + weighted analysis (LLM DOWN -> heuristic rationales)
# ---------------------------------------------------------------------------
print("\n[extract + analyze (LLM down)]")
engine.llm.complete_with_cached_profile = _boom

ok(set(engine.extract_skills("We use Python, React.js and Kubernetes")) ==
   {"Python", "React", "Kubernetes"}, "extract_skills normalizes React.js -> React")
ok(engine.extract_skills("machine learning role") == ["Machine Learning"],
   "multi-word 'machine learning' wins over 'learning'")

PROFILE = {
    "summary": "Backend engineer.",
    "skills": [{"name": "Python"}, {"name": "SQL"}],
    "work_history": [], "education": [], "preferences": {},
}


class FakeJob:
    def __init__(self, id, title, description, fit_score=None, fit_gaps=None):
        self.id = id
        self.title = title
        self.description = description
        self.fit_score = fit_score
        self.fit_gaps = fit_gaps or []


class FakeApp:
    def __init__(self, job_id, gaps=None):
        self.job_id = job_id
        self.gaps = gaps or []


jobs = [
    FakeJob(1, "Senior React Engineer", "React, TypeScript, GraphQL, Kubernetes", fit_score=90),
    FakeJob(2, "Frontend Engineer", "React, JavaScript, Docker", fit_score=80),
    FakeJob(3, "Backend Engineer", "Python, SQL, Docker", fit_score=40),
    FakeJob(4, "Platform Engineer", "Kubernetes, Terraform, AWS", fit_score=75,
            fit_gaps=["Kubernetes", "Terraform"]),
]
apps = [FakeApp(1, gaps=["GraphQL"])]

results = engine.analyze_skill_gaps(jobs, apps, PROFILE, use_llm=True, top_n=20)
ok(len(results) > 0, "analysis returns ranked results")
ok(all(r["rationale"] for r in results), "every result has a rationale (heuristic fallback)")
ok(results[0]["rank"] == 1, "results ranked from 1")
names = [r["name"] for r in results]
ok("React" in names and "Kubernetes" in names, "high-frequency high-fit skills surface")

by_name = {r["name"]: r for r in results}
# Python & SQL are on the resume (have) -> is_gap False; React/Kubernetes are gaps.
ok(by_name["React"]["is_gap"] is True, "React flagged as gap (not on resume)")
ok(by_name.get("Python", {}).get("is_gap") in (False, None) or "Python" not in by_name,
   "Python (on resume) not flagged as a pure gap")
# GraphQL appears in application gaps -> boosted.
ok("GraphQL" in names, "GraphQL (from application gaps) is present")
# React (2 jobs, high fit, gap) should outrank Python (1 job, low fit, on resume).
ok(names.index("React") < names.index("Python") if "Python" in names else True,
   "React outranks Python (fit + gap weighting)")
ok(by_name["Kubernetes"]["appears_in_target"] is True,
   "Kubernetes appears_in_target (high-fit/applied roles)")

# ---------------------------------------------------------------------------
# 2. Learning path + practice fallback (LLM down); quiz grading deterministic
# ---------------------------------------------------------------------------
print("\n[path/practice fallback + grading]")
path = engine.generate_learning_path("React", PROFILE)
ok(path["generated_by"] == "fallback", "learning path falls back when LLM down")
ok(len(path["milestones"]) == 3, "fallback path has 3 milestones (Beginner/Intermediate/Fluent)")
ok(all(m["objectives"] for m in path["milestones"]), "each milestone has objectives")
ok(path["time_to_fluency"], "path has a time-to-fluency estimate")

practice = engine.generate_practice("React", PROFILE)
ok(practice["generated_by"] == "fallback", "practice falls back when LLM down")
ok(len(practice["quiz"]) >= 2 and practice["flashcards"] and practice["drills"],
   "fallback practice has quiz + flashcards + drills")

# All-correct answers -> 100%
correct = {i: q["answer"] for i, q in enumerate(practice["quiz"])}
graded = engine.grade_quiz(practice, correct)
ok(graded["pct"] == 100 and graded["score"] == graded["total"], "all-correct quiz grades 100%")
# All-wrong (pick an index != answer) -> 0%
wrong = {i: (q["answer"] + 1) % len(q["options"]) for i, q in enumerate(practice["quiz"])}
graded0 = engine.grade_quiz(practice, wrong)
ok(graded0["pct"] == 0, "all-wrong quiz grades 0%")

# ---------------------------------------------------------------------------
# 3. LLM-backed generation (mock) produces ai-generated content
# ---------------------------------------------------------------------------
print("\n[path/practice with mock LLM]")
def _mock_path(**kwargs):
    up = kwargs.get("user_prompt", "")
    if "path" in up.lower() or "milestone" in up.lower() or "SKILL:" in up:
        return ('{"skill":"React","time_to_fluency":"5 weeks","milestones":['
                '{"level":"Beginner","objectives":["Learn JSX"],"resources":[{"name":"React docs","kind":"docs","link":"https://react.dev"}]},'
                '{"level":"Intermediate","objectives":["Build an app"],"resources":[]},'
                '{"level":"Fluent","objectives":["Ship a project"],"resources":[]}]}')
    return "{}"


engine.llm.complete_with_cached_profile = _mock_path
path2 = engine.generate_learning_path("React", PROFILE)
ok(path2["generated_by"] == "ai" and path2["time_to_fluency"] == "5 weeks",
   "LLM-authored path parsed + normalized")

# ---------------------------------------------------------------------------
# 4. Routes: reanalyze persists, detail renders, generate, quiz, mark fluent
# ---------------------------------------------------------------------------
print("\n[routes]")
engine.llm.complete_with_cached_profile = _boom  # force heuristic/fallback everywhere

with Session(db_engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact={"name": "Ada"}, summary="Backend engineer.",
                      work_history=[], skills=[{"name": "Python"}, {"name": "SQL"}],
                      education=[], preferences={}))
    for j in [
        ("React Engineer", "React, TypeScript, Kubernetes", 90),
        ("Frontend Dev", "React, JavaScript", 80),
        ("Backend Dev", "Python, Docker", 50),
    ]:
        s.add(Job(source="pasted", source_id=f"sk|{j[0]}", title=j[0],
                  company="Acme", description=j[1], fit_score=j[2]))
    s.commit()
    aj = s.exec(select(Job)).first()
    s.add(Application(job_id=aj.id, status=ApplicationStatus.submitted, gaps=["GraphQL"]))
    s.commit()

client = TestClient(app, raise_server_exceptions=True)

r = client.get("/skills/")
ok(r.status_code == 200, f"GET /skills/ -> {r.status_code}")
ok("Re-analyze" in r.text, "skills home has Re-analyze button")

r = client.post("/skills/reanalyze", follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/reanalyze -> {r.status_code}")

with Session(db_engine) as s:
    ins = list(s.exec(select(SkillInsight).order_by(SkillInsight.rank)).all())
    ok(len(ins) > 0, f"SkillInsight rows persisted ({len(ins)})")
    ok(ins[0].rank == 1 and ins[0].rationale, "top insight ranked #1 with a rationale")
    ok(all(isinstance(i.sample_job_titles, list) for i in ins), "sample_job_titles stored as list")

r = client.get("/skills/")
ok("React" in r.text, "reanalyzed skills shown on the home page")

# Skill detail (get-or-create progress)
r = client.get("/skills/detail?name=React")
ok(r.status_code == 200, f"GET /skills/detail?name=React -> {r.status_code}")
ok("Learning path" in r.text and "Progress" in r.text, "detail page renders path + progress sections")

# Generate path + practice (fallback content persisted)
r = client.post("/skills/generate", data={"name": "React", "what": "both"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/generate -> {r.status_code}")
with Session(db_engine) as s:
    prog = s.exec(select(SkillProgress).where(SkillProgress.skill_name == "React")).first()
    ok(prog is not None and prog.learning_path and prog.practice,
       "learning_path + practice persisted on SkillProgress")
    ok(prog.proficiency == SkillProficiency.learning, "proficiency advanced to learning on generate")
    quiz_len = len(prog.practice.get("quiz", []))

# Submit quiz (all correct) -> should record a score and bump progress.
with Session(db_engine) as s:
    prog = s.exec(select(SkillProgress).where(SkillProgress.skill_name == "React")).first()
    quiz = prog.practice["quiz"]
form = {"name": "React"}
for i, q in enumerate(quiz):
    form[f"q{i}"] = str(q["answer"])
r = client.post("/skills/quiz", data=form, follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/quiz -> {r.status_code}")
with Session(db_engine) as s:
    prog = s.exec(select(SkillProgress).where(SkillProgress.skill_name == "React")).first()
    ok(prog.quiz_scores and prog.quiz_scores[-1]["pct"] == 100, "quiz score 100% recorded")
    ok(prog.progress_pct == 100, "progress_pct bumped to 100 by perfect quiz")
    # 100% >= threshold -> auto-promote to fluent + resume suggestion.
    ok(prog.proficiency == SkillProficiency.fluent, "perfect quiz auto-promotes to fluent")
    ok(prog.resume_suggested and prog.resume_suggestion, "fluency triggered a resume suggestion")

# ---------------------------------------------------------------------------
# 5. Mark-fluent via proficiency route also triggers suggestion (fresh skill)
# ---------------------------------------------------------------------------
print("\n[mark fluent + resume accept]")
r = client.post("/skills/proficiency", data={"name": "Kubernetes", "proficiency": "fluent"},
                follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/proficiency fluent -> {r.status_code}")
with Session(db_engine) as s:
    kp = s.exec(select(SkillProgress).where(SkillProgress.skill_name == "Kubernetes")).first()
    ok(kp.proficiency == SkillProficiency.fluent and kp.marked_fluent_at is not None,
       "Kubernetes marked fluent with timestamp")
    ok(kp.resume_suggested and kp.resume_suggestion.get("bullets"),
       "mark-fluent produced resume suggestion with bullets")

# Accept the resume suggestion -> writes a REAL dict into Profile.skills.
r = client.post("/skills/resume/accept", data={"name": "Kubernetes"}, follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/resume/accept -> {r.status_code}")
with Session(db_engine) as s:
    p = s.get(Profile, 1)
    ok(isinstance(p.skills, list), "Profile.skills is a real list (not a JSON string)")
    ok(all(isinstance(x, dict) for x in p.skills), "every Profile.skills entry is a real dict")
    added = [x for x in p.skills if str(x.get("name", "")).lower() == "kubernetes"]
    ok(len(added) == 1, "Kubernetes added exactly once to master profile skills")
    kp = s.exec(select(SkillProgress).where(SkillProgress.skill_name == "Kubernetes")).first()
    ok(kp.resume_applied is True, "SkillProgress.resume_applied set True after accept")

# Accept again must not duplicate the skill.
client.post("/skills/resume/accept", data={"name": "Kubernetes"}, follow_redirects=False)
with Session(db_engine) as s:
    p = s.get(Profile, 1)
    dupes = [x for x in p.skills if str(x.get("name", "")).lower() == "kubernetes"]
    ok(len(dupes) == 1, "re-accepting does not duplicate the skill entry")

# ---------------------------------------------------------------------------
# 6. Tutor: create session + append a turn; reply degrades gracefully
# ---------------------------------------------------------------------------
print("\n[tutor chat]")
r = client.post("/skills/tutor/start", data={"name": "React"}, follow_redirects=False)
ok(r.status_code == 303, f"POST /skills/tutor/start -> {r.status_code}")
tutor_url = r.headers["location"]
tsid = int(tutor_url.rstrip("/").split("/")[-1])
with Session(db_engine) as s:
    turns = list(s.exec(select(SkillTutorTurn).where(SkillTutorTurn.session_id == tsid)).all())
    ok(len(turns) == 1 and turns[0].role == "tutor", "tutor opening turn persisted")

r = client.get(tutor_url)
ok(r.status_code == 200 and "chat-log" in r.text, "tutor page renders chat log")

r = client.post(f"/skills/tutor/{tsid}/message", data={"message": "How do hooks work?"},
                headers={"hx-request": "true"})
ok(r.status_code == 200, f"POST tutor message (HTMX) -> {r.status_code}")
ok("bubble-candidate" in r.text and "bubble-interviewer" in r.text,
   "tutor exchange returns student + tutor bubbles")
ok("unavailable" in r.text.lower(), "tutor reply degrades to 'unavailable' message (LLM down)")
with Session(db_engine) as s:
    turns = list(s.exec(select(SkillTutorTurn).where(SkillTutorTurn.session_id == tsid)
                        .order_by(SkillTutorTurn.id)).all())
    ok(len(turns) == 3, "student + tutor turns appended (3 total)")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL SKILLS SMOKE TESTS PASSED.")
