"""Greenhouse public boards source.

Greenhouse hosts most company careers pages at boards.greenhouse.io/<slug>.
Their public API lives at boards-api.greenhouse.io/v1/boards/<slug>/jobs and
needs no auth.

Slug discovery: open boards.greenhouse.io/<company>; the slug in the URL is
what we want. Some companies use a bespoke domain that proxies to Greenhouse —
in that case, view-source and look for a `boards-api.greenhouse.io/v1/boards/<X>`
fetch to find the slug.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from findmemyjob.models import Job


def _strip_html(s: str) -> str:
    """Greenhouse 'content' field is HTML — flatten to readable text."""
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text("\n", strip=True)


def fetch_jobs_for_slug(slug: str, *, limit: Optional[int] = None) -> List[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = httpx.get(url, params={"content": "true"}, timeout=30,
                     headers={"User-Agent": "FindMeMyJob/0.1"})
    resp.raise_for_status()
    data = resp.json()
    company = (data.get("meta") or {}).get("name") or slug.capitalize()

    out: List[Job] = []
    for j in data.get("jobs", [])[:limit] if limit else data.get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        # Department often best signal for "team"
        depts = j.get("departments") or []
        team = depts[0]["name"] if depts and depts[0].get("name") else None
        # Keep the URL Greenhouse provides (works for both stock + bespoke domains)
        job_url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}"
        out.append(Job(
            source="greenhouse",
            source_id=str(j["id"]),
            title=j.get("title", "") or "",
            company=company,
            team=team,
            location=location,
            description=_strip_html(j.get("content", "")),
            url=job_url,
            fetched_at=datetime.utcnow(),
            raw={"slug": slug, "data_url_id": j.get("data_url_id")},
        ))
    return out


def fetch_one_by_url(url: str) -> Job:
    """Pull a single job from a Greenhouse-hosted detail URL."""
    parsed = urlparse(url)
    # Common forms:
    #   boards.greenhouse.io/<slug>/jobs/<id>
    #   boards.greenhouse.io/embed/job_app?token=<id>&for=<slug>  (less common)
    m = re.search(r"/(?:embed/)?(?:jobs|jobs/)/?(\d+)", parsed.path)
    slug_match = re.search(r"^/([^/]+)/", parsed.path)
    if not m or not slug_match:
        raise ValueError(f"Doesn't look like a Greenhouse job URL: {url}")
    slug = slug_match.group(1)
    job_id = m.group(1)

    resp = httpx.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
        params={"questions": "false"}, timeout=30,
        headers={"User-Agent": "FindMeMyJob/0.1"},
    )
    resp.raise_for_status()
    j = resp.json()

    company_resp = httpx.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=15,
        headers={"User-Agent": "FindMeMyJob/0.1"},
    )
    company = (company_resp.json().get("name") if company_resp.is_success else None) or slug.capitalize()

    location = (j.get("location") or {}).get("name") or ""
    depts = j.get("departments") or []
    team = depts[0]["name"] if depts and depts[0].get("name") else None
    return Job(
        source="greenhouse",
        source_id=str(j["id"]),
        title=j.get("title", "") or "",
        company=company,
        team=team,
        location=location,
        description=_strip_html(j.get("content", "")),
        url=j.get("absolute_url") or url,
        fetched_at=datetime.utcnow(),
        raw={"slug": slug, "manual_add": True},
    )


class GreenhouseSource:
    name = "greenhouse"

    def __init__(self, slugs: Iterable[str]) -> None:
        self.slugs = [s.strip().lower() for s in slugs if s and s.strip()]

    def fetch(self, *, query: str = "", limit: int = 1000) -> List[Job]:
        results: List[Job] = []
        for slug in self.slugs:
            try:
                jobs = fetch_jobs_for_slug(slug, limit=None)
                if query:
                    q = query.lower()
                    jobs = [j for j in jobs if q in j.title.lower() or q in j.description.lower()]
                results.extend(jobs)
            except httpx.HTTPStatusError as e:
                # 404 = stale slug (company migrated ATSes). Silent — expected over time.
                if e.response.status_code != 404:
                    print(f"[greenhouse] {slug}: {e}")
            except httpx.HTTPError as e:
                print(f"[greenhouse] {slug}: {e}")
            if len(results) >= limit:
                break
        return results[:limit]
