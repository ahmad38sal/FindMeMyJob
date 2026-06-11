"""Lever public postings source.

Lever hosts company careers at jobs.lever.co/<slug>. Their public API:
    https://api.lever.co/v0/postings/<slug>?mode=json
Returns a JSON list with id, text (title), categories (team/location/commitment),
descriptionPlain, hostedUrl. No auth.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import httpx

from findmemyjob.models import Job


def fetch_jobs_for_slug(slug: str, *, limit: Optional[int] = None) -> List[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    resp = httpx.get(url, params={"mode": "json"}, timeout=30,
                     headers={"User-Agent": "FindMeMyJob/0.1"})
    resp.raise_for_status()
    data = resp.json()
    items = data[:limit] if (limit and isinstance(data, list)) else data

    out: List[Job] = []
    for j in items:
        cats = j.get("categories") or {}
        team = cats.get("team") or cats.get("department")
        location = cats.get("location") or ""
        # descriptionPlain is the main body; some postings include lists/additional too.
        body_parts = [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
        for sec in (j.get("lists") or []):
            content = sec.get("content") or ""
            text = re.sub(r"<[^>]+>", "", content).strip()
            if text:
                body_parts.append(f"## {sec.get('text', '')}\n{text}")
        description = "\n\n".join(p for p in body_parts if p).strip()

        out.append(Job(
            source="lever",
            source_id=str(j.get("id") or j.get("hostedUrl") or ""),
            title=j.get("text", "") or "",
            company=slug.capitalize(),  # Lever doesn't expose display name in this endpoint
            team=team,
            location=location,
            description=description,
            url=j.get("hostedUrl") or "",
            fetched_at=datetime.utcnow(),
            raw={"slug": slug, "categories": cats, "commitment": cats.get("commitment")},
        ))
    return out


def fetch_one_by_url(url: str) -> Job:
    """Pull a single job from a Lever-hosted detail URL."""
    parsed = urlparse(url)
    m = re.search(r"^/([^/]+)/([0-9a-f-]+)", parsed.path)
    if not m:
        raise ValueError(f"Doesn't look like a Lever job URL: {url}")
    slug, posting_id = m.group(1), m.group(2)

    resp = httpx.get(
        f"https://api.lever.co/v0/postings/{slug}/{posting_id}",
        params={"mode": "json"}, timeout=30,
        headers={"User-Agent": "FindMeMyJob/0.1"},
    )
    resp.raise_for_status()
    j = resp.json()
    cats = j.get("categories") or {}
    team = cats.get("team") or cats.get("department")
    location = cats.get("location") or ""
    body_parts = [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
    for sec in (j.get("lists") or []):
        content = re.sub(r"<[^>]+>", "", sec.get("content") or "").strip()
        if content:
            body_parts.append(f"## {sec.get('text', '')}\n{content}")
    description = "\n\n".join(p for p in body_parts if p).strip()

    return Job(
        source="lever",
        source_id=str(j.get("id") or posting_id),
        title=j.get("text", "") or "",
        company=slug.capitalize(),
        team=team,
        location=location,
        description=description,
        url=j.get("hostedUrl") or url,
        fetched_at=datetime.utcnow(),
        raw={"slug": slug, "manual_add": True},
    )


class LeverSource:
    name = "lever"

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
                if e.response.status_code != 404:
                    print(f"[lever] {slug}: {e}")
            except httpx.HTTPError as e:
                print(f"[lever] {slug}: {e}")
            if len(results) >= limit:
                break
        return results[:limit]
