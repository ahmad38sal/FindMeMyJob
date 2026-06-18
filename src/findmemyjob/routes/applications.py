from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from findmemyjob.db import get_session
from findmemyjob.models import Application, Job

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def list_applications(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    apps = session.exec(select(Application).order_by(Application.last_status_change.desc())).all()
    jobs_by_id = {j.id: j for j in session.exec(select(Job)).all()}
    rows = [{"app": a, "job": jobs_by_id.get(a.job_id)} for a in apps]
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "applications.html", {"rows": rows})
