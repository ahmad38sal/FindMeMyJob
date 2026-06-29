"""Mock-interview routes — start a session, run the turn-by-turn chat, debrief.

The text chat is the source of truth; voice (Web Speech API) is layered on in
the template and degrades to typing. Every handler degrades gracefully — a busy
LLM produces a friendly canned turn rather than a 500.
"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from findmemyjob import interview as engine
from findmemyjob.db import get_session
from findmemyjob.models import (
    ExperienceItem,
    InterviewSession,
    InterviewTurn,
    Job,
    Profile,
)

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _profile_dict(session: Session) -> Dict:
    profile = session.get(Profile, 1)
    if profile is None:
        raise HTTPException(400, "Profile not set up — visit /profile to import your resume first.")
    return profile.model_dump()


def _active_experience(session: Session) -> List[ExperienceItem]:
    return list(session.exec(
        select(ExperienceItem).where(ExperienceItem.active == True)  # noqa: E712
    ).all())


def _session_turns(session: Session, session_id: int) -> List[InterviewTurn]:
    return list(session.exec(
        select(InterviewTurn)
        .where(InterviewTurn.session_id == session_id)
        .order_by(InterviewTurn.id)
    ).all())


def _render_page(request: Request, session: Session, isession: InterviewSession) -> HTMLResponse:
    job = session.get(Job, isession.job_id)
    turns = _session_turns(session, isession.id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "interview.html",
        {
            "job": job,
            "isession": isession,
            "turns": turns,
            "round_labels": engine.ROUND_LABELS,
            "round_order": engine.ROUND_ORDER,
        },
    )


@router.post("/start/{job_id}")
def start_interview(job_id: int, request: Request, session: Session = Depends(get_session)):
    """Create a session for a job, generate the opener, and open the chat page."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    profile_dict = _profile_dict(session)
    items = _active_experience(session)

    isession = InterviewSession(
        job_id=job_id,
        status="active",
        current_round=engine.ROUND_ORDER[0],
        config={"questions_per_round": engine.DEFAULT_QUESTIONS_PER_ROUND},
    )
    session.add(isession)
    session.commit()
    session.refresh(isession)

    opener = engine.opening_message(profile_dict, job, items)
    session.add(InterviewTurn(
        session_id=isession.id, role="interviewer",
        round=engine.ROUND_ORDER[0], content=opener,
    ))
    session.commit()

    return RedirectResponse(url=f"/interview/{isession.id}", status_code=303)


@router.get("/{session_id}", response_class=HTMLResponse)
def interview_page(session_id: int, request: Request, session: Session = Depends(get_session)):
    isession = session.get(InterviewSession, session_id)
    if isession is None:
        raise HTTPException(404, "Interview session not found")
    return _render_page(request, session, isession)


@router.post("/{session_id}/answer")
def submit_answer(
    session_id: int,
    request: Request,
    answer: str = Form(""),
    session: Session = Depends(get_session),
):
    """Record the candidate's answer, return inline feedback + the next turn.

    HTMX: appends the new exchange to the chat log. Returns the debrief fragment
    when the interview completes.
    """
    isession = session.get(InterviewSession, session_id)
    if isession is None:
        raise HTTPException(404, "Interview session not found")

    job = session.get(Job, isession.job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    templates = request.app.state.templates
    answer = (answer or "").strip()

    # Already finished — just re-render the page (idempotent, no 500).
    if isession.status == "completed":
        if _is_htmx(request):
            return HTMLResponse("")
        return RedirectResponse(url=f"/interview/{session_id}", status_code=303)

    profile_dict = _profile_dict(session)
    items = _active_experience(session)
    prior_turns = _session_turns(session, session_id)

    # Persist the candidate turn (feedback filled in after we compute it).
    cand = InterviewTurn(
        session_id=session_id, role="candidate",
        round=isession.current_round, content=answer or "(no answer given)",
    )
    session.add(cand)
    session.commit()
    session.refresh(cand)

    result = engine.process_answer(
        profile_dict, job, isession, prior_turns, answer, items,
    )

    # Attach inline feedback (+ score) to the candidate turn.
    fb = dict(result.get("feedback") or {})
    fb["score"] = result.get("score", 60)
    cand.feedback = fb
    session.add(cand)

    interviewer_turn = None
    debrief = None
    if result.get("complete"):
        isession.status = "completed"
        # Reload turns (now incl. this answer) for an accurate debrief.
        all_turns = _session_turns(session, session_id)
        debrief = engine.build_debrief(profile_dict, job, all_turns)
        isession.debrief = debrief
        session.add(isession)
    else:
        next_round = result.get("next_round") or isession.current_round
        isession.current_round = next_round
        interviewer_turn = InterviewTurn(
            session_id=session_id, role="interviewer",
            round=next_round, content=result.get("next_message") or "",
        )
        session.add(interviewer_turn)
        session.add(isession)

    session.commit()
    if interviewer_turn is not None:
        session.refresh(interviewer_turn)
    session.refresh(cand)
    session.refresh(isession)

    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_interview_exchange.html",
            {
                "job": job,
                "isession": isession,
                "candidate_turn": cand,
                "interviewer_turn": interviewer_turn,
                "debrief": debrief,
                "round_labels": engine.ROUND_LABELS,
            },
        )
    return RedirectResponse(url=f"/interview/{session_id}", status_code=303)
