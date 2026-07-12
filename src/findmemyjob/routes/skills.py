"""Skill Growth Engine routes.

Read paths only render persisted data (fast, safe on Gemini 401). The heavy LLM
work lives in explicit POST actions — Re-analyze, generate path/practice, tutor
replies — each degrading to heuristics/canned content rather than 500ing.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, delete, select

from findmemyjob import skills as engine
from findmemyjob.db import get_session
from findmemyjob.models import (
    Application,
    Job,
    Profile,
    SkillInsight,
    SkillProficiency,
    SkillProgress,
    SkillTutorSession,
    SkillTutorTurn,
)

router = APIRouter()

# Auto-promote to fluent when a quiz is aced at/above this percent.
_FLUENT_QUIZ_THRESHOLD = 80


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _profile_dict(session: Session) -> Dict:
    profile = session.get(Profile, 1)
    if profile is None:
        # Skills analysis works off jobs even without a profile; use an empty one.
        return {}
    return profile.model_dump()


def _get_or_create_progress(session: Session, name: str) -> SkillProgress:
    prog = session.exec(
        select(SkillProgress).where(SkillProgress.skill_name == name)
    ).first()
    if prog is None:
        prog = SkillProgress(skill_name=name)
        session.add(prog)
        session.commit()
        session.refresh(prog)
    return prog


def _progress_by_name(session: Session) -> Dict[str, SkillProgress]:
    return {p.skill_name: p for p in session.exec(select(SkillProgress)).all()}


# ---------------------------------------------------------------------------
# Dashboard + re-analyze
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def skills_home(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    insights = session.exec(
        select(SkillInsight).order_by(SkillInsight.rank)
    ).all()
    progress = _progress_by_name(session)
    computed_at = insights[0].computed_at if insights else None
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "skills.html",
        {
            "insights": insights,
            "progress": progress,
            "computed_at": computed_at,
        },
    )


@router.post("/reanalyze")
def reanalyze(request: Request, session: Session = Depends(get_session)):
    """Recompute the weighted skill-gap analysis and replace SkillInsight rows."""
    jobs = list(session.exec(select(Job)).all())
    applications = list(session.exec(select(Application)).all())
    profile = _profile_dict(session)
    results = engine.analyze_skill_gaps(jobs, applications, profile, use_llm=True, top_n=20)

    try:
        session.exec(delete(SkillInsight))
        now = datetime.utcnow()
        for r in results:
            session.add(SkillInsight(
                name=r["name"],
                frequency=r["frequency"],
                weighted_score=r["weighted_score"],
                appears_in_target=r["appears_in_target"],
                is_gap=r["is_gap"],
                sample_job_titles=r["sample_job_titles"],
                rationale=r["rationale"],
                rank=r["rank"],
                computed_at=now,
            ))
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[skills] reanalyze persist failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save the analysis. Please try again.")

    return RedirectResponse(url="/skills", status_code=303)


# ---------------------------------------------------------------------------
# Skill detail: learning path + practice + progress
# ---------------------------------------------------------------------------

@router.get("/detail", response_class=HTMLResponse)
def skill_detail(
    request: Request,
    name: str,
    session: Session = Depends(get_session),
):
    if not name.strip():
        raise HTTPException(400, "Missing skill name")
    prog = _get_or_create_progress(session, name.strip())
    insight = session.exec(
        select(SkillInsight).where(SkillInsight.name == name.strip())
    ).first()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "skill_detail.html",
        {
            "name": name.strip(),
            "prog": prog,
            "insight": insight,
            "proficiencies": [p.value for p in SkillProficiency],
        },
    )


@router.post("/generate")
def generate_content(
    request: Request,
    name: str = Form(...),
    what: str = Form("both"),  # path | practice | both
    session: Session = Depends(get_session),
):
    """Generate (or regenerate) the learning path and/or practice for a skill."""
    name = name.strip()
    prog = _get_or_create_progress(session, name)
    profile = _profile_dict(session)
    if what in ("path", "both"):
        prog.learning_path = engine.generate_learning_path(name, profile)
    if what in ("practice", "both"):
        prog.practice = engine.generate_practice(name, profile)
    if prog.proficiency == SkillProficiency.not_started:
        prog.proficiency = SkillProficiency.learning
    prog.updated_at = datetime.utcnow()
    session.add(prog)
    session.commit()
    return RedirectResponse(url=f"/skills/detail?name={_q(name)}", status_code=303)


@router.post("/quiz")
async def submit_quiz(
    request: Request,
    session: Session = Depends(get_session),
):
    """Grade the submitted quiz, store the score, advance proficiency/progress.

    Answers arrive as form fields q0, q1, ... holding the chosen option index.
    """
    formdata = await request.form()
    name = str(formdata.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Missing skill name")
    prog = _get_or_create_progress(session, name)
    practice = prog.practice or {}
    answers: Dict[int, int] = {}
    for key in formdata.keys():
        if key.startswith("q") and key[1:].isdigit():
            try:
                answers[int(key[1:])] = int(formdata.get(key))
            except (TypeError, ValueError):
                continue

    result = engine.grade_quiz(practice, answers)
    scores = list(prog.quiz_scores or [])
    scores.append({
        "pct": result["pct"], "score": result["score"], "total": result["total"],
        "at": datetime.utcnow().isoformat(),
    })
    prog.quiz_scores = scores
    prog.progress_pct = max(prog.progress_pct or 0, result["pct"])
    if prog.proficiency in (SkillProficiency.not_started, SkillProficiency.learning):
        prog.proficiency = SkillProficiency.practicing
    prog.updated_at = datetime.utcnow()

    if result["pct"] >= _FLUENT_QUIZ_THRESHOLD and prog.proficiency != SkillProficiency.fluent:
        _promote_to_fluent(session, prog, _profile_dict(session))

    session.add(prog)
    session.commit()
    return RedirectResponse(url=f"/skills/detail?name={_q(name)}", status_code=303)


@router.post("/proficiency")
def set_proficiency(
    request: Request,
    name: str = Form(...),
    proficiency: str = Form(...),
    session: Session = Depends(get_session),
):
    name = name.strip()
    prog = _get_or_create_progress(session, name)
    try:
        new_p = SkillProficiency(proficiency)
    except ValueError:
        raise HTTPException(400, "Unknown proficiency")
    prog.proficiency = new_p
    prog.updated_at = datetime.utcnow()
    if new_p == SkillProficiency.fluent:
        _promote_to_fluent(session, prog, _profile_dict(session))
    session.add(prog)
    session.commit()
    return RedirectResponse(url=f"/skills/detail?name={_q(name)}", status_code=303)


def _promote_to_fluent(session: Session, prog: SkillProgress, profile: Dict) -> None:
    """Mark fluent + generate a resume suggestion (once). Never raises."""
    prog.proficiency = SkillProficiency.fluent
    if prog.marked_fluent_at is None:
        prog.marked_fluent_at = datetime.utcnow()
    prog.progress_pct = 100
    if not prog.resume_suggested:
        try:
            prog.resume_suggestion = engine.suggest_resume_edits(prog.skill_name, profile)
        except Exception as e:  # noqa: BLE001
            print(f"[skills] resume suggestion failed: {e}")
            prog.resume_suggestion = engine._fallback_resume_suggestion(prog.skill_name)
        prog.resume_suggested = True
    prog.updated_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Resume loop
# ---------------------------------------------------------------------------

@router.post("/resume/accept")
def resume_accept(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Write the suggested skill into the master Profile skills (a real dict)."""
    name = name.strip()
    prog = _get_or_create_progress(session, name)
    suggestion = prog.resume_suggestion or {}
    entry = suggestion.get("skill_entry") or {"name": name, "category": "Other"}
    bullets = suggestion.get("bullets") or []

    profile = session.get(Profile, 1)
    if profile is None:
        raise HTTPException(400, "Profile not set up — visit /profile first.")

    # Normalize skills to a real list of dicts (defensive against str storage).
    existing = profile.skills
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except (ValueError, TypeError):
            existing = []
    if not isinstance(existing, list):
        existing = []

    have = {
        (s.get("name") if isinstance(s, dict) else str(s) or "").strip().lower()
        for s in existing
    }
    entry_name = str(entry.get("name") or name).strip()
    new_entry = {
        "name": entry_name,
        "category": str(entry.get("category") or "Other"),
        "evidence": " ".join(str(b) for b in bullets)[:600],
    }
    updated = list(existing)
    if entry_name.lower() not in have:
        updated.append(new_entry)
    profile.skills = updated  # reassign so SQLModel flags the JSON column dirty
    profile.updated_at = datetime.utcnow()

    prog.resume_applied = True
    prog.updated_at = datetime.utcnow()

    try:
        session.add(profile)
        session.add(prog)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[skills] resume accept failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't update your profile. Please try again.")

    return RedirectResponse(url=f"/skills/detail?name={_q(name)}", status_code=303)


@router.post("/resume/dismiss")
def resume_dismiss(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    name = name.strip()
    prog = _get_or_create_progress(session, name)
    prog.resume_suggested = False
    prog.resume_suggestion = None
    prog.updated_at = datetime.utcnow()
    session.add(prog)
    session.commit()
    return RedirectResponse(url=f"/skills/detail?name={_q(name)}", status_code=303)


# ---------------------------------------------------------------------------
# AI tutor chat (mirrors interview.py)
# ---------------------------------------------------------------------------

@router.post("/tutor/start")
def tutor_start(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    name = name.strip()
    prog = _get_or_create_progress(session, name)
    profile = _profile_dict(session)

    tsession = SkillTutorSession(skill_name=name, status="active")
    session.add(tsession)
    session.commit()
    session.refresh(tsession)

    note = f"proficiency={prog.proficiency.value}, progress={prog.progress_pct}%"
    opener = engine.tutor_opening(name, profile, progress_note=note)
    session.add(SkillTutorTurn(session_id=tsession.id, role="tutor", content=opener))
    session.commit()

    return RedirectResponse(url=f"/skills/tutor/{tsession.id}", status_code=303)


@router.get("/tutor/{session_id}", response_class=HTMLResponse)
def tutor_page(session_id: int, request: Request, session: Session = Depends(get_session)):
    tsession = session.get(SkillTutorSession, session_id)
    if tsession is None:
        raise HTTPException(404, "Tutor session not found")
    turns = _tutor_turns(session, session_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "skill_tutor.html",
        {"tsession": tsession, "turns": turns},
    )


@router.post("/tutor/{session_id}/message")
def tutor_message(
    session_id: int,
    request: Request,
    message: str = Form(""),
    session: Session = Depends(get_session),
):
    tsession = session.get(SkillTutorSession, session_id)
    if tsession is None:
        raise HTTPException(404, "Tutor session not found")
    message = (message or "").strip()
    profile = _profile_dict(session)
    prior = _tutor_turns(session, session_id)

    student = SkillTutorTurn(
        session_id=session_id, role="student", content=message or "(no message)",
    )
    session.add(student)
    session.commit()
    session.refresh(student)

    prog = _get_or_create_progress(session, tsession.skill_name)
    note = f"proficiency={prog.proficiency.value}, progress={prog.progress_pct}%"
    reply = engine.tutor_reply(tsession.skill_name, profile, prior, message, progress_note=note)
    tutor_turn = SkillTutorTurn(session_id=session_id, role="tutor", content=reply)
    session.add(tutor_turn)
    session.commit()
    session.refresh(tutor_turn)

    templates = request.app.state.templates
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_tutor_exchange.html",
            {"student_turn": student, "tutor_turn": tutor_turn},
        )
    return RedirectResponse(url=f"/skills/tutor/{session_id}", status_code=303)


def _tutor_turns(session: Session, session_id: int) -> List[SkillTutorTurn]:
    return list(session.exec(
        select(SkillTutorTurn)
        .where(SkillTutorTurn.session_id == session_id)
        .order_by(SkillTutorTurn.id)
    ).all())


def _q(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")
