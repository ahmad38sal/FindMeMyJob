from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from findmemyjob.db import get_session
from findmemyjob.models import Application, ApplicationStatus, Job

router = APIRouter()

# Display order for the board columns — the natural job-hunt funnel.
STATUS_ORDER = [
    ApplicationStatus.pending,
    ApplicationStatus.ready,
    ApplicationStatus.submitted,
    ApplicationStatus.responded,
    ApplicationStatus.interview,
    ApplicationStatus.offer,
    ApplicationStatus.rejected,
    ApplicationStatus.withdrawn,
]


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _board(session: Session):
    """Build status-grouped columns of (app, job) rows for the board view."""
    apps = session.exec(
        select(Application).order_by(Application.last_status_change.desc())
    ).all()
    jobs_by_id = {j.id: j for j in session.exec(select(Job)).all()}
    columns = {s: [] for s in STATUS_ORDER}
    for a in apps:
        columns.setdefault(a.status, []).append(
            {"app": a, "job": jobs_by_id.get(a.job_id)}
        )
    return [{"status": s, "rows": columns.get(s, [])} for s in STATUS_ORDER]


@router.get("/", response_class=HTMLResponse)
def list_applications(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    columns = _board(session)
    total = sum(len(c["rows"]) for c in columns)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "applications.html",
        {
            "columns": columns,
            "total": total,
            "all_statuses": [s.value for s in STATUS_ORDER],
        },
    )


def _render_card(request: Request, session: Session, app: Application) -> HTMLResponse:
    job = session.get(Job, app.job_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_application_card.html",
        {"row": {"app": app, "job": job}, "all_statuses": [s.value for s in STATUS_ORDER]},
    )


@router.post("/{app_id}/status")
def change_status(
    app_id: int,
    request: Request,
    status: str = Form(...),
    session: Session = Depends(get_session),
):
    app = session.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    try:
        new_status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(400, "Unknown status")
    app.status = new_status
    if new_status == ApplicationStatus.submitted and app.submitted_at is None:
        app.submitted_at = datetime.utcnow()
    app.last_status_change = datetime.utcnow()
    try:
        session.add(app)
        session.commit()
        session.refresh(app)
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[applications] status change failed for {app_id}: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't update status right now. Please try again.")
    if _is_htmx(request):
        return _render_card(request, session, app)
    return RedirectResponse(url="/applications", status_code=303)


@router.post("/{app_id}/notes")
def save_notes(
    app_id: int,
    request: Request,
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    app = session.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    app.notes = notes.strip()
    app.last_status_change = datetime.utcnow()
    try:
        session.add(app)
        session.commit()
        session.refresh(app)
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[applications] notes save failed for {app_id}: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save notes right now. Please try again.")
    if _is_htmx(request):
        return _render_card(request, session, app)
    return RedirectResponse(url="/applications", status_code=303)
