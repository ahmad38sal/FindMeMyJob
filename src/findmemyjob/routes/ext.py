"""Endpoints consumed by the FindMeMyJob MV3 browser extension.

Why a separate router: the HTMX UI uses redirect-on-POST and HTML responses;
the extension needs JSON + CORS + bearer-token auth. Keeping it isolated avoids
sprinkling auth checks into the main routes.

Auth: bearer token from settings.ext_token. If unset, every endpoint 503s
(extension API disabled) — never wide-open.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from findmemyjob.config import settings
from findmemyjob.db import get_session
from findmemyjob.llm import llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import (
    Application,
    ApplicationStatus,
    ApplyMode,
    Job,
    Profile,
    Resume,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_token(request: Request) -> None:
    token = settings.ext_token
    if not token:
        raise HTTPException(503, "Extension API disabled — set FINDMEMYJOB_EXT_TOKEN")
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    if auth.split(" ", 1)[1].strip() != token:
        raise HTTPException(401, "Bad token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    """url-without-query-or-fragment, lowercased host, trailing slash trimmed."""
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


def _normalize_title(title: str) -> str:
    # Job listing titles are noisy: " - Apply Now", " | Greenhouse", "(Remote)".
    t = title.lower()
    t = re.sub(r"[\|\-—–•·]+.*$", "", t)         # drop trailing " - Company" / " | site"
    t = re.sub(r"\([^)]*\)", "", t)              # drop "(Remote)" parentheticals
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _split_name(full: str) -> Dict[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return {"first_name": "", "last_name": ""}
    if len(parts) == 1:
        return {"first_name": parts[0], "last_name": ""}
    return {"first_name": parts[0], "last_name": " ".join(parts[1:])}


_PHONE_DIGITS = re.compile(r"\D+")


def _phone_e164(phone: str) -> str:
    if not phone:
        return ""
    digits = _PHONE_DIGITS.sub("", phone)
    if not digits:
        return ""
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _split_location(location: str) -> Dict[str, str]:
    """Best-effort 'City, State, Country' → parts. Don't pull in a geocoder for a personal tool."""
    if not location:
        return {"city": "", "region": "", "country": ""}
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) == 1:
        return {"city": parts[0], "region": "", "country": ""}
    if len(parts) == 2:
        return {"city": parts[0], "region": parts[1], "country": ""}
    return {"city": parts[0], "region": parts[1], "country": parts[-1]}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MatchByUrlIn(BaseModel):
    url: Optional[str] = None
    page_title: Optional[str] = None
    company: Optional[str] = None
    # When set, skip URL matching and resolve directly. Used by the extension
    # to look up a job pinned from a previous page (e.g. listing → apply form
    # is a different URL on Workday but should still resolve to the same job).
    job_id: Optional[int] = None


class MatchByUrlOut(BaseModel):
    job_id: Optional[int] = None
    title: Optional[str] = None
    company: Optional[str] = None
    match_score: Optional[float] = None
    match_reasoning: Optional[str] = None
    tailored_resume_available: bool = False
    keywords_targeted: List[str] = []
    suggest_action: str  # "attach-and-autofill" | "tailor" | "score" | "track" | "open"


class TrackUrlIn(BaseModel):
    url: str
    page_title: Optional[str] = None
    company: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
def health(request: Request) -> Dict[str, Any]:
    _require_token(request)
    return {"ok": True, "service": "findmemyjob-ext", "version": 1}


@router.post("/match-by-url", response_model=MatchByUrlOut)
def match_by_url(
    payload: MatchByUrlIn,
    request: Request,
    session: Session = Depends(get_session),
) -> MatchByUrlOut:
    """Resolve a browser tab to a Job in our tracker.

    Resolution order:
      0. payload.job_id (used when a tab is pinned to a known job_id by the
         extension — the apply-form URL won't match the tracked listing URL).
      1. exact URL match
      2. URL-without-query match
      3. (company + normalized title) match
    """
    _require_token(request)

    job: Optional[Job] = None

    if payload.job_id:
        job = session.get(Job, payload.job_id)
        if job is None:
            return MatchByUrlOut(suggest_action="track")
    else:
        raw_url = (payload.url or "").strip()
        if not raw_url:
            raise HTTPException(400, "url or job_id required")
        norm = _normalize_url(raw_url)

        job = session.exec(select(Job).where(Job.url == raw_url)).first()
        if job is None:
            # Match against any Job whose normalized URL agrees. Cheap because
            # we only have personal-scale jobs in the table.
            for candidate in session.exec(select(Job)).all():
                if candidate.url and _normalize_url(candidate.url) == norm:
                    job = candidate
                    break

        if job is None and payload.company and payload.page_title:
            target_company = payload.company.strip().lower()
            target_title = _normalize_title(payload.page_title)
            for candidate in session.exec(select(Job)).all():
                if (candidate.company or "").strip().lower() != target_company:
                    continue
                if _normalize_title(candidate.title or "") == target_title:
                    job = candidate
                    break

        if job is None:
            return MatchByUrlOut(suggest_action="track")

    app = session.exec(select(Application).where(Application.job_id == job.id)).first()
    resume = session.get(Resume, app.tailored_resume_id) if (app and app.tailored_resume_id) else None
    has_pdf = bool(resume and resume.pdf_path)

    if has_pdf:
        action = "attach-and-autofill"
    elif app is not None and app.match_score is not None:
        action = "tailor"
    else:
        action = "score"

    return MatchByUrlOut(
        job_id=job.id,
        title=job.title,
        company=job.company,
        match_score=app.match_score if app else None,
        match_reasoning=(app.match_reasoning if app else None),
        tailored_resume_available=has_pdf,
        keywords_targeted=(resume.keywords_targeted if resume else []) or [],
        suggest_action=action,
    )


@router.get("/jobs/{job_id}/autofill-payload")
def autofill_payload(
    job_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Flat canonical-key payload that adapters consume.

    Keys are intentionally narrow — only what at least one surveyed ATS asks for.
    Adapter authors: when adding a new ATS, prefer mapping its field to one of
    these existing keys before adding a new one here.
    """
    _require_token(request)
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    profile = session.get(Profile, 1)
    if profile is None:
        raise HTTPException(400, "profile not set up")

    contact: Dict[str, Any] = profile.contact or {}
    name_parts = _split_name(contact.get("name") or "")
    loc_parts = _split_location(contact.get("location") or "")

    work_history = profile.work_history or []
    current = work_history[0] if work_history else {}

    skills = profile.skills or []
    skill_names = [s.get("name") for s in skills if isinstance(s, dict) and s.get("name")]

    # Repeating-row sections (Workday "My Experience" page, etc.) want the
    # full lists. Adapters split start/end into month + year parts.
    work_history_clean = [
        {
            "company": w.get("company") or "",
            "title": w.get("title") or "",
            "location": w.get("location") or "",
            "start": w.get("start") or "",
            "end": w.get("end") or "",
            "currently_work_here": not w.get("end"),
            "bullets": w.get("bullets") or [],
            "description": "\n• ".join(w.get("bullets") or []) if w.get("bullets") else "",
            "skills": w.get("skills") or [],
        }
        for w in work_history
    ]
    education_clean = [
        {
            "school": e.get("school") or "",
            "degree": e.get("degree") or "",
            "field_of_study": e.get("field") or "",
            "start": e.get("start") or "",
            "end": e.get("end") or "",
            "gpa": e.get("gpa"),
            "description": "\n• ".join(e.get("highlights") or []) if e.get("highlights") else "",
        }
        for e in (profile.education or [])
    ]
    certifications_clean = [
        {
            "name": c.get("name") or "",
            "issuer": c.get("issuer") or "",
            "date_earned": c.get("date_earned") or "",
            "expires": c.get("expires") or "",
        }
        for c in (profile.certifications or [])
    ]

    payload: Dict[str, Any] = {
        # Identity
        "first_name": name_parts["first_name"],
        "last_name": name_parts["last_name"],
        "full_name": contact.get("name") or "",
        "email": contact.get("email") or "",
        "phone": contact.get("phone") or "",
        "phone_e164": _phone_e164(contact.get("phone") or ""),

        # Links
        "linkedin_url": contact.get("linkedin") or "",
        "github_url": contact.get("github") or "",
        "portfolio_url": contact.get("portfolio") or "",
        "website_url": contact.get("portfolio") or "",  # alias

        # Location
        "location": contact.get("location") or "",
        "city": loc_parts["city"],
        "region": loc_parts["region"],
        "country": loc_parts["country"],

        # Current employment
        "current_company": current.get("company") or "",
        "current_title": current.get("title") or "",

        # Resume content (rarely asked for as a text field, but cheap to include)
        "summary": profile.summary or "",
        "skills_csv": ", ".join(skill_names),
        "skills": skill_names,

        # Repeating-list sections — adapters with `sections` declarations
        # consume these. Workday's "My Experience" page is the canonical case.
        "work_history": work_history_clean,
        "education": education_clean,
        "certifications": certifications_clean,

        # Job context the adapter may want
        "_job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "url": job.url,
        },
    }
    return payload


@router.get("/jobs/{job_id}/tailored-resume.pdf")
def tailored_resume_pdf(
    job_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    _require_token(request)
    app = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if not app or not app.tailored_resume_id:
        raise HTTPException(404, "no tailored resume — tailor the role first at /jobs/{job_id}")
    resume = session.get(Resume, app.tailored_resume_id)
    if not resume or not resume.pdf_path:
        raise HTTPException(404, "tailored resume has no PDF on disk")
    job = session.get(Job, job_id)
    safe_company = (job.company if job else "resume").replace(" ", "_")
    return FileResponse(
        resume.pdf_path,
        media_type="application/pdf",
        filename=f"resume-{safe_company}-{job_id}.pdf",
    )


@router.post("/track-url")
def track_url(
    payload: TrackUrlIn,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Idempotent: if a Job with this URL already exists, return its id."""
    _require_token(request)
    url = payload.url.strip()
    if not url:
        raise HTTPException(400, "url required")
    norm = _normalize_url(url)

    existing: Optional[Job] = None
    for candidate in session.exec(select(Job)).all():
        if candidate.url and _normalize_url(candidate.url) == norm:
            existing = candidate
            break
    if existing:
        return JSONResponse({"job_id": existing.id, "created": False})

    title = (payload.page_title or "Untitled role").strip()
    title = re.sub(r"\s*[\|\-—].*$", "", title) or "Untitled role"
    company = (payload.company or "Unknown").strip() or "Unknown"

    job = Job(
        source="extension",
        source_id=norm,
        title=title,
        company=company,
        url=url,
        description="",
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    # Seed an Application row so the popup can show "in tracker" immediately.
    app = Application(
        job_id=job.id,
        status=ApplicationStatus.pending,
        mode=ApplyMode.confirm,
    )
    session.add(app)
    session.commit()
    return JSONResponse({"job_id": job.id, "created": True})


# ---------------------------------------------------------------------------
# LLM-driven smart fill
# ---------------------------------------------------------------------------

class FillSuggestIn(BaseModel):
    page_url: str = ""
    page_title: str = ""
    headings: List[str] = []
    elements: List[Dict[str, Any]]
    payload: Dict[str, Any]
    already_filled_keys: List[str] = []
    pass_number: int = 1


class FillAction(BaseModel):
    element_id: str
    value: Any
    canonical_key: str = ""
    reason: str = ""


class ClickAction(BaseModel):
    element_id: str
    times: int = 1
    purpose: str = ""


class FillSuggestOut(BaseModel):
    fills: List[FillAction] = []
    clicks: List[ClickAction] = []
    needs_resnapshot: bool = False
    note: str = ""


# Stable across calls so prompt caching kicks in. If you change this, the
# cached profile block is invalidated too.
_FILL_INSTRUCTIONS = """\
You are an autofill agent for job application forms. The candidate's full
profile is in the system block above. The user message will give you a
snapshot of every visible interactable element on the current page. You
return strict JSON describing how to fill the form.

ABSOLUTE RULES:
1. Only fill from data in the candidate profile. Never fabricate.
2. NEVER fill EEO / demographics / voluntary-disclosure / self-identify
   fields — return them as skipped (just don't include them in fills).
3. NEVER fill work-authorization radios, sponsorship questions, or anything
   about visa status — too high-error-cost. Skip silently.
4. NEVER click anything labeled "Submit", "Apply", "Continue to Submit",
   "Save and Submit". Don't include those in clicks.
5. For repeating-list sections (work experience, education, certifications):
   if the candidate has N entries but the page shows fewer rows, return a
   click action for the section's "Add Another" button with `times` set to
   the number of additional rows needed, set `needs_resnapshot=true`, and
   DO NOT fill that section in this pass — fill it in the next pass after
   the engine resnapshots and calls you again.
6. For dates split into separate month + year inputs (very common in
   Workday), return MM as zero-padded two digits ("02") and YYYY as four
   digits ("2024"). Match by the element's automation_id / label that
   says "month" vs "year".
7. For checkboxes, value should be true or false (boolean).
8. For dropdowns / typeaheads / role=combobox / role=button-with-listbox,
   return the user-visible text the engine should select (e.g. "United
   States", not a code). The engine handles opening + clicking.
9. If a single element clearly maps to a profile field, fill it. If it's
   ambiguous, skip it — false fills are worse than empty.
10. canonical_key in your output should match one of the payload's top-level
    keys when possible (first_name, last_name, email, phone, phone_e164,
    city, region, country, postal_code, linkedin_url, github_url, etc.).
    For repeating-section fills use a synthetic key like
    "work_history[2].title" so the user can audit what was set.
11. Multi-select / typeahead skill inputs (Workday "Skills" widget,
    multiSelectContainer, role=combobox with aria-multiselectable=true):
    return ONE fill per selected item, all targeting the same element_id.
    Example: for skills ["Python", "Kubernetes"], emit two fills:
    {"element_id": "el_58", "value": "Python", ...} and
    {"element_id": "el_58", "value": "Kubernetes", ...}. The engine clicks
    the trigger, types each value in turn, and selects the matching option.
    Pick the most relevant 5–10 skills for THIS job (use the page's headings
    + job context if visible) — don't dump all 25.
12. Certifications: treat the same as work_history. If the page shows a
    Certifications section with N rows but the candidate has M entries,
    return clicks to grow it before filling.

OUTPUT FORMAT (strict JSON, no markdown, no commentary):
{
  "fills": [
    {"element_id": "el_3", "value": "Ahmad", "canonical_key": "first_name", "reason": "matches the firstName label"},
    ...
  ],
  "clicks": [
    {"element_id": "el_47", "times": 3, "purpose": "expand work-experience to 4 rows"}
  ],
  "needs_resnapshot": true,
  "note": "First pass: expanded work experience. Will fill rows in pass 2."
}

Element_ids must come exactly from the schema you were given. Don't invent ids.
"""


@router.post("/llm-fill-suggest", response_model=FillSuggestOut)
def llm_fill_suggest(payload: FillSuggestIn, request: Request) -> FillSuggestOut:
    """LLM-driven autofill mapping.

    The content script snapshots visible form elements and ships them here
    along with the candidate's autofill payload. Claude returns the list of
    {element_id, value} fills + any "click N times" actions for repeating
    sections.
    """
    _require_token(request)

    # Cap inputs so a hostile or huge page can't blow the context window.
    elements = payload.elements[:400]
    headings = payload.headings[:30]

    user_prompt = (
        f"PAGE URL: {payload.page_url}\n"
        f"PAGE TITLE: {payload.page_title}\n"
        f"PASS NUMBER: {payload.pass_number} (1 = first attempt, 2 = after expansion clicks)\n"
        f"ALREADY FILLED CANONICAL KEYS (don't repeat): {payload.already_filled_keys}\n"
        f"PAGE HEADINGS:\n{json.dumps(headings)}\n\n"
        f"VISIBLE FORM ELEMENTS ({len(elements)}):\n"
        f"{json.dumps(elements, indent=2)}\n\n"
        f"Return strict JSON with fills + clicks + needs_resnapshot."
    )

    try:
        raw = llm.complete_with_cached_profile(
            profile=payload.payload,
            instructions=_FILL_INSTRUCTIONS,
            user_prompt=user_prompt,
            # Sonnet for now — autofill mapping is more reasoning-heavy than
            # it sounds (matching ambiguous labels, deciding what to skip).
            # Switch to haiku here if cost becomes a concern.
            model="anthropic.claude-sonnet-4-6",
            # 8192 because a Workday experience page with 4 work entries +
            # multi-skill list + cert section can produce 60+ fills with
            # reasons — 4096 was getting truncated.
            max_tokens=8192,
            temperature=0.1,
        )
    except RuntimeError as e:
        raise HTTPException(503, f"LLM unavailable: {e}")
    except Exception as e:  # network, etc.
        raise HTTPException(502, f"LLM call failed: {e}")

    cleaned = _strip_code_fence(raw)
    parsed = _parse_fill_json(cleaned, raw)
    return FillSuggestOut(**parsed)


def _parse_fill_json(cleaned: str, raw: str) -> Dict[str, Any]:
    """Be lenient with the LLM's output.

    Sonnet usually returns clean JSON, but occasionally:
      - wraps the JSON in commentary ("Here's the mapping: {...}")
      - emits trailing text after the closing brace
      - truncates if max_tokens is exhausted (then JSON is invalid).

    We try strict parse first; on failure, regex-extract the outermost
    {...} block and try again. If that also fails, log the raw response
    so we can debug, and surface a 502 with the head of the response.
    """
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find the outermost balanced {...}
    start = cleaned.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try the truncation-recovery path next

    # Last-ditch: response was probably truncated by max_tokens. Try to
    # auto-close the JSON by counting open braces/brackets — return whatever
    # parses (callers handle missing fills gracefully).
    open_curly = cleaned.count("{") - cleaned.count("}")
    open_brack = cleaned.count("[") - cleaned.count("]")
    if open_curly > 0 or open_brack > 0:
        repaired = cleaned + ("]" * max(0, open_brack)) + ("}" * max(0, open_curly))
        # Strip trailing comma before close if present.
        repaired = re.sub(r",\s*([\]}])", r"\1", repaired)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # Give up — surface the head of the raw response so the user sees what Sonnet sent.
    print(f"[llm-fill-suggest] failed to parse LLM output. Raw response:\n{raw[:2000]}")
    raise HTTPException(
        502,
        f"LLM returned unparseable output. Head: {cleaned[:300]!r}"
    )

