from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session

from findmemyjob.config import settings
from findmemyjob.db import get_session
from findmemyjob.importing import import_resume
from findmemyjob.models import Profile
from findmemyjob.pdf import save_resume_pdf
from findmemyjob.search_strategy import suggest_search_queries

router = APIRouter()


def _get_or_create_profile(session: Session) -> Profile:
    profile = session.get(Profile, 1)
    if profile is None:
        profile = Profile(id=1)
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


@router.get("/", response_class=HTMLResponse)
def view_profile(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    profile = _get_or_create_profile(session)
    prefs = profile.preferences or {}
    # Skills with user-asserted evidence — surfaced as a separate panel so the
    # user can audit what's been added across all the gap-claims they've made.
    claimed_skills = [
        s for s in (profile.skills or [])
        if (s.get("evidence") or "").strip()
    ]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "profile": profile,
            "profile_json": json.dumps(profile.model_dump(), indent=2, default=str),
            "claimed_skills": claimed_skills,
            "search_queries": prefs.get("apple_search_queries") or [],
            "search_width": prefs.get("apple_search_width") or "medium",
            "search_max_pages": prefs.get("apple_search_max_pages") or 3,
            "search_url": prefs.get("apple_search_url") or "",
            "external_companies": "\n".join(prefs.get("external_companies") or []),
            "use_bundled_directory": bool(prefs.get("use_bundled_directory")),
            "enable_remoteok": bool(prefs.get("enable_remoteok")),
            "enable_hn_whoishiring": bool(prefs.get("enable_hn_whoishiring")),
            "hn_limit": prefs.get("hn_limit") or 40,
        },
    )


@router.post("/import")
async def import_from_file(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / (file.filename or "resume.pdf")
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    parsed = import_resume(dest)
    profile = _get_or_create_profile(session)
    profile.contact = parsed.get("contact") or {}
    profile.summary = parsed.get("summary") or ""
    profile.work_history = parsed.get("work_history") or []
    profile.education = parsed.get("education") or []
    profile.skills = parsed.get("skills") or []
    profile.certifications = parsed.get("certifications") or []
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/save-json")
def save_profile_json(
    profile_json: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    data = json.loads(profile_json)
    profile = _get_or_create_profile(session)
    for field in ("contact", "summary", "work_history", "education",
                  "skills", "certifications", "preferences"):
        if field in data:
            setattr(profile, field, data[field])
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/skills/remove")
def remove_skill(
    name: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Drop a skill from the master profile by name (case-insensitive).

    Used by the "Claimed skills" panel — when the user reconsiders a gap
    claim, this removes the skill entirely so future scoring/tailoring no
    longer sees it.
    """
    profile = _get_or_create_profile(session)
    target = name.strip().lower()
    if target:
        profile.skills = [
            s for s in (profile.skills or [])
            if (s.get("name") or "").strip().lower() != target
        ]
        profile.updated_at = datetime.utcnow()
        session.add(profile)
        session.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.get("/master-resume.pdf")
def download_master_pdf(session: Session = Depends(get_session)) -> FileResponse:
    profile = _get_or_create_profile(session)
    p = profile.model_dump()
    pdf_path = save_resume_pdf(
        contact=p.get("contact") or {},
        summary=p.get("summary") or "",
        work_history=p.get("work_history") or [],
        education=p.get("education") or [],
        skills=p.get("skills") or [],
        certifications=p.get("certifications") or [],
        filename_hint="master",
    )
    name = (p.get("contact") or {}).get("name", "resume").replace(" ", "_")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"{name}-master.pdf")


@router.post("/external/save")
def save_external_companies(
    external_companies: str = Form(""),
    use_bundled_directory: Optional[str] = Form(None),
    enable_remoteok: Optional[str] = Form(None),
    enable_hn_whoishiring: Optional[str] = Form(None),
    hn_limit: int = Form(40),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Save external company list + free aggregator toggles."""
    entries: List[str] = []
    for line in external_companies.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        src, slug = line.split(":", 1)
        src, slug = src.strip().lower(), slug.strip().lower()
        if src in {"greenhouse", "lever"} and slug:
            entries.append(f"{src}:{slug}")

    profile = _get_or_create_profile(session)
    prefs = dict(profile.preferences or {})
    prefs["external_companies"] = entries
    prefs["use_bundled_directory"] = bool(use_bundled_directory)
    prefs["enable_remoteok"] = bool(enable_remoteok)
    prefs["enable_hn_whoishiring"] = bool(enable_hn_whoishiring)
    prefs["hn_limit"] = max(10, min(200, int(hn_limit)))
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    return RedirectResponse(url="/profile", status_code=303)


# ---------------------------------------------------------------------------
# Search filters (Apple internal)
# ---------------------------------------------------------------------------

@router.post("/apple-session/upload")
async def upload_apple_session(
    file: UploadFile = File(...),
) -> RedirectResponse:
    """Save an apple_session.json (Playwright storage state) to the data dir.

    The file must be a Playwright browser storage-state JSON exported after
    logging in to AppleConnect in a headed browser locally.  Once uploaded,
    the apple_internal source can use it for headless scraping on Railway.
    """
    dest = settings.data_dir / "apple_session.json"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/search/regenerate")
def regenerate_search_queries(
    width: str = Form("medium"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """LLM-suggest search queries based on the current profile + width."""
    profile = _get_or_create_profile(session)
    suggestions = suggest_search_queries(profile.model_dump(), width=width)
    prefs = dict(profile.preferences or {})
    prefs["apple_search_queries"] = [s["query"] for s in suggestions if s.get("query")]
    prefs["apple_search_query_rationales"] = {s["query"]: s.get("rationale", "") for s in suggestions}
    prefs["apple_search_width"] = width
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/search/save")
async def save_search_filters(
    request: Request,
    width: str = Form("medium"),
    max_pages: int = Form(3),
    custom_query: str = Form(""),
    custom_url: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Save the active query list (only the checked ones) + width + max pages + custom URL."""
    form = await request.form()
    all_queries = [v.strip() for v in form.getlist("query")]
    active_idx = {int(i) for i in form.getlist("active_idx") if i.isdigit()}
    active_queries = [q for i, q in enumerate(all_queries) if i in active_idx and q]
    if custom_query.strip():
        active_queries.append(custom_query.strip())

    profile = _get_or_create_profile(session)
    prefs = dict(profile.preferences or {})
    prefs["apple_search_queries"] = active_queries
    prefs["apple_search_width"] = width
    prefs["apple_search_max_pages"] = max(1, min(20, int(max_pages)))
    prefs["apple_search_url"] = custom_url.strip()
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    session.commit()
    return RedirectResponse(url="/profile", status_code=303)
