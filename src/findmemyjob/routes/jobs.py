from __future__ import annotations

from datetime import datetime
from typing import Dict, List
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from findmemyjob.db import get_session
from findmemyjob.matching import prefilter, score_job
from findmemyjob.models import (
    Application,
    ApplicationStatus,
    ApplyMode,
    Job,
    Profile,
    Resume,
    ResumeKind,
)
from findmemyjob.pdf import save_resume_pdf
from findmemyjob.salary import SalaryEstimate, estimate_salary, fmt_money, position_pct
from findmemyjob.sources import directory as directory_seed
from findmemyjob.sources import generic_url as generic_source
from findmemyjob.sources import greenhouse as greenhouse_source
from findmemyjob.sources import lever as lever_source
from findmemyjob.sources.apple_internal import (
    AppleInternalSource,
    fetch_one_by_url as apple_fetch_one_by_url,
    hydrate_job_description,
)
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
        "_match_section.html",
        {"request": request, "job": job, "app": app, "work_history": work_history},
    )


def _render_salary_partial(request: Request, job: Job) -> HTMLResponse:
    """Render the salary panel as a standalone fragment for HTMX swaps."""
    templates = request.app.state.templates
    estimate = (job.raw or {}).get("salary_estimate")
    return templates.TemplateResponse(
        "_salary_panel.html",
        {
            "request": request,
            "job": job,
            "estimate": estimate,
            "fmt_money": fmt_money,
            "position_pct": position_pct,
        },
    )


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
) -> HTMLResponse:
    """Job list with filters + sort. URL params drive state."""
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

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "rows": decorated,
            "total": len(rows),
            "shown": len(decorated),
            "filters": {
                "q": q, "source": source, "work_mode": work_mode, "status": status,
                "min_salary": min_salary, "score_filter": score_filter, "sort": sort,
            },
            "all_sources": all_sources,
            "all_modes": all_modes,
            "all_statuses": all_statuses,
        },
    )


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
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))

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
    _rescore_app(profile_dict, job, app)
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

    estimate = (job.raw or {}).get("salary_estimate")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "job_detail.html",
        {
            "request": request, "job": job, "app": app, "resume": resume,
            "work_history": work_history,
            "estimate": estimate,
            "fmt_money": fmt_money,
            "position_pct": position_pct,
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
    job.raw = raw
    session.add(job)
    session.commit()
    session.refresh(job)

    if _is_htmx(request):
        return _render_salary_partial(request, job)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/tailor")
def tailor(job_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Generate tailored resume + cover letter + PDF. Confirm-mode only in v1."""
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

    tailored = tailor_resume(profile_dict, job)
    diff = compute_diff(profile_dict, tailored)
    cover = generate_cover_letter(profile_dict, job)

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
    )
    session.add(resume)
    session.commit()
    session.refresh(resume)

    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if app is None:
        app = Application(job_id=job_id)
    app.tailored_resume_id = resume.id
    app.cover_letter = cover
    app.status = ApplicationStatus.ready
    app.mode = ApplyMode.confirm
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}/resume.pdf")
def download_tailored_pdf(job_id: int, session: Session = Depends(get_session)) -> FileResponse:
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if not app or not app.tailored_resume_id:
        raise HTTPException(404, "No tailored resume yet — tailor the role first.")
    resume = session.get(Resume, app.tailored_resume_id)
    if not resume or not resume.pdf_path:
        raise HTTPException(404, "Tailored resume has no PDF on disk.")
    job = session.get(Job, job_id)
    safe_company = (job.company if job else "resume").replace(" ", "_")
    download_name = f"resume-{safe_company}-{job_id}.pdf"
    return FileResponse(resume.pdf_path, media_type="application/pdf", filename=download_name)


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
    _rescore_app(profile_dict, job, app)
    app.last_status_change = datetime.utcnow()
    session.add(app)
    session.commit()
    session.refresh(app)

    if _is_htmx(request):
        return _render_match_partial(request, job, app, profile)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
