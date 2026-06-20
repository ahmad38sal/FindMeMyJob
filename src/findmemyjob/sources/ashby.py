"""Ashby public job-board source.

Ashby hosts company careers at jobs.ashbyhq.com/<org>. Their public posting API:
    https://api.ashbyhq.com/posting-api/job-board/<org>
Returns {"jobs": [...]} with id, title, location, employmentType, isRemote,
publishedAt, jobUrl, descriptionPlain/descriptionHtml. No auth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

import httpx
from bs4 import BeautifulSoup

from findmemyjob.models import Job

_UA = {"User-Agent": "FindMeMyJob/0.1 (personal job-search tool)"}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text("\n", strip=True)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Ashby publishedAt is ISO-8601 (often with trailing Z)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def fetch_jobs_for_org(org: str, *, limit: Optional[int] = None) -> List[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
    resp = httpx.get(url, params={"includeCompensation": "true"}, timeout=30, headers=_UA)
    resp.raise_for_status()
    data = resp.json()
    postings = data.get("jobs") or []
    items = postings[:limit] if limit else postings

    out: List[Job] = []
    for j in items:
        title = j.get("title") or ""
        if not title:
            continue
        is_remote = bool(j.get("isRemote"))
        location = j.get("location") or ("Remote" if is_remote else "")
        description = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
        out.append(Job(
            source="ashby",
            source_id=str(j.get("id") or j.get("jobUrl") or title),
            title=title,
            company=org.capitalize(),
            team=j.get("department") or j.get("team") or None,
            location=location,
            work_mode="remote" if is_remote else None,
            description=description,
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            posted_at=_parse_dt(j.get("publishedAt")),
            fetched_at=datetime.utcnow(),
            raw={"org": org, "employmentType": j.get("employmentType")},
        ))
    return out


def fetch_one_by_url(url: str) -> Job:
    """Pull a single job from an Ashby-hosted detail URL.

    URL forms: jobs.ashbyhq.com/<org>/<uuid>  (org is the first path segment).
    We fetch the org's board and match the posting by id or URL.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(f"Doesn't look like an Ashby job URL: {url}")
    org = parts[0].lower()
    posting_id = parts[1] if len(parts) > 1 else ""

    jobs = fetch_jobs_for_org(org)
    for j in jobs:
        if posting_id and (j.source_id == posting_id or posting_id in (j.url or "")):
            j.raw = {**(j.raw or {}), "manual_add": True}
            return j
    if jobs:
        jobs[0].raw = {**(jobs[0].raw or {}), "manual_add": True}
        return jobs[0]
    raise ValueError(f"No open Ashby postings found for org {org!r}")


class AshbySource:
    name = "ashby"

    def __init__(self, orgs: Iterable[str]) -> None:
        self.orgs = [o.strip().lower() for o in orgs if o and o.strip()]

    def fetch(self, *, query: str = "", limit: int = 1000) -> List[Job]:
        results: List[Job] = []
        for org in self.orgs:
            try:
                jobs = fetch_jobs_for_org(org, limit=None)
                if query:
                    q = query.lower()
                    jobs = [j for j in jobs if q in j.title.lower() or q in j.description.lower()]
                results.extend(jobs)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    print(f"[ashby] {org}: {e}")
            except httpx.HTTPError as e:
                print(f"[ashby] {org}: {e}")
            if len(results) >= limit:
                break
        return results[:limit]
