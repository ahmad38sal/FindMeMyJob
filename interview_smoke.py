"""Interview Prepper smoke test (SQLite, LLM mocked for determinism).

Exercises end-to-end:
  - engine.opening_message: produces an opener (mocked LLM) and falls back
    gracefully when the LLM raises.
  - engine.process_answer: returns inline feedback + a follow-up question;
    round progression is deterministic (advances after the per-round budget);
    job context + experience bank reach the prompt; short/empty answers are
    handled without raising.
  - engine.build_debrief: produces per-round + overall scores, strengths, gaps;
    falls back deterministically from per-answer scores when the LLM is down.
  - routes: start a session for a job (DB-assigned ids), render the chat page,
    submit answers via HTMX through ALL FOUR rounds to completion, get the
    debrief, and confirm the page includes Web Speech voice controls. Empty
    answer -> no 500. Existing job page renders the "Prep for interview" button.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python interview_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_interview_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import findmemyjob.interview as engine  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine as db_engine  # noqa: E402
from findmemyjob.models import (  # noqa: E402
    ExperienceItem, InterviewSession, InterviewTurn, Job, Profile,
)

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


PROFILE = {
    "summary": "Senior backend engineer, 8 years.",
    "work_history": [{"company": "Acme", "title": "Senior Engineer",
                      "start": "2016-01-01", "end": None,
                      "bullets": ["Built distributed systems"], "skills": ["python", "go"]}],
    "skills": [{"name": "python"}, {"name": "go"}],
    "education": [],
    "preferences": {"salary_target": 180000, "work_modes": ["remote"]},
}

EXP_NOTE = "Rewrote our deploy pipeline; releases went from 40 min to 5."


# Captured prompts so we can assert job context + bank reach the LLM.
captured = {"prompts": []}


def _mock_interviewer(*, profile, instructions, user_prompt, model, max_tokens, temperature):
    captured["prompts"].append(user_prompt)
    if "Start the interview now" in user_prompt:
        return '{"next_message": "Hi! Thanks for joining for the Staff Engineer role at WideCo. Walk me through your background."}'
    if "Write the debrief" in user_prompt:
        return (
            '{"overall_score": 78, "summary": "Solid across the board.",'
            '"round_scores": ['
            '{"round":"recruiter","score":80,"note":"Clear."},'
            '{"round":"behavioral","score":75,"note":"Good STAR."},'
            '{"round":"technical","score":76,"note":"Reasoned well."},'
            '{"round":"company","score":82,"note":"Researched."}],'
            '"strengths":["Clear communication","Concrete examples"],'
            '"gaps":["Quantify impact more"],'
            '"better_answers":[{"question":"Why us?","reframe":"Tie mission to your work."}]}'
        )
    # A normal turn: feedback + next question.
    return (
        '{"feedback": {"worked": "You gave a concrete example.",'
        '"improve": "Add a measurable result.",'
        '"stronger": "Frame it as Situation-Action-Result."},'
        '"score": 72, "next_message": "Got it — and tell me more. Next question for this round?"}'
    )


engine.llm.complete_with_cached_profile = _mock_interviewer

# ---------------------------------------------------------------------------
# 1. opening_message
# ---------------------------------------------------------------------------
print("\n[opening_message]")
job = Job(id=1, source="pasted", source_id="iv|1", title="Staff Engineer",
          company="WideCo", description="Own backend systems. Python, Go, distributed.")
items = [ExperienceItem(id=1, raw_text=EXP_NOTE, label="Deploy speedup", job_id=1, active=True)]

opener = engine.opening_message(PROFILE, job, items)
ok("WideCo" in opener or "background" in opener, "opener generated from mock")
ok(any("EXPERIENCE BANK" in p for p in captured["prompts"]), "opener prompt carries experience bank")
ok(any("Staff Engineer" in p for p in captured["prompts"]), "opener prompt carries job title")


def _boom(**kwargs):
    raise RuntimeError("LLM down")


engine.llm.complete_with_cached_profile = _boom
opener_fb = engine.opening_message(PROFILE, job, items)
ok(opener_fb == engine._FALLBACK_OPENERS["recruiter"], "opener falls back when LLM raises")
engine.llm.complete_with_cached_profile = _mock_interviewer

# ---------------------------------------------------------------------------
# 2. process_answer + round progression
# ---------------------------------------------------------------------------
print("\n[process_answer / rounds]")
sess = InterviewSession(id=1, job_id=1, current_round="recruiter",
                        config={"questions_per_round": 2})

# First answer of recruiter round (1 of 2) -> stays in recruiter.
prior = [InterviewTurn(id=1, session_id=1, role="interviewer", round="recruiter",
                       content="Walk me through your background.")]
res = engine.process_answer(PROFILE, job, sess, prior, "I'm a senior backend eng.", items)
ok(res["feedback"]["worked"], "answer 1: inline feedback returned")
ok(isinstance(res["score"], int) and 0 <= res["score"] <= 100, "answer 1: score in range")
ok(res["next_round"] == "recruiter" and not res["complete"], "answer 1: still in recruiter round")
ok(res["next_message"], "answer 1: follow-up question produced")
ok(any(EXP_NOTE in p for p in captured["prompts"]), "process prompt carries experience bank note")

# Second answer of recruiter (2 of 2) -> advances to behavioral.
prior += [
    InterviewTurn(id=2, session_id=1, role="candidate", round="recruiter", content="..."),
    InterviewTurn(id=3, session_id=1, role="interviewer", round="recruiter", content="salary?"),
]
res2 = engine.process_answer(PROFILE, job, sess, prior, "Targeting 180k, available in 4 weeks.", items)
ok(res2["next_round"] == "behavioral", "answer 2: advances recruiter -> behavioral")
ok(not res2["complete"], "answer 2: not complete")

# advance_round helper covers the full chain and the terminal None.
ok(engine.advance_round("recruiter") == "behavioral", "advance recruiter->behavioral")
ok(engine.advance_round("behavioral") == "technical", "advance behavioral->technical")
ok(engine.advance_round("technical") == "company", "advance technical->company")
ok(engine.advance_round("company") is None, "advance company->None (end)")

# Final round, final answer -> complete=True, no next question.
sess_final = InterviewSession(id=2, job_id=1, current_round="company",
                              config={"questions_per_round": 1})
res3 = engine.process_answer(PROFILE, job, sess_final, [], "Because the mission fits me.", items)
ok(res3["complete"] and res3["next_message"] is None, "final answer: complete, no next question")
ok(res3["feedback"], "final answer: still gets inline feedback")

# Empty answer must not raise.
res_empty = engine.process_answer(PROFILE, job, sess, prior, "", items)
ok(isinstance(res_empty, dict), "empty answer handled without raising")

# ---------------------------------------------------------------------------
# 3. build_debrief
# ---------------------------------------------------------------------------
print("\n[build_debrief]")
turns = [
    InterviewTurn(id=10, session_id=3, role="interviewer", round="recruiter", content="q1"),
    InterviewTurn(id=11, session_id=3, role="candidate", round="recruiter", content="a1", feedback={"score": 80}),
    InterviewTurn(id=12, session_id=3, role="candidate", round="behavioral", content="a2", feedback={"score": 70}),
]
deb = engine.build_debrief(PROFILE, job, turns)
ok(deb["overall_score"] == 78, "debrief: overall score from mock LLM")
ok(len(deb["round_scores"]) == 4, "debrief: four round scores")
ok(deb["strengths"] and deb["gaps"], "debrief: strengths + gaps present")
ok(deb["better_answers"], "debrief: better answers present")

# Fallback debrief when the LLM is down: derives from per-answer scores.
engine.llm.complete_with_cached_profile = _boom
deb_fb = engine.build_debrief(PROFILE, job, turns)
ok(deb_fb["overall_score"] == 75, "debrief fallback: averages per-answer scores (80,70)->75")
ok(len(deb_fb["round_scores"]) >= 1, "debrief fallback: has round scores")
engine.llm.complete_with_cached_profile = _mock_interviewer

# ---------------------------------------------------------------------------
# 4. Routes — full interview to completion via HTMX
# ---------------------------------------------------------------------------
print("\n[routes]")
with Session(db_engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact={"name": "Grace Hopper"}, summary=PROFILE["summary"],
                      work_history=PROFILE["work_history"], skills=PROFILE["skills"],
                      education=[], preferences=PROFILE["preferences"]))
    rj = Job(source="pasted", source_id="iv|route", title="Staff Engineer", company="WideCo",
             description="Own backend systems. Python, Go, distributed.")
    s.add(rj)
    s.add(ExperienceItem(raw_text=EXP_NOTE, label="Deploy", active=True))
    s.commit()
    s.refresh(rj)
    route_job_id = rj.id

client = TestClient(app, raise_server_exceptions=True)

# Job page shows the button.
r = client.get(f"/jobs/{route_job_id}")
ok(r.status_code == 200, f"GET job page -> {r.status_code}")
ok("Prep for interview" in r.text, "job page shows Prep for interview button")

# Start a session.
r = client.post(f"/interview/start/{route_job_id}", follow_redirects=False)
ok(r.status_code == 303, f"POST start -> {r.status_code}")
sess_url = r.headers["location"]
session_id = int(sess_url.rstrip("/").split("/")[-1])
ok(session_id > 0, f"DB assigned session id {session_id}")

with Session(db_engine) as s:
    t = list(s.exec(select(InterviewTurn).where(InterviewTurn.session_id == session_id)).all())
    ok(len(t) == 1 and t[0].role == "interviewer", "opener interviewer turn persisted")

# Chat page renders with voice controls.
r = client.get(sess_url)
ok(r.status_code == 200, f"GET chat page -> {r.status_code}")
ok("chat-log" in r.text, "chat page renders chat log")
ok("webkitSpeechRecognition" in r.text and "speechSynthesis" in r.text,
   "chat page includes Web Speech (STT + TTS) controls")
ok("tts-toggle" in r.text and "mic-btn" in r.text, "voice toggle + mic button present")
ok("answer-form" in r.text, "answer form present (typing always works)")

# Submit answers via HTMX until complete. 4 rounds x 2 questions = 8 answers.
complete_seen = False
for i in range(12):  # cap to avoid an infinite loop on a bug
    r = client.post(f"/interview/{session_id}/answer",
                    data={"answer": f"Concrete answer number {i} with a result."},
                    headers={"hx-request": "true"})
    ok(r.status_code == 200, f"answer {i} -> {r.status_code}") if i == 0 else None
    if "data-interview-complete" in r.text or "Debrief" in r.text:
        complete_seen = True
        break
ok(complete_seen, "interview reaches completion + debrief via HTMX")
ok("Coaching" in r.text or "Debrief" in r.text, "exchange/debrief fragment rendered")

with Session(db_engine) as s:
    fin = s.get(InterviewSession, session_id)
    ok(fin.status == "completed", "session marked completed")
    ok(fin.debrief and "overall_score" in fin.debrief, "debrief stored on session")
    cand_turns = list(s.exec(select(InterviewTurn).where(
        InterviewTurn.session_id == session_id,
        InterviewTurn.role == "candidate")).all())
    ok(all(ct.feedback for ct in cand_turns), "every candidate turn has inline feedback")
    rounds_hit = {ct.round for ct in cand_turns}
    ok({"recruiter", "behavioral", "technical", "company"}.issubset(rounds_hit),
       f"all four rounds were exercised (got {rounds_hit})")

# Empty answer to a fresh session -> no 500.
r = client.post(f"/interview/start/{route_job_id}", follow_redirects=False)
sid2 = int(r.headers["location"].rstrip("/").split("/")[-1])
r = client.post(f"/interview/{sid2}/answer", data={"answer": ""}, headers={"hx-request": "true"})
ok(r.status_code == 200, f"empty answer -> {r.status_code} (no 500)")

# Past sessions now listed on the job page.
r = client.get(f"/jobs/{route_job_id}")
ok("Past sessions" in r.text, "job page lists past interview sessions")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL INTERVIEW SMOKE TESTS PASSED.")
