"""Google Jobs discovery via SerpApi's Google Jobs engine.

Google for Jobs aggregates Indeed, LinkedIn, ZipRecruiter, Workday, Lever,
company career pages, and more, so this one source dramatically widens
discovery. We reach it through SerpApi:

    GET https://serpapi.com/search?engine=google_jobs&q=<query>&api_key=<KEY>

API KEY (read from the environment at call time):
    ==> SERPAPI_API_KEY <==
The key is injected later via the secure custom-credentials form. When the env
var is missing or empty, ``is_configured()`` is False and ``fetch()`` returns
``[]`` WITHOUT raising, so discovery keeps running with the other sources. The
key is never logged or committed.

Pagination uses ``serpapi_pagination.next_page_token`` (~10 results/page); we
loop until ``limit`` is reached or no token, capped at ``max_pages`` to control
cost. Each ``jobs_results`` item is normalized into an unpersisted ``Job`` row
(``source="google_jobs"``); the caller upserts and dedupes by source+source_id.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from findmemyjob.models import Job

SOURCE_NAME = "google_jobs"
API_KEY_ENV = "SERPAPI_API_KEY"
_BASE_URL = "https://serpapi.com/search"
_UA = {"User-Agent": "FindMeMyJob/0.1 (personal job-search tool)"}


def is_configured() -> bool:
    """True when the SerpApi key env var is set to a non-empty value."""
    return bool((os.environ.get(API_KEY_ENV) or "").strip())


def _api_key() -> str:
    return (os.environ.get(API_KEY_ENV) or "").strip()


def _strip_html(s: Any) -> str:
    if not s:
        return ""
    return BeautifulSoup(str(s), "html.parser").get_text("\n", strip=True)


# ---------------------------------------------------------------------------
# Relative "posted_at" parsing ("22 hours ago", "25 days ago", "yesterday", ...)
# ---------------------------------------------------------------------------
_REL_UNIT_DELTA = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
_REL_RE = re.compile(
    r"^(?:about\s+|over\s+|almost\s+)?"
    r"(a|an|\d+)\+?\s*"
    r"(second|minute|hour|day|week|month|year)s?\s*ago$"
)


def parse_relative_posted_at(text: Any, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a Google-Jobs relative time string into an absolute datetime.

    Handles "22 hours ago", "25 days ago", "30+ days ago", "a day ago",
    "yesterday", "today"/"just now". Returns None when unparseable.
    """
    if not text:
        return None
    now = now or datetime.utcnow()
    s = str(text).strip().lower()
    if s in ("just now", "today", "now"):
        return now
    if s == "yesterday":
        return now - timedelta(days=1)
    m = _REL_RE.match(s)
    if not m:
        return None
    qty = 1 if m.group(1) in ("a", "an") else int(m.group(1))
    return now - _REL_UNIT_DELTA[m.group(2)] * qty


# ---------------------------------------------------------------------------
# Field inference
# ---------------------------------------------------------------------------
_SENIORITY_KEYWORDS = [
    ("intern", "intern"),
    ("principal", "principal"),
    ("staff", "staff"),
    ("lead", "lead"),
    ("senior", "senior"),
    ("sr.", "senior"),
    ("junior", "junior"),
    ("jr.", "junior"),
    ("entry", "junior"),
    ("associate", "junior"),
]


def _infer_seniority(title: str) -> Optional[str]:
    low = (title or "").lower()
    for needle, level in _SENIORITY_KEYWORDS:
        if needle in low:
            return level
    return None


def _infer_work_mode(item: Dict[str, Any]) -> Optional[str]:
    det = item.get("detected_extensions") or {}
    if det.get("work_from_home") is True:
        return "remote"
    haystack = " ".join([
        str(item.get("location") or ""),
        " ".join(str(x) for x in (item.get("extensions") or [])),
    ]).lower()
    if "hybrid" in haystack:
        return "hybrid"
    if "remote" in haystack or "work from home" in haystack:
        return "remote"
    if "on-site" in haystack or "onsite" in haystack or "in office" in haystack:
        return "onsite"
    return None


def _stable_source_id(item: Dict[str, Any]) -> str:
    """SerpApi job_id when present, else a stable hash of identifying fields."""
    jid = item.get("job_id")
    if jid:
        return str(jid)
    basis = "|".join([
        str(item.get("title") or ""),
        str(item.get("company_name") or ""),
        str(item.get("location") or ""),
        str(item.get("via") or ""),
    ])
    return "gj_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _render_highlights(item: Dict[str, Any]) -> str:
    """Compact rendering of job_highlights so tailoring/matching has signal."""
    parts: List[str] = []
    for block in item.get("job_highlights") or []:
        if not isinstance(block, dict):
            continue
        title = _strip_html(block.get("title"))
        items = [_strip_html(x) for x in (block.get("items") or []) if x]
        if not items:
            continue
        header = f"{title}:" if title else ""
        parts.append("\n".join([header, *(f"- {it}" for it in items)]).strip())
    return "\n\n".join(parts)


def _apply_url(item: Dict[str, Any]) -> str:
    for opt in item.get("apply_options") or []:
        if isinstance(opt, dict) and opt.get("link"):
            return str(opt["link"])
    return str(item.get("share_link") or "")


def job_from_item(item: Dict[str, Any]) -> Optional[Job]:
    """Normalize a single SerpApi ``jobs_results`` item into a ``Job`` row."""
    if not isinstance(item, dict):
        return None
    title = _strip_html(item.get("title"))
    if not title:
        return None

    det = item.get("detected_extensions") or {}
    description = _strip_html(item.get("description"))
    highlights = _render_highlights(item)
    if highlights:
        description = f"{description}\n\n{highlights}".strip()

    raw: Dict[str, Any] = dict(item)
    raw["schedule_type"] = det.get("schedule_type")
    raw["apply_options"] = item.get("apply_options") or []

    return Job(
        source=SOURCE_NAME,
        source_id=_stable_source_id(item),
        title=title,
        company=_strip_html(item.get("company_name")) or "",
        location=_strip_html(item.get("location")) or None,
        work_mode=_infer_work_mode(item),
        seniority=_infer_seniority(title),
        description=description,
        url=_apply_url(item),
        posted_at=parse_relative_posted_at(det.get("posted_at")),
        fetched_at=datetime.utcnow(),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Fetch (paginated)
# ---------------------------------------------------------------------------
def fetch_search(
    query: str = "",
    *,
    limit: int = 100,
    location: Optional[str] = None,
    gl: str = "us",
    hl: str = "en",
    max_pages: int = 5,
) -> List[Job]:
    """Query SerpApi Google Jobs, following pagination up to ``max_pages``.

    Returns [] (no exception) when the key is unconfigured.
    """
    if not is_configured():
        print(f"[google_jobs] {API_KEY_ENV} not set — skipping (returning []).")
        return []

    out: List[Job] = []
    next_token: Optional[str] = None
    for _ in range(max(1, max_pages)):
        params: Dict[str, str] = {
            "engine": "google_jobs",
            "q": query or "",
            "api_key": _api_key(),
            "gl": gl,
            "hl": hl,
        }
        if location:
            params["location"] = location
        if next_token:
            params["next_page_token"] = next_token

        resp = httpx.get(_BASE_URL, params=params, timeout=30, headers=_UA)
        resp.raise_for_status()
        data = resp.json()

        status = ((data.get("search_metadata") or {}).get("status") or "").lower()
        if data.get("error") or status == "error":
            print(f"[google_jobs] API error: {data.get('error') or status}")
            break

        for item in data.get("jobs_results") or []:
            job = job_from_item(item)
            if job is not None:
                out.append(job)
            if len(out) >= limit:
                return out[:limit]

        next_token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not next_token:
            break

    return out[:limit]


class GoogleJobsSource:
    """Always-registered source; no-ops to [] when the API key is unconfigured."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        location: Optional[str] = None,
        gl: str = "us",
        hl: str = "en",
        max_pages: int = 5,
    ) -> None:
        self.location = location
        self.gl = gl
        self.hl = hl
        self.max_pages = max_pages

    def fetch(self, *, query: str = "", limit: int = 100) -> List[Job]:
        if not is_configured():
            return []
        try:
            return fetch_search(
                query,
                limit=limit,
                location=self.location,
                gl=self.gl,
                hl=self.hl,
                max_pages=self.max_pages,
            )
        except httpx.HTTPError as e:
            print(f"[google_jobs] {e}")
            return []
