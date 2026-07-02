from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, select

from findmemyjob.db import get_session
from findmemyjob.discovery import get_or_create_search_profile, run_discovery
from findmemyjob.matching import prefilter, score_job, score_jobs_bulk
from findmemyjob.models import DiscoveryRun, SearchProfile
from findmemyjob.models import (
    Application,
    ApplicationStatus,
    ApplyMode,
    ExperienceItem,
    InterviewSession,
    Job,
    Profile,
    Resume,
    ResumeKind,
)
from findmemyjob.pdf import PDF_NO_CACHE_HEADERS, save_resume_pdf
from findmemyjob.salary import (
    SalaryEstimate,
    build_salary_view,
    compute_fair_ask,
    estimate_salary,
    fmt_money,
    position_pct,
)
from findmemyjob.sources import directory as directory_seed
from findmemyjob.sources import generic_url as generic_source
from findmemyjob.sources import greenhouse as greenhouse_source
from findmemyjob.sources import lever as lever_source
from findmemyjob.sources.apple_internal import (
    AppleInternalSource,
    fetch_one_by_url as apple_fetch_one_by_url,
    hydrate_job_description,
)
from findmemyjob.sources.ashby import fetch_one_by_url as ashby_fetch_one_by_url
from findmemyjob.sources.greenhouse import GreenhouseSource
from findmemyjob.sources.hn_whoishiring import HNWhoIsHiringSource
from findmemyjob.sources.lever import LeverSource
from findmemyjob.sources.remoteok import RemoteOKSource
from findmemyjob.tailoring import compute_diff, generate_cover_letter, tailor_resume

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _rescore_app(profile_dict: Dict, job: Job, app: Application) -> None:
    """Mutate `app` in place with score/reasoning/gaps for `job` vs `profile_dict`."""
    drop_reason = prefilter(profile_dict, job)
    if drop_reason:
        app.match_score = 0
        app.match_reasoning = f"Pre-filtered: {drop_reason}"
        app.gaps = []
        app.stretch_required = False
    else:
        result = score_job(profile_dict, job)
        app.match_score = result.score
        app.match_reasoning = result.reasoning
        app.gaps = result.gaps
        app.stretch_required = result.stretch_required


def _render_match_partial(
    request: Request, job: Job, app: Application, profile: Profile
) -> HTMLResponse:
    templates = request.app.state.templates
    work_history = profile.work_history if profile else []
    return templates.TemplateResponse(
        request,
        "_match_section.html",
        {"job": job, "app": app, "work_history": work_history},
    )


def _ensure_fair_ask(session: Session, job: Job, *, force: bool = False) -> Dict:
    """Return the cached fair-ask for `job`, computing+caching it once if absent.

    Computed lazily so the meter has a recommendation on first view without a
    dedicated user action. Cached on `job.raw["fair_ask"]`; `force=True` (the
    refresh button) recomputes. Never raises — degrades to the heuristic.
    """
    raw = job.raw or {}
    cached = raw.get("fair_ask")
    if cached and not force:
        return cached

    profile = session.get(Profile, 1)
    profile_dict = profile.model_dump() if profile else {}
    estimate = raw.get("salary_estimate")
    ask = compute_fair_ask(profile_dict, job, estimate)
    cached = {
        **ask.model_dump(),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    new_raw = dict(raw)
    new_raw["fair_ask"] = cached
    job.raw = new_raw
    session.add(job)
    session.commit()
    session.refresh(job)
    return cached


def _salary_context(session: Session, job: Job) -> Dict:
    """Template vars for the salary panel — estimate, fair-ask, and the view."""
    raw = job.raw or {}
    estimate = raw.get("salary_estimate")
    fair_ask = _ensure_fair_ask(session, job)
    view = build_salary_view(job, estimate, fair_ask)
    return {
        "job": job,
        "estimate": estimate,
        "fair_ask": fair_ask,
        "salary": view,
        "fmt_money": fmt_money,
        "position_pct": position_pct,
    }


def _render_salary_partial(request: Request, job: Job, session: Session) -> HTMLResponse:
    """Render the salary panel as a standalone fragment for HTMX swaps."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_salary_panel.html",
        _salary_context(session, job),
    )


def _load_experience_items(session: Session) -> List[ExperienceItem]:
    """All active experience-bank items, for tailoring. Empty list = behaves
    exactly as before the bank existed."""
    return list(session.exec(
        select(ExperienceItem).where(ExperienceItem.active == True)  # noqa: E712
    ).all())


def _profile_dict(session: Session) -> Dict:
    profile = session.get(Profile, 1)
    if profile is None:
        raise HTTPException(400, "Profile not set up — visit /profile to import your resume first.")
    return profile.model_dump()


def _build_sources(profile_dict: Dict) -> List:
    """Source registry built per-request from profile preferences.

    Apple internal: custom URL > LLM-suggested queries.
    Greenhouse / Lever: union of user-configured `external_companies` and the
        bundled directory (when `use_bundled_directory` is on).
    RemoteOK / HN Who's Hiring: enabled per flag.
    """
    prefs = profile_dict.get("preferences") or {}
    custom_url: str = (prefs.get("apple_search_url") or "").strip()
    queries: List[str] = [q for q in (prefs.get("apple_search_queries") or []) if q]
    max_pages: int = int(prefs.get("apple_search_max_pages") or 3)

    sources: List = []
    if custom_url:
        sources.append(AppleInternalSource(start_url=custom_url, max_pages=max_pages))
    elif queries:
        sources.append(AppleInternalSource(queries=queries, max_pages=max_pages))

    # External companies (user-configured)
    gh: List[str] = []
    lv: List[str] = []
    for entry in prefs.get("external_companies") or []:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        src, slug = entry.split(":", 1)
        src, slug = src.strip().lower(), slug.strip().lower()
        if not slug:
            continue
        if src == "greenhouse":
            gh.append(slug)
        elif src == "lever":
            lv.append(slug)

    # Bundled directory (defaults on once enabled in prefs)
    if prefs.get("use_bundled_directory"):
        gh = list({*gh, *directory_seed.GREENHOUSE})
        lv = list({*lv, *directory_seed.LEVER})

    if gh:
        sources.append(GreenhouseSource(gh))
    if lv:
        sources.append(LeverSource(lv))

    if prefs.get("enable_remoteok"):
        sources.append(RemoteOKSource())
    if prefs.get("enable_hn_whoishiring"):
        sources.append(HNWhoIsHiringSource(limit=int(prefs.get("hn_limit") or 40)))

    return sources


def _fetch_one_by_url(url: str) -> Job:
    """Dispatch a single-URL fetch by host."""
    host = urlparse(url).netloc.lower()
    if "careers.apple.com" in host:
        return apple_fetch_one_by_url(url)
    if "greenhouse.io" in host:
        return greenhouse_source.fetch_one_by_url(url)
    if "lever.co" in host:
        return lever_source.fetch_one_by_url(url)
    if "ashbyhq.com" in host:
        return ashby_fetch_one_by_url(url)
    return generic_source.fetch_one_by_url(url)


@router.get("/", response_class=HTMLResponse)
def list_jobs(
    request: Request,
    session: Session = Depends(get_session),
    q: str = "",
    source: List[str] = Query(default=[]),
    work_mode: List[str] = Query(default=[]),
    status: List[str] = Query(default=[]),
    min_salary: int = 0,
    score_filter: str = "all",
    sort: str = "score_desc",
    page: int = 1,
) -> HTMLResponse:
    """Job list with filters + sort + pagination. URL params drive state."""
    rows = session.exec(select(Job)).all()

    job_apps: Dict[int, Application] = {}
    for app in session.exec(select(Application)).all():
        cur = job_apps.get(app.job_id)
        if cur is None or app.last_status_change > cur.last_status_change:
            job_apps[app.job_id] = app

    # Filter
    q_lower = q.strip().lower()
    source_set = set(source)
    mode_set = set(work_mode)
    status_set = set(status)

    def keep(job: Job) -> bool:
        if source_set and job.source not in source_set:
            return False
        if mode_set and (job.work_mode or "") not in mode_set:
            return False
        if min_salary and (job.salary_max or 0) < min_salary:
            return False
        if q_lower:
            hay = " ".join([job.title or "", job.company or "", job.team or "",
                            job.location or "", job.description or ""]).lower()
            if q_lower not in hay:
                return False
        app = job_apps.get(job.id)
        if status_set:
            current_status = app.status.value if app else "none"
            if current_status not in status_set:
                return False
        if score_filter == "scored" and (app is None or app.match_score is None):
            return False
        if score_filter == "unscored" and app is not None and app.match_score is not None:
            return False
        return True

    filtered = [j for j in rows if keep(j)]
    decorated = [{"job": j, "app": job_apps.get(j.id),
                  "score": (job_apps[j.id].match_score if j.id in job_apps else None)}
                 for j in filtered]

    # Sort
    if sort == "score_asc":
        decorated.sort(key=lambda d: (d["score"] is None, d["score"] or 0))
    elif sort == "recent":
        decorated.sort(key=lambda d: d["job"].fetched_at, reverse=True)
    elif sort == "salary_desc":
        decorated.sort(key=lambda d: (d["job"].salary_max or 0), reverse=True)
    else:  # score_desc (default)
        decorated.sort(key=lambda d: (d["score"] is None, -(d["score"] or 0)))

    # All known sources / statuses for the filter UI checkboxes
    all_sources = sorted({j.source for j in rows if j.source})
    all_modes = sorted({j.work_mode for j in rows if j.work_mode})
    all_statuses = sorted({app.status.value for app in job_apps.values() if app})

    # Pagination over the filtered+sorted result set.
    per_page = 50
    matched = len(decorated)
    total_pages = max(1, (matched + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_rows = decorated[start:start + per_page]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": page_rows,
            "total": len(rows),
            "shown": matched,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "page_start": start,
            "filters": {
                "q": q, "source": source, "work_mode": work_mode, "status": status,
                "min_salary": min_salary, "score_filter": score_filter, "sort": sort,
            },
            "all_sources": all_sources,
            "all_modes": all_modes,
            "all_statuses": all_statuses,
        },
    )


@router.post("/score-all")
async def score_all_jobs(session: Session = Depends(get_session)) -> RedirectResponse:
    """Score all unscored jobs concurrently (up to 5 in-flight at once).

    Creates or updates Application rows with match scores, then redirects
    to /jobs so the updated scores are visible immediately.
    """
    profile_dict = _profile_dict(session)

    # Collect jobs that don't have a score yet
    all_jobs = session.exec(select(Job)).all()
    job_apps: Dict[int, Application] = {}
    for app in session.exec(select(Application)).all():
        cur = job_apps.get(app.job_id)
        if cur is None or app.last_status_change > cur.last_status_change:
            job_apps[app.job_id] = app

    unscored = [
        j for j in all_jobs
        if j.id is not None and (
            j.id not in job_apps or job_apps[j.id].match_score is None
        )
    ]

    if not unscored:
        return RedirectResponse(url="/jobs", status_code=303)

    results = await score_jobs_bulk(profile_dict, unscored)

    now = datetime.utcnow()
    for job in unscored:
        if job.id not in results:
            continue
        result = results[job.id]
        app = job_apps.get(job.id)
        if app is None:
            app = Application(job_id=job.id)
        app.match_score = result.score
        app.match_reasoning = result.reasoning
        app.gaps = result.gaps
        app.stretch_required = result.stretch_required
        app.last_status_change = now
        session.add(app)
    session.commit()
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/refresh")
def refresh_jobs(session: Session = Depends(get_session)) -> RedirectResponse:
    """Pull fresh listings from every enabled source. Filters by configured queries.

    Resilient: one source failing (network error, blocked domain, expired
    session, parser breakage) is logged and skipped — the rest still run.
    """
    profile_dict = _profile_dict(session)
    prefs = profile_dict.get("preferences") or {}
    queries: List[str] = [q for q in (prefs.get("apple_search_queries") or []) if q]

    for source in _build_sources(profile_dict):
        try:
            if isinstance(source, AppleInternalSource):
                jobs = source.fetch()
            else:
                seen: set = set()
                jobs = []
                if queries:
                    for q in queries:
                        for j in source.fetch(query=q):
                            if j.source_id in seen:
                                continue
                            seen.add(j.source_id)
                            jobs.append(j)
                else:
                    jobs = source.fetch()
        except Exception as e:
            print(f"[refresh] {source.name} failed, skipping: {e}")
            continue

        for job in jobs:
            existing = session.exec(
                select(Job).where(Job.source == job.source).where(Job.source_id == job.source_id)
            ).first()
            if existing is None:
                session.add(job)
        session.commit()
    return RedirectResponse(url="/jobs", status_code=303)


def _max_age_days(prefs: Dict) -> int:
    try:
        return max(1, min(120, int(prefs.get("discovery_max_age_days") or 14)))
    except (ValueError, TypeError):
        return 14


@router.get("/top-picks", response_class=HTMLResponse)
def top_picks(
    request: Request,
    session: Session = Depends(get_session),
    max_age_days: int = 0,
) -> HTMLResponse:
    """Ranked 'Top Picks for you' — best fresh discovered matches with reasoning."""
    profile = session.get(Profile, 1)
    prefs = (profile.preferences if profile else {}) or {}
    if max_age_days <= 0:
        max_age_days = _max_age_days(prefs)
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)

    sp = session.get(SearchProfile, 1)
    last_run = session.exec(
        select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc())
    ).first()

    # Jobs that have been fit-scored by the discovery engine.
    scored = session.exec(
        select(Job).where(Job.fit_score != None)  # noqa: E711
    ).all()

    def is_fresh(j: Job) -> bool:
        return j.posted_at is None or j.posted_at >= cutoff

    rows = [j for j in scored if is_fresh(j)]
    rows.sort(key=lambda j: (j.fit_score or 0), reverse=True)
    rows = rows[:50]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "top_picks.html",
        {
            "rows": rows,
            "search_profile": sp,
            "last_run": last_run,
            "max_age_days": max_age_days,
            "profile_set": profile is not None and bool((profile.contact or {}).get("name")),
        },
    )


@router.post("/discover")
def discover(
    session: Session = Depends(get_session),
    regenerate: str = Form(""),
) -> RedirectResponse:
    """On-demand 'Find new jobs now' — runs the full pipeline, then shows picks."""
    profile = session.get(Profile, 1)
    prefs = (profile.preferences if profile else {}) or {}
    run_discovery(
        session,
        regenerate_search_profile=bool(regenerate),
        max_age_days=_max_age_days(prefs),
    )
    return RedirectResponse(url="/jobs/top-picks", status_code=303)


@router.post("/api/discover")
def api_discover(
    session: Session = Depends(get_session),
    regenerate: bool = False,
) -> JSONResponse:
    """JSON discovery endpoint for cron. Returns the run summary + NEW top matches.

    Response shape:
      {
        "run_id": int, "started_at": iso, "finished_at": iso,
        "sources_used": [...], "fetched": n, "new": n, "scored": n, "fresh": n,
        "error": null | str,
        "top_matches": [
          {job_id, title, company, url, score, reasoning, gaps, posted_at, undated}, ...
        ]
      }
    """
    profile = session.get(Profile, 1)
    prefs = (profile.preferences if profile else {}) or {}
    run = run_discovery(
        session,
        regenerate_search_profile=regenerate,
        max_age_days=_max_age_days(prefs),
    )
    return JSONResponse({
        "run_id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "sources_used": run.sources_used,
        "fetched": run.fetched_count,
        "new": run.new_count,
        "scored": run.scored_count,
        "fresh": run.fresh_count,
        "error": run.error,
        "top_matches": run.top_matches,
    })


@router.post("/search-profile/regenerate")
def regenerate_search_profile_route(
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Re-derive the ideal-role search profile from the current Profile."""
    get_or_create_search_profile(session, regenerate=True)
    return RedirectResponse(url="/jobs/top-picks", status_code=303)


@router.post("/add-by-url")
def add_by_url(
    url: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Paste any job URL — host dispatch picks Apple / Greenhouse / Lever / generic."""
    url = url.strip()
    if not url:
        raise HTTPException(400, "Empty URL")
    try:
        job = _fetch_one_by_url(url)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001 - last-resort guard
        # Never leak a raw 500 to the user for an add-by-url attempt. Most
        # unexpected failures here are upstream (model busy, page quirks).
        print(f"[add-by-url] unexpected error for {url!r}: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't add this job right now due to an unexpected error. "
            "Please try again in a moment, or paste the description manually.",
        )

    try:
        existing = session.exec(
            select(Job).where(Job.source == job.source).where(Job.source_id == job.source_id)
        ).first()
        if existing:
            existing.description = job.description or existing.description
            existing.salary_min = job.salary_min or existing.salary_min
            existing.salary_max = job.salary_max or existing.salary_max
            session.add(existing)
            session.commit()
            return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
        session.add(job)
        session.commit()
        session.refresh(job)
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except Exception as e:  # noqa: BLE001 - don't leak a 500 on DB write
        session.rollback()
        print(f"[add-by-url] DB write failed for {url!r}: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Extracted the job but couldn't save it. Please try again in a moment.",
        )


@router.post("/paste")
def add_by_paste(
    text: str = Form(...),
    url: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Add a job by pasting its raw text — for postings that can't be fetched by
    URL (login-walled sites like LinkedIn, PDFs, emails, crawl-blocked pages).

    The shared LLM extractor turns the text into a structured Job, we dedupe and
    save it, then immediately fit-score it so the match shows on the detail page.
    """
    text = (text or "").strip()
    url = (url or "").strip()
    if len(text) < 40:
        raise HTTPException(
            400,
            "Please paste the full job posting text (title, company, and "
            "description). That looked too short.",
        )
    try:
        job = generic_source.build_job_from_text(text, url=url or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001 - never leak a raw 500
        print(f"[paste] unexpected extraction error: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't read that job posting right now due to an unexpected error. "
            "Please try again in a moment.",
        )

    try:
        existing = session.exec(
            select(Job).where(Job.source == job.source).where(Job.source_id == job.source_id)
        ).first()
        if existing:
            existing.description = job.description or existing.description
            existing.salary_min = job.salary_min or existing.salary_min
            existing.salary_max = job.salary_max or existing.salary_max
            existing.work_mode = job.work_mode or existing.work_mode
            session.add(existing)
            session.commit()
            session.refresh(existing)
            job = existing
        else:
            session.add(job)
            session.commit()
            session.refresh(job)
    except Exception as e:  # noqa: BLE001 - don't leak a 500 on DB write
        session.rollback()
        print(f"[paste] DB write failed: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Extracted the job but couldn't save it. Please try again in a moment.",
        )

    # Immediately fit-score so the user sees the match on the detail page.
    # Best-effort: a scoring failure shouldn't lose the saved job.
    try:
        profile_dict = _profile_dict(session)
        app = session.exec(select(Application).where(Application.job_id == job.id)).first()
        if app is None:
            app = Application(job_id=job.id)
        _rescore_app(profile_dict, job, app)
        app.last_status_change = datetime.utcnow()
        session.add(app)
        session.commit()
    except HTTPException:
        # Profile not set up — still keep the job; user can score later.
        session.rollback()
    except Exception as e:  # noqa: BLE001 - scoring is best-effort here
        session.rollback()
        print(f"[paste] auto-score skipped for job {job.id}: {type(e).__name__}: {e}")

    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@router.post("/{job_id}/experience")
def add_experience_from_job(
    job_id: int,
    request: Request,
    raw_text: str = Form(...),
    label: str = Form(""),
    category: str = Form(""),
    session: Session = Depends(get_session),
):
    """Quick-add an experience-bank note while viewing a job.

    Saves an ExperienceItem linked to this job (job_id set) so tailoring for this
    role prioritizes it; it also lives in the central bank. HTMX swaps the panel
    inline; a plain POST redirects back to the job.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    text = (raw_text or "").strip()
    if len(text) < 8:
        if _is_htmx(request):
            return _render_experience_panel(
                request, job,
                error="Please write a little more — a few words about what you did.",
            )
        raise HTTPException(
            400, "Please write a little more about the experience (a few words)."
        )

    item = ExperienceItem(
        raw_text=text,
        label=(label or "").strip() or None,
        category=(category or "").strip() or None,
        job_id=job_id,
    )
    try:
        session.add(item)
        session.commit()
    except Exception as e:  # noqa: BLE001 - don't leak a 500 on DB write
        session.rollback()
        print(f"[experience] DB write failed for job {job_id}: {type(e).__name__}: {e}")
        if _is_htmx(request):
            return _render_experience_panel(
                request, job, error="Couldn't save that note. Please try again.",
            )
        raise HTTPException(503, "Couldn't save that note. Please try again.")

    if _is_htmx(request):
        return _render_experience_panel(request, job, saved=True)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _render_experience_panel(
    request: Request, job: Job, *, saved: bool = False, error: str = ""
) -> HTMLResponse:
    templates = request.app.state.templates
    session = next(get_session())
    try:
        linked = list(session.exec(
            select(ExperienceItem)
            .where(ExperienceItem.job_id == job.id)
            .order_by(ExperienceItem.created_at.desc())
        ).all())
    finally:
        session.close()
    return templates.TemplateResponse(
        request,
        "_experience_panel.html",
        {"job": job, "linked_items": linked, "saved": saved, "error": error},
    )


@router.post("/{job_id}/score")
def score(
    job_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    profile_dict = _profile_dict(session)

    # Lazy hydrate: matching needs the full description, but listing fetches
    # don't pull detail pages (too slow for 100s of jobs at once).
    if not job.description:
        try:
            hydrate_job_description(job)
            session.add(job)
            session.commit()
            session.refresh(job)
        except Exception as e:
            print(f"[score] description hydration failed: {e}")

    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id)
    try:
        _rescore_app(profile_dict, job, app)
    except Exception as e:  # noqa: BLE001 - scoring must never 500 the page
        session.rollback()
        print(f"[score] scoring failed for job {job_id}: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't score this job right now (the AI model may be busy). "
            "Please try again in a moment.",
        )
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    session.refresh(app)

    if _is_htmx(request):
        profile = session.get(Profile, 1)
        return _render_match_partial(request, job, app, profile)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    resume = None
    if app and app.tailored_resume_id:
        resume = session.get(Resume, app.tailored_resume_id)

    profile = session.get(Profile, 1)
    work_history = profile.work_history if profile else []

    linked_items = list(session.exec(
        select(ExperienceItem)
        .where(ExperienceItem.job_id == job_id)
        .order_by(ExperienceItem.created_at.desc())
    ).all())

    interview_sessions = list(session.exec(
        select(InterviewSession)
        .where(InterviewSession.job_id == job_id)
        .order_by(InterviewSession.id.desc())
    ).all())

    # Salary panel context (estimate + lazily-cached fair-ask + meter view).
    # Defensive: a salary glitch must never take down the whole detail page.
    try:
        salary_ctx = _salary_context(session, job)
    except Exception as e:  # noqa: BLE001
        print(f"[job_detail] salary context failed: {e}")
        salary_ctx = {
            "job": job,
            "estimate": (job.raw or {}).get("salary_estimate"),
            "fair_ask": None,
            "salary": build_salary_view(job, (job.raw or {}).get("salary_estimate"), None),
            "fmt_money": fmt_money,
            "position_pct": position_pct,
        }

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            **salary_ctx,
            "job": job, "app": app, "resume": resume,
            "work_history": work_history,
            "linked_items": linked_items,
            "interview_sessions": interview_sessions,
            "saved": False,
            "error": "",
        },
    )


@router.post("/{job_id}/estimate-salary")
def estimate_salary_route(
    job_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """LLM-estimate market range + user target for a job that has no posted salary.

    Cached on `job.raw["salary_estimate"]`. Re-calling overwrites — that's the
    "Re-estimate" button.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    profile_dict = _profile_dict(session)

    # Hydrate the description if we don't have one — the estimate quality
    # depends heavily on JD signal.
    if not job.description:
        try:
            hydrate_job_description(job)
            session.add(job)
            session.commit()
            session.refresh(job)
        except Exception as e:
            print(f"[estimate-salary] description hydration failed: {e}")

    estimate = estimate_salary(profile_dict, job)
    raw = dict(job.raw or {})
    raw["salary_estimate"] = {
        **estimate.model_dump(),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    # Estimate changed — drop the cached fair-ask so it recomputes against it.
    raw.pop("fair_ask", None)
    job.raw = raw
    session.add(job)
    session.commit()
    session.refresh(job)

    if _is_htmx(request):
        return _render_salary_partial(request, job, session)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/fair-ask")
def fair_ask_route(
    job_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Recompute the recommended fair ask (the "Refresh ask" button).

    Hydrates the JD if missing for better signal, forces a fresh LLM/heuristic
    computation, and re-renders the salary panel. Never 500s.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    if not job.description:
        try:
            hydrate_job_description(job)
            session.add(job)
            session.commit()
            session.refresh(job)
        except Exception as e:  # noqa: BLE001
            print(f"[fair-ask] description hydration failed: {e}")

    _ensure_fair_ask(session, job, force=True)

    if _is_htmx(request):
        return _render_salary_partial(request, job, session)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/tailor")
def tailor(
    job_id: int,
    include_summary: bool = Form(False),
    page_length: str = Form("auto"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Generate tailored resume + cover letter + PDF. Confirm-mode only in v1.

    include_summary: unchecked checkbox isn't submitted, so absence -> False; the
    form ships the box checked, so the default user experience is summary-on.
    page_length: "auto" (default) | "1" | "2".
    """
    if page_length not in ("1", "2"):
        page_length = "auto"
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    profile_dict = _profile_dict(session)

    # Tailoring needs the full description; hydrate if it's empty.
    if not job.description:
        try:
            hydrate_job_description(job)
            session.add(job)
            session.commit()
            session.refresh(job)
        except Exception as e:
            print(f"[tailor] description hydration failed: {e}")

    experience_items = _load_experience_items(session)
    try:
        tailored = tailor_resume(
            profile_dict, job, experience_items,
            include_summary=include_summary, page_length=page_length,
        )
        diff = compute_diff(profile_dict, tailored)
        cover = generate_cover_letter(profile_dict, job, experience_items)
    except Exception as e:  # noqa: BLE001 - tailoring must never 500 the page
        session.rollback()
        print(f"[tailor] generation failed for job {job_id}: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't tailor this resume right now (the AI model may be busy). "
            "Please try again in a moment.",
        )

    # Render the tailored resume to PDF — contact comes from master profile,
    # rest from the tailored content.
    pdf_path = save_resume_pdf(
        contact=profile_dict.get("contact") or {},
        summary=tailored.summary,
        work_history=tailored.work_history,
        education=tailored.education or profile_dict.get("education") or [],
        skills=tailored.skills,
        certifications=profile_dict.get("certifications") or [],
        filename_hint=f"job-{job_id}-{job.company}",
    )

    resume = Resume(
        kind=ResumeKind.tailored,
        job_id=job_id,
        content=tailored.model_dump(),
        diff_from_master=[d.model_dump() for d in diff],
        keywords_targeted=tailored.keywords_targeted,
        pdf_path=str(pdf_path),
        include_summary=include_summary,
        page_length=page_length,
    )
    session.add(resume)
    session.commit()
    session.refresh(resume)

    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id)
    # If the user tailored without scoring first, compute a match score now so
    # the tracker still shows it. Best-effort — never block tailoring on it.
    if app.match_score is None:
        try:
            _rescore_app(profile_dict, job, app)
        except Exception as e:  # noqa: BLE001
            print(f"[tailor] auto-score skipped for job {job_id}: {e}")
    app.tailored_resume_id = resume.id
    app.cover_letter = cover
    app.status = ApplicationStatus.ready
    app.mode = ApplyMode.confirm
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _regen_and_store_pdf(session: Session, resume: Resume, job) -> str:
    """Render a fresh PDF from the resume's current content, persist the path.

    Uses the same renderer/args as the manual-edit save path. Content is
    normalized through _as_content_dict so a legacy string-content row renders
    fine. Writes under settings.resumes_dir (absolute), updates resume.pdf_path,
    and commits. Raises on render failure (callers decide how to surface it).
    """
    content = _as_content_dict(resume.content)
    profile_dict = _profile_dict(session)
    job_id = job.id if job else resume.job_id
    company = (job.company if job else None) or "resume"
    pdf_path = save_resume_pdf(
        contact=profile_dict.get("contact") or {},
        summary=content.get("summary") or "",
        work_history=content.get("work_history") or [],
        education=content.get("education") or profile_dict.get("education") or [],
        skills=content.get("skills") or [],
        certifications=profile_dict.get("certifications") or [],
        filename_hint=f"job-{job_id}-{company}-edited",
    )
    resume.pdf_path = str(pdf_path)
    session.add(resume)
    session.commit()
    session.refresh(resume)
    return resume.pdf_path


def _ensure_resume_pdf(session: Session, resume: Resume, job) -> bool:
    """Guarantee resume.pdf_path points at an on-disk file, regenerating if not.

    Self-heals legacy rows whose pdf_path is empty or a stale relative path to a
    file that no longer exists on the container. Returns True when a usable file
    exists after the call, False if regeneration failed.
    """
    if resume.pdf_path and os.path.exists(resume.pdf_path):
        return True
    try:
        _regen_and_store_pdf(session, resume, job)
    except Exception:  # noqa: BLE001 - surface for logs; caller returns a 404, never 500
        session.rollback()
        print(f"[resume-pdf] on-demand regen failed for resume {resume.id}:\n{traceback.format_exc()}")
        return False
    return bool(resume.pdf_path and os.path.exists(resume.pdf_path))


@router.get("/{job_id}/resume.pdf")
def download_tailored_pdf(job_id: int, session: Session = Depends(get_session)) -> FileResponse:
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if not app or not app.tailored_resume_id:
        raise HTTPException(404, "No tailored resume yet — tailor the role first.")
    resume = session.get(Resume, app.tailored_resume_id)
    if not resume:
        raise HTTPException(404, "Tailored resume not found.")
    job = session.get(Job, job_id)
    if not _ensure_resume_pdf(session, resume, job):
        raise HTTPException(404, "Could not produce a PDF for this resume.")
    safe_company = (job.company if job else "resume").replace(" ", "_")
    download_name = f"resume-{safe_company}-{job_id}.pdf"
    return FileResponse(resume.pdf_path, media_type="application/pdf",
                        filename=download_name, headers=PDF_NO_CACHE_HEADERS)


def _tailored_resume_for_job(session: Session, job_id: int) -> Optional[Resume]:
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if not app or not app.tailored_resume_id:
        return None
    return session.get(Resume, app.tailored_resume_id)


def _as_content_dict(raw) -> Dict:
    """Normalize a Resume.content value to a dict, whatever shape it's stored in.

    Most rows hold a proper dict, but a few legacy rows have ``content`` stored
    as a JSON *string* (json.dumps written into the JSON column), which round-
    trips back as ``str`` and breaks any ``.get()``/``dict()`` access. Parse the
    string (and unwrap one extra layer of double-encoding) so callers always get
    a dict; anything unparseable degrades to ``{}``.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, str):  # guard against nested double-encoding
            try:
                parsed = json.loads(parsed)
            except (ValueError, TypeError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _apply_manual_edits(content: Dict, form) -> Dict:
    """Overlay edited text fields onto a copy of the stored content.

    We mutate a copy of the existing structured content (rather than rebuilding
    from scratch) so untouched metadata — skill years/evidence, per-role skills,
    keywords_targeted, etc. — is preserved. Bullets/highlights are edited as
    one-per-line textareas: blank lines are dropped so removing a line removes
    the bullet. Keys are matched positionally against the render-time order.
    """
    content = _as_content_dict(content)  # defense-in-depth: tolerate str content
    edited = dict(content)
    edited["summary"] = (form.get("summary") or "").strip()

    new_wh = []
    for i, role in enumerate(content.get("work_history") or []):
        role = dict(role)
        for key in ("title", "company", "location", "start", "end"):
            val = form.get(f"wh_{i}_{key}")
            if val is not None:
                role[key] = val.strip()
        bullets_raw = form.get(f"wh_{i}_bullets")
        if bullets_raw is not None:
            role["bullets"] = [b.strip() for b in bullets_raw.splitlines() if b.strip()]
        new_wh.append(role)
    edited["work_history"] = new_wh

    new_edu = []
    for j, ed in enumerate(content.get("education") or []):
        ed = dict(ed)
        for key in ("school", "degree", "field", "start", "end", "gpa"):
            val = form.get(f"edu_{j}_{key}")
            if val is not None:
                ed[key] = val.strip()
        hl_raw = form.get(f"edu_{j}_highlights")
        if hl_raw is not None:
            ed["highlights"] = [h.strip() for h in hl_raw.splitlines() if h.strip()]
        new_edu.append(ed)
    edited["education"] = new_edu

    new_skills = []
    for k, sk in enumerate(content.get("skills") or []):
        sk = dict(sk)
        nm = form.get(f"skill_{k}_name")
        if nm is not None:
            sk["name"] = nm.strip()
        cat = form.get(f"skill_{k}_category")
        if cat is not None:
            sk["category"] = cat.strip()
        if sk.get("name"):
            new_skills.append(sk)
    edited["skills"] = new_skills
    return edited


def _content_is_empty(content: Dict) -> bool:
    if (content.get("summary") or "").strip():
        return False
    if any((r.get("bullets") or r.get("title") or r.get("company"))
           for r in (content.get("work_history") or [])):
        return False
    if any(s.get("name") for s in (content.get("skills") or [])):
        return False
    if any((e.get("school") or e.get("degree") or e.get("field") or e.get("highlights"))
           for e in (content.get("education") or [])):
        return False
    return True


@router.get("/{job_id}/resume/edit")
def edit_resume_form(
    job_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    """Inline full-text editor for an already-tailored resume (no AI)."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    resume = _tailored_resume_for_job(session, job_id)
    if resume is None:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "resume_edit.html",
        {"job": job, "resume": resume, "content": _as_content_dict(resume.content), "error": None},
    )


@router.post("/{job_id}/resume/edit")
async def save_resume_edit(
    job_id: int, request: Request, session: Session = Depends(get_session)
):
    """Save hand-edited resume text and re-render the PDF. No LLM involved."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    resume = _tailored_resume_for_job(session, job_id)
    if resume is None:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    form = await request.form()
    edited = _apply_manual_edits(_as_content_dict(resume.content), form)
    templates = request.app.state.templates

    if _content_is_empty(edited):
        return templates.TemplateResponse(
            request, "resume_edit.html",
            {"job": job, "resume": resume, "content": edited,
             "error": "Resume cannot be empty — add a summary, a role, or a skill before saving."},
            status_code=400,
        )

    # 1) Persist the text edit first so it's never lost, even if the PDF fails.
    try:
        resume.content = edited
        resume.manually_edited = True
        session.add(resume)
        session.commit()
        session.refresh(resume)
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[resume-edit] save failed for job {job_id}: {type(e).__name__}: {e}")
        return templates.TemplateResponse(
            request, "resume_edit.html",
            {"job": job, "resume": resume, "content": edited,
             "error": "Couldn't save your edits just now. Please try again."},
            status_code=200,  # never a raw 500; show a friendly page instead
        )

    # 2) Best-effort PDF regen from the edited content (same renderer as tailoring).
    #    The text is already committed above, so a render glitch must not 500 and
    #    must not lose the edit — but we log the full traceback to stay debuggable.
    try:
        _regen_and_store_pdf(session, resume, job)
    except Exception:  # noqa: BLE001
        session.rollback()
        print(f"[resume-edit] PDF regen failed for job {job_id}:\n{traceback.format_exc()}")

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/mark-applied")
def mark_applied(job_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """User submitted manually (or via the form-fill extension in v2). Record it."""
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id)
    app.status = ApplicationStatus.submitted
    app.submitted_at = datetime.utcnow()
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/claim-gap")
def claim_gap(
    job_id: int,
    request: Request,
    gap: str = Form(...),
    evidence: str = Form(""),
    role_index: int = Form(-1),
    bullet: str = Form(""),
    session: Session = Depends(get_session),
):
    """User asserts they actually have a flagged gap → add to master profile + rescore.

    Persists the user's free-text `evidence` directly on the skill record so the
    matching LLM has real context next time around (the previous version stored
    only the bare skill name, which the LLM often re-flagged as a gap).

    Optionally also attaches the skill (and an explicit bullet) to a chosen role
    in `work_history`. If `bullet` is empty but a role is picked, `evidence` is
    used as the bullet — saves the user from typing the same thing twice.
    """
    profile = session.get(Profile, 1)
    if profile is None:
        raise HTTPException(400, "Profile not set up.")

    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    gap_text = gap.strip()
    if not gap_text:
        if _is_htmx(request):
            app = session.exec(select(Application).where(Application.job_id == job_id)).first()
            return _render_match_partial(request, job, app, profile)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    evidence_text = evidence.strip()

    # 1. Top-level skills — merge case-insensitively. If the skill already
    #    exists, append new evidence to whatever's there; otherwise add fresh.
    skills = list(profile.skills or [])
    matched = None
    for s in skills:
        if (s.get("name") or "").strip().lower() == gap_text.lower():
            matched = s
            break
    if matched is None:
        skills.append({
            "name": gap_text,
            "category": "Other",
            "years": None,
            "evidence": evidence_text or None,
        })
    elif evidence_text:
        prior = (matched.get("evidence") or "").strip()
        matched["evidence"] = (prior + "\n" + evidence_text).strip() if prior else evidence_text
    profile.skills = skills

    # 2. Role-level — only when a real role index is given.
    if role_index >= 0:
        work = list(profile.work_history or [])
        if role_index < len(work):
            role = dict(work[role_index])
            role_skills = list(role.get("skills") or [])
            if gap_text.lower() not in {s.lower() for s in role_skills if isinstance(s, str)}:
                role_skills.append(gap_text)
            role["skills"] = role_skills
            bullet_text = bullet.strip() or evidence_text
            if bullet_text:
                role_bullets = list(role.get("bullets") or [])
                role_bullets.append(bullet_text)
                role["bullets"] = role_bullets
            work[role_index] = role
            profile.work_history = work

    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    session.refresh(profile)

    # 3. Re-score so the score + remaining gaps reflect the new profile.
    profile_dict = profile.model_dump()
    if not job.description:
        try:
            hydrate_job_description(job)
            session.add(job)
            session.commit()
            session.refresh(job)
        except Exception:
            pass

    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id)
    try:
        _rescore_app(profile_dict, job, app)
    except Exception as e:  # noqa: BLE001 - scoring must never 500 the page
        session.rollback()
        print(f"[score] scoring failed for job {job_id}: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't score this job right now (the AI model may be busy). "
            "Please try again in a moment.",
        )
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    session.refresh(app)

    if _is_htmx(request):
        return _render_match_partial(request, job, app, profile)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
