from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

try:  # py3.9+ stdlib; always present on the Railway image
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 — degrade to UTC if tzdata missing
    _NY = timezone.utc

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


def _applied_ts(app: Application):
    """The timestamp we treat as 'applied' — submitted_at per spec, else None."""
    return app.submitted_at


def _to_ny_date(dt) -> date | None:
    """Convert a stored (naive UTC) datetime to a calendar date in America/New_York."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_NY).date()


def _week_start(d: date) -> date:
    """Monday of the week containing *d*."""
    return d - timedelta(days=d.weekday())


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
    return [{"status": s, "rows": columns.get(s, [])} for s in STATUS_ORDER], apps


def _compute_stats(apps, on_date: date):
    """Counters, per-status counts, charts and response-rate math.

    'Applied' uses submitted_at (per spec) interpreted in America/New_York.
    """
    today_ny = datetime.now(_NY).date()
    week_start = _week_start(today_ny)

    total = len(apps)
    applied_today = 0
    applied_this_week = 0
    applied_on_date = 0

    status_counts = {s.value: 0 for s in STATUS_ORDER}

    # applications-over-time: applied count per day for the last 14 days.
    day_counts: dict = defaultdict(int)

    submitted_total = 0
    for a in apps:
        status_counts[a.status.value] = status_counts.get(a.status.value, 0) + 1
        ad = _to_ny_date(_applied_ts(a))
        if ad is not None:
            submitted_total += 1
            day_counts[ad] += 1
            if ad == today_ny:
                applied_today += 1
            if ad >= week_start:
                applied_this_week += 1
            if ad == on_date:
                applied_on_date += 1

    # 14-day series (oldest -> newest) for the trend chart.
    series = []
    for i in range(13, -1, -1):
        d = today_ny - timedelta(days=i)
        series.append({"date": d, "label": d.strftime("%m/%d"), "count": day_counts.get(d, 0)})

    # Response-rate math — denominator is everything that reached "submitted".
    responded = status_counts.get("responded", 0)
    interview = status_counts.get("interview", 0)
    offer = status_counts.get("offer", 0)
    rejected = status_counts.get("rejected", 0)
    denom = submitted_total or 1
    rates = {
        "submitted_total": submitted_total,
        "response_rate": round(100 * (responded + interview + offer) / denom),
        "interview_rate": round(100 * (interview + offer) / denom),
        "offer_rate": round(100 * offer / denom),
        "rejection_rate": round(100 * rejected / denom),
    }

    return {
        "total": total,
        "applied_today": applied_today,
        "applied_this_week": applied_this_week,
        "applied_on_date": applied_on_date,
        "status_counts": status_counts,
        "series": series,
        "series_max": max([s["count"] for s in series] + [1]),
        "rates": rates,
        "today_ny": today_ny,
    }


@router.get("/", response_class=HTMLResponse)
def list_applications(
    request: Request,
    on: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    columns, apps = _board(session)
    # Parse the "on this day" control; default to today (NY).
    try:
        on_date = date.fromisoformat(on) if on else datetime.now(_NY).date()
    except ValueError:
        on_date = datetime.now(_NY).date()
    stats = _compute_stats(apps, on_date)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "applications.html",
        {
            "columns": columns,
            "stats": stats,
            "on_date": on_date,
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
