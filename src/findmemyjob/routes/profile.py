from __future__ import annotations

import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session
from starlette.datastructures import FormData

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


# ---------------------------------------------------------------------------
# Structured-form parsing
#
# The profile editor posts repeatable sections as parallel indexed arrays, e.g.
# work_company[], work_title[], work_bullets[] (newline-joined per row). Parsing
# is fully defensive: a malformed row is skipped rather than 500ing, and empty
# rows (the "add row" template the user never filled in) are dropped.
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _lines(value: Any) -> List[str]:
    """Split a textarea value into a clean list of non-empty lines."""
    return [ln.strip() for ln in _clean(value).splitlines() if ln.strip()]


def _csv(value: Any) -> List[str]:
    return [p.strip() for p in _clean(value).split(",") if p.strip()]


def _opt_int(value: Any) -> Optional[int]:
    s = _clean(value)
    if not s:
        return None
    try:
        return int(float(s.replace(",", "")))
    except (ValueError, TypeError):
        return None


def _opt_float(value: Any) -> Optional[float]:
    s = _clean(value)
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _opt_date(value: Any) -> Optional[str]:
    """Keep a date as an ISO-ish string. Stored as JSON, so a string is fine and
    avoids choking on partial dates the user may type (e.g. '2021')."""
    s = _clean(value)
    return s or None


def parse_profile_form(form: FormData) -> Dict[str, Any]:
    """Turn the structured editor's form fields into the Profile JSON shape.

    Never raises on bad row data — incomplete rows are skipped. The only hard
    requirement enforced by the route is the contact name.
    """
    g = form.get
    gl = form.getlist

    contact = {
        "name": _clean(g("contact_name")),
        "email": _clean(g("contact_email")) or None,
        "phone": _clean(g("contact_phone")) or None,
        "location": _clean(g("contact_location")) or None,
        "linkedin": _clean(g("contact_linkedin")) or None,
        "github": _clean(g("contact_github")) or None,
        "portfolio": _clean(g("contact_portfolio")) or None,
    }

    # Work history — parallel arrays.
    work_history: List[Dict[str, Any]] = []
    companies = gl("work_company")
    titles = gl("work_title")
    locations = gl("work_location")
    starts = gl("work_start")
    ends = gl("work_end")
    bullets = gl("work_bullets")
    skills_cols = gl("work_skills")
    for i in range(max(len(companies), len(titles))):
        company = _clean(companies[i]) if i < len(companies) else ""
        title = _clean(titles[i]) if i < len(titles) else ""
        if not company and not title:
            continue
        work_history.append({
            "company": company,
            "title": title,
            "location": _clean(locations[i]) if i < len(locations) else None,
            "start": _opt_date(starts[i]) if i < len(starts) else None,
            "end": _opt_date(ends[i]) if i < len(ends) else None,
            "bullets": _lines(bullets[i]) if i < len(bullets) else [],
            "skills": _csv(skills_cols[i]) if i < len(skills_cols) else [],
        })

    # Education — parallel arrays.
    education: List[Dict[str, Any]] = []
    schools = gl("edu_school")
    degrees = gl("edu_degree")
    fields = gl("edu_field")
    edu_starts = gl("edu_start")
    edu_ends = gl("edu_end")
    gpas = gl("edu_gpa")
    for i in range(max(len(schools), len(degrees))):
        school = _clean(schools[i]) if i < len(schools) else ""
        degree = _clean(degrees[i]) if i < len(degrees) else ""
        if not school and not degree:
            continue
        education.append({
            "school": school,
            "degree": degree,
            "field": _clean(fields[i]) if i < len(fields) else None,
            "start": _opt_date(edu_starts[i]) if i < len(edu_starts) else None,
            "end": _opt_date(edu_ends[i]) if i < len(edu_ends) else None,
            "gpa": _opt_float(gpas[i]) if i < len(gpas) else None,
            "highlights": [],
        })

    # Skills — parallel arrays.
    skills: List[Dict[str, Any]] = []
    skill_names = gl("skill_name")
    skill_cats = gl("skill_category")
    skill_years = gl("skill_years")
    skill_evidence = gl("skill_evidence")
    for i in range(len(skill_names)):
        name = _clean(skill_names[i])
        if not name:
            continue
        skills.append({
            "name": name,
            "category": _clean(skill_cats[i]) if i < len(skill_cats) else None,
            "years": _opt_float(skill_years[i]) if i < len(skill_years) else None,
            "evidence": (_clean(skill_evidence[i]) if i < len(skill_evidence) else "") or None,
        })

    # Certifications — parallel arrays.
    certifications: List[Dict[str, Any]] = []
    cert_names = gl("cert_name")
    cert_issuers = gl("cert_issuer")
    cert_earned = gl("cert_earned")
    cert_expires = gl("cert_expires")
    for i in range(len(cert_names)):
        name = _clean(cert_names[i])
        if not name:
            continue
        certifications.append({
            "name": name,
            "issuer": _clean(cert_issuers[i]) if i < len(cert_issuers) else "",
            "date_earned": _opt_date(cert_earned[i]) if i < len(cert_earned) else None,
            "expires": _opt_date(cert_expires[i]) if i < len(cert_expires) else None,
        })

    work_modes = [m for m in gl("pref_work_modes") if m in {"remote", "hybrid", "onsite"}]
    try:
        stretch = max(0, min(100, int(_clean(g("pref_stretch")) or 30)))
    except (ValueError, TypeError):
        stretch = 30

    preferences = {
        "salary_min": _opt_int(g("pref_salary_min")),
        "salary_target": _opt_int(g("pref_salary_target")),
        "currency": _clean(g("pref_currency")) or "USD",
        "locations": _lines(g("pref_locations")),
        "work_modes": work_modes,
        "seniority_levels": _csv(g("pref_seniority")),
        "industries": _csv(g("pref_industries")),
        "exclude_companies": _lines(g("pref_exclude")),
        "stretch_slider": stretch,
    }

    return {
        "contact": contact,
        "summary": _clean(g("summary")),
        "work_history": work_history,
        "education": education,
        "skills": skills,
        "certifications": certifications,
        "preferences": preferences,
    }


@router.get("/", response_class=HTMLResponse)
def view_profile(
    request: Request,
    saved: int = 0,
    session: Session = Depends(get_session),
) -> HTMLResponse:
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
            "saved": bool(saved),
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
    try:
        upload_dir = settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / (file.filename or "resume.pdf")
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        parsed = import_resume(dest)
    except Exception as e:  # noqa: BLE001 - parsing/IO can fail many ways
        print(f"[profile] import failed: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't parse that resume (the AI model may be busy, or the file "
            "format isn't supported). Nothing was saved — try again or edit the "
            "profile manually below.",
        )

    profile = _get_or_create_profile(session)
    profile.contact = parsed.get("contact") or {}
    profile.summary = parsed.get("summary") or ""
    profile.work_history = parsed.get("work_history") or []
    profile.education = parsed.get("education") or []
    profile.skills = parsed.get("skills") or []
    profile.certifications = parsed.get("certifications") or []
    profile.updated_at = datetime.utcnow()
    try:
        session.add(profile)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[profile] import save failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Parsed the resume but couldn't save it. Please try again.")
    return RedirectResponse(url="/profile?saved=1", status_code=303)


# Source-configuration keys live in `preferences` but are owned by the other
# panels (search filters, external companies). The structured editor must not
# clobber them, so we carry them over on every save.
_SOURCE_PREF_KEYS = {
    "apple_search_queries",
    "apple_search_query_rationales",
    "apple_search_width",
    "apple_search_max_pages",
    "apple_search_url",
    "external_companies",
    "use_bundled_directory",
    "enable_remoteok",
    "enable_hn_whoishiring",
    "hn_limit",
}


@router.post("/save")
async def save_profile(
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Save the structured profile editor. Validates and never 500s on bad input."""
    try:
        form = await request.form()
        data = parse_profile_form(form)
    except Exception as e:  # noqa: BLE001 - parsing should be total; guard anyway
        print(f"[profile] form parse failed: {type(e).__name__}: {e}")
        raise HTTPException(
            400,
            "Couldn't read the profile form. Nothing was saved — please try again.",
        )

    if not data["contact"].get("name"):
        raise HTTPException(
            400,
            "Your name is required (Contact → Full name). Nothing was saved.",
        )

    profile = _get_or_create_profile(session)
    # Preserve source-config keys the editor doesn't own.
    merged_prefs = dict(data["preferences"])
    for key in _SOURCE_PREF_KEYS:
        if key in (profile.preferences or {}):
            merged_prefs[key] = profile.preferences[key]
    data["preferences"] = merged_prefs

    for field in ("contact", "summary", "work_history", "education",
                  "skills", "certifications", "preferences"):
        setattr(profile, field, data[field])
    profile.updated_at = datetime.utcnow()
    try:
        session.add(profile)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[profile] save failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save your profile right now. Please try again.")
    return RedirectResponse(url="/profile?saved=1", status_code=303)


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
    try:
        prefs["hn_limit"] = max(10, min(200, int(hn_limit)))
    except (ValueError, TypeError):
        prefs["hn_limit"] = 40
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    try:
        session.add(profile)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[profile] external save failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save those sources right now. Please try again.")
    return RedirectResponse(url="/profile?saved=1", status_code=303)


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
    try:
        suggestions = suggest_search_queries(profile.model_dump(), width=width)
    except Exception as e:  # noqa: BLE001 - LLM call can fail/time out
        print(f"[profile] regenerate queries failed: {type(e).__name__}: {e}")
        raise HTTPException(
            503,
            "Couldn't generate search queries right now (the AI model may be "
            "busy). Please try again in a moment.",
        )
    prefs = dict(profile.preferences or {})
    prefs["apple_search_queries"] = [s["query"] for s in suggestions if s.get("query")]
    prefs["apple_search_query_rationales"] = {s["query"]: s.get("rationale", "") for s in suggestions}
    prefs["apple_search_width"] = width
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    try:
        session.add(profile)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[profile] regenerate save failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save the generated queries. Please try again.")
    return RedirectResponse(url="/profile?saved=1", status_code=303)


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
    try:
        prefs["apple_search_max_pages"] = max(1, min(20, int(max_pages)))
    except (ValueError, TypeError):
        prefs["apple_search_max_pages"] = 3
    prefs["apple_search_url"] = custom_url.strip()
    profile.preferences = prefs
    profile.updated_at = datetime.utcnow()
    try:
        session.add(profile)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[profile] search save failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Couldn't save the search filters. Please try again.")
    return RedirectResponse(url="/profile?saved=1", status_code=303)
