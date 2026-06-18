from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from findmemyjob.db import get_session
from findmemyjob.models import Application, ApplicationStatus, Job, Profile

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    profile = session.get(Profile, 1)
    job_count = len(session.exec(select(Job)).all())
    pending_count = len(session.exec(
        select(Application).where(Application.status == ApplicationStatus.pending)
    ).all())
    ready_count = len(session.exec(
        select(Application).where(Application.status == ApplicationStatus.ready)
    ).all())

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "profile_set": profile is not None and bool((profile.contact or {}).get("name")),
            "job_count": job_count,
            "pending_count": pending_count,
            "ready_count": ready_count,
        },
    )
