"""ATS-safe normalization for application autofill.

Workday (and other ATS) parse an uploaded resume into structured fields, and
routinely mangle dates, phone numbers, company names, and titles. Rather than
fight the parser, the extension fills clean values directly — this module is
where the cleaning happens.

Everything here is PURE (no DB, no I/O) so it's trivially unit-testable. The
one impure bit — assembling a job's application data from the Profile + tailored
Resume — lives in ``build_application_data`` and takes already-loaded dicts.

Design note: functions are deliberately small and standalone so a second ATS
(Greenhouse, Lever) can reuse the same normalizers with a different field map.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTH_ABBR = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_CURRENT_WORDS = {"present", "current", "now", "ongoing", "today", "présent"}


def _empty_date() -> Dict[str, Any]:
    return {"display": "", "month": None, "year": None, "current": False}


def _current_date() -> Dict[str, Any]:
    return {"display": "Present", "month": None, "year": None, "current": True}


def _build_date(month: Optional[int], year: Optional[int]) -> Dict[str, Any]:
    if month is not None and not (1 <= month <= 12):
        month = None
    if year is not None:
        if year < 100:  # two-digit year → assume 19xx/20xx
            year += 2000 if year < 50 else 1900
        if not (1900 <= year <= 2100):
            year = None
    if year is None and month is None:
        return _empty_date()
    if year is not None and month is not None:
        display = f"{_MONTH_ABBR[month]} {year}"
    elif year is not None:
        display = str(year)
    else:  # month only, no year — not useful on its own
        return _empty_date()
    return {"display": display, "month": month, "year": year, "current": False}


def normalize_date(raw: Any) -> Dict[str, Any]:
    """Normalize a messy date into a consistent structure.

    Returns ``{"display": "Jan 2023", "month": 1, "year": 2023, "current": bool}``.
    Workday needs the numeric month/year separately (From Month / From Year
    selects) plus the "I currently work here" flag, so we surface all three.

    Handles: ``date``/``datetime`` objects, ISO ("2023-01", "2023-01-15",
    "2023/01"), US ("01/2023", "1/2023", "01-2023", "03/15/2023"), month names
    ("Jan 2023", "January 2023", "Jan. 2023", "Sept 2023"), year-only ("2023"),
    and "Present"/"Current"/"Now"/"" → current / empty.
    """
    if raw is None:
        return _empty_date()
    if isinstance(raw, (date, datetime)):
        return _build_date(raw.month, raw.year)

    s = str(raw).strip()
    if not s:
        return _empty_date()
    if s.lower() in _CURRENT_WORDS:
        return _current_date()

    # Month name form: "January 2023", "Jan 2023", "Jan. 2023", "Sept 2023".
    m = re.match(r"^([A-Za-z]{3,9})\.?\s*[,]?\s*(\d{4})$", s)
    if m and m.group(1).lower() in _MONTH_NAMES:
        return _build_date(_MONTH_NAMES[m.group(1).lower()], int(m.group(2)))

    # "2023" (year only) or a stray textual month with no year.
    if re.match(r"^\d{4}$", s):
        return _build_date(None, int(s))
    if s.lower() in _MONTH_NAMES:
        return _empty_date()  # month with no year is not fillable

    # Numeric groups separated by - / or . — figure out which is the year.
    parts = [p for p in re.split(r"[/\-.\s]+", s) if p.isdigit()]
    if not parts:
        return _empty_date()
    ints = [int(p) for p in parts]

    year = None
    year_idx = None
    for i, (p, v) in enumerate(zip(parts, ints)):
        if len(p) == 4:
            year = v
            year_idx = i
            break
    remaining = [v for i, v in enumerate(ints) if i != year_idx]

    if year is None:
        # No 4-digit token: e.g. "01/23" (MM/YY) or "1/2/23". Last is the year.
        year = ints[-1]
        remaining = ints[:-1]

    month = None
    for v in remaining:
        if 1 <= v <= 12:
            month = v
            break
    return _build_date(month, year)


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(raw: Any) -> str:
    """Normalize a phone number to one consistent E.164-ish string.

    US 10-digit and 1+10-digit numbers become "+1XXXXXXXXXX". Anything that
    already starts with "+" keeps its country code. Junk in → best-effort out
    (never raises); empty/garbage → "".
    """
    if not raw:
        return ""
    s = str(raw).strip()
    had_plus = s.startswith("+")
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return ""
    if had_plus:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


# ---------------------------------------------------------------------------
# Company / title / location / name
# ---------------------------------------------------------------------------

def _clean_ws(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize_company(raw: Any) -> str:
    """Canonical company name: collapse whitespace, strip surrounding quotes and
    trailing punctuation. We keep legal suffixes (Inc., LLC) — they're part of
    the name an ATS expects — but drop noise like a trailing comma or bullet."""
    s = _clean_ws(raw)
    s = s.strip("\"'“”‘’ ")
    s = re.sub(r"[\s,;·•|]+$", "", s)
    return s


def normalize_title(raw: Any) -> str:
    """Standardized job title for display/fill.

    Job titles copied off listings carry trailing noise: " - Company",
    " | Site", "(Remote)", "- Apply Now". Strip those but preserve the title's
    own casing (we don't title-case — that would mangle "iOS", "ML")."""
    s = _clean_ws(raw)
    s = re.sub(r"\s*\([^)]*\)", "", s)              # "(Remote)" parentheticals
    s = re.sub(r"\s*[|–—].*$", "", s)               # " | site" / " – company"
    s = re.sub(r"\s+-\s+.*$", "", s)                # " - Company" (spaced hyphen only)
    return _clean_ws(s)


def normalize_location(raw: Any) -> Dict[str, str]:
    """Best-effort 'City, State, Country' → parts + a cleaned display string.

    No geocoder — this is a single-user tool. Returns
    ``{"display", "city", "region", "country"}``.
    """
    s = _clean_ws(raw)
    if not s:
        return {"display": "", "city": "", "region": "", "country": ""}
    parts = [p.strip() for p in s.split(",") if p.strip()]
    display = ", ".join(parts)
    if len(parts) == 1:
        return {"display": display, "city": parts[0], "region": "", "country": ""}
    if len(parts) == 2:
        return {"display": display, "city": parts[0], "region": parts[1], "country": ""}
    return {"display": display, "city": parts[0], "region": parts[1], "country": parts[-1]}


def split_name(full: Any) -> Dict[str, str]:
    """Full name → first/last (+ cleaned full). Single-token names get an empty
    last name; 3+ tokens fold the middle names into last (ATS wants two boxes)."""
    s = _clean_ws(full)
    parts = s.split()
    if not parts:
        return {"first_name": "", "last_name": "", "full_name": ""}
    if len(parts) == 1:
        return {"first_name": parts[0], "last_name": "", "full_name": s}
    return {"first_name": parts[0], "last_name": " ".join(parts[1:]), "full_name": s}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _first_nonempty(*vals: Any) -> Any:
    for v in vals:
        if isinstance(v, (list, dict)):
            if v:
                return v
        elif v not in (None, ""):
            return v
    return None


def _merge_section(profile_items: Any, resume_items: Any) -> List[Dict[str, Any]]:
    """Prefer the tailored resume's list when it has content, else the profile's.

    We don't try to reconcile row-by-row: a tailored resume rephrases bullets
    but keeps the same companies/titles/dates, so whichever list is populated
    is internally consistent. Profile is the canonical fallback.
    """
    r = resume_items if isinstance(resume_items, list) else []
    p = profile_items if isinstance(profile_items, list) else []
    chosen = r if r else p
    return [x for x in chosen if isinstance(x, dict)]


def _work_item(w: Dict[str, Any]) -> Dict[str, Any]:
    start = normalize_date(w.get("start"))
    raw_end = w.get("end")
    end = normalize_date(raw_end)
    # A role is current when its end date is empty/missing or literally "Present".
    current = end["current"] or not _first_nonempty(raw_end)
    bullets = w.get("bullets") or []
    return {
        "company": normalize_company(w.get("company")),
        "title": normalize_title(w.get("title")),
        "location": normalize_location(w.get("location") or "")["display"],
        "current": current,
        "currently_work_here": current,  # alias the extension engine already reads
        # Structured date objects.
        "start": start,
        "end": _empty_date() if current else end,
        # Flat convenience fields the Workday adapter fills directly.
        "start_display": start["display"],
        "start_month": start["month"],
        "start_year": start["year"],
        "end_display": "" if current else end["display"],
        "end_month": None if current else end["month"],
        "end_year": None if current else end["year"],
        "bullets": bullets,
        "description": "\n• ".join(bullets) if bullets else "",
        "skills": w.get("skills") or [],
    }


def _education_item(e: Dict[str, Any]) -> Dict[str, Any]:
    start = normalize_date(e.get("start"))
    end = normalize_date(e.get("end"))
    highlights = e.get("highlights") or []
    return {
        "school": normalize_company(e.get("school")),
        "degree": _clean_ws(e.get("degree")),
        "field_of_study": _clean_ws(e.get("field") or e.get("field_of_study")),
        "gpa": e.get("gpa"),
        "start": start,
        "end": end,
        "start_display": start["display"],
        "start_month": start["month"],
        "start_year": start["year"],
        "end_display": end["display"],
        "end_month": end["month"],
        "end_year": end["year"],
        "description": "\n• ".join(highlights) if highlights else "",
    }


def build_application_data(
    job: Any,
    profile: Dict[str, Any],
    resume_content: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the normalized, ATS-safe application data for one job.

    Sources: the master ``profile`` dict (canonical) overlaid with the job's
    tailored ``resume_content`` where the resume supplies richer sections. All
    dates are provided BOTH as "MMM YYYY" display strings AND separate numeric
    month/year fields plus a current-role bool — exactly what Workday's split
    date selects + "I currently work here" checkbox need.

    The returned dict is a SUPERSET of the older ``autofill-payload`` shape
    (same flat identity keys) so the extension can consume it as a drop-in
    replacement for every fill path (heuristic, sections, and LLM).
    """
    profile = profile or {}
    resume = resume_content or {}

    contact = _first_nonempty(resume.get("contact"), profile.get("contact")) or {}
    if not isinstance(contact, dict):
        contact = {}

    name_parts = split_name(contact.get("name") or "")
    loc = normalize_location(contact.get("location") or "")

    work_src = _merge_section(profile.get("work_history"), resume.get("work_history"))
    edu_src = _merge_section(profile.get("education"), resume.get("education"))
    work_history = [_work_item(w) for w in work_src]
    education = [_education_item(e) for e in edu_src]

    skills_src = _first_nonempty(resume.get("skills"), profile.get("skills")) or []
    skill_names = [
        s.get("name") if isinstance(s, dict) else str(s)
        for s in skills_src
        if (s.get("name") if isinstance(s, dict) else s)
    ]

    summary = _first_nonempty(resume.get("summary"), profile.get("summary")) or ""
    current_role = work_history[0] if work_history else {}

    contact_out = {
        "full_name": name_parts["full_name"],
        "first_name": name_parts["first_name"],
        "last_name": name_parts["last_name"],
        "email": contact.get("email") or "",
        "phone": normalize_phone(contact.get("phone") or ""),
        "phone_raw": contact.get("phone") or "",
        "location": loc["display"],
        "city": loc["city"],
        "region": loc["region"],
        "country": loc["country"],
        "linkedin": contact.get("linkedin") or "",
        "github": contact.get("github") or "",
        "portfolio": contact.get("portfolio") or "",
    }

    job_ctx = {
        "id": getattr(job, "id", None),
        "title": getattr(job, "title", None),
        "company": getattr(job, "company", None),
        "url": getattr(job, "url", None),
    }

    return {
        "job": job_ctx,
        "contact": contact_out,
        "work_history": work_history,
        "education": education,
        "skills": skill_names,
        "summary": summary,
        # ---- Flat identity keys (drop-in compatibility w/ autofill-payload) ----
        "first_name": contact_out["first_name"],
        "last_name": contact_out["last_name"],
        "full_name": contact_out["full_name"],
        "email": contact_out["email"],
        "phone": contact_out["phone"],
        "phone_e164": contact_out["phone"],
        "linkedin_url": contact_out["linkedin"],
        "github_url": contact_out["github"],
        "portfolio_url": contact_out["portfolio"],
        "website_url": contact_out["portfolio"],
        "location": contact_out["location"],
        "city": contact_out["city"],
        "region": contact_out["region"],
        "country": contact_out["country"],
        "current_company": current_role.get("company") or "",
        "current_title": current_role.get("title") or "",
        "skills_csv": ", ".join(skill_names),
        "_job": job_ctx,  # legacy alias
    }
