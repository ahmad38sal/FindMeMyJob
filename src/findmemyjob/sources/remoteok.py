"""RemoteOK — public JSON feed of remote jobs, no auth.

API: https://remoteok.com/api
Returns a list whose first entry is metadata; the rest are postings with
id, position (title), company, location, description (HTML), tags,
url, apply_url, salary_min, salary_max, epoch.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from findmemyjob.models import Job


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text("\n", strip=True)


def _posted_at(j: dict) -> "datetime | None":
    """RemoteOK gives `epoch` (unix seconds) and/or `date` (ISO). Normalize."""
    epoch = j.get("epoch")
    if epoch:
        try:
            return datetime.utcfromtimestamp(int(epoch))
        except (ValueError, TypeError, OSError):
            pass
    date_str = j.get("date")
    if isinstance(date_str, str):
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
    return None


def fetch_all() -> List[Job]:
    resp = httpx.get(
        "https://remoteok.com/api",
        timeout=30,
        headers={"User-Agent": "FindMeMyJob/0.1 (personal job-search tool)"},
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return []
    items = data[1:] if isinstance(data[0], dict) and "legal" in data[0] else data

    out: List[Job] = []
    for j in items:
        if not isinstance(j, dict):
            continue
        title = j.get("position") or j.get("role") or ""
        company = j.get("company") or ""
        if not title or not company:
            continue
        tags = j.get("tags") or []
        team = ", ".join(tags[:3]) if tags else None
        out.append(Job(
            source="remoteok",
            source_id=str(j.get("id") or j.get("slug") or j.get("url")),
            title=title,
            company=company,
            team=team,
            location=j.get("location") or "Remote",
            work_mode="remote",
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            description=_strip_html(j.get("description", "")),
            url=j.get("url") or j.get("apply_url") or "",
            posted_at=_posted_at(j),
            fetched_at=datetime.utcnow(),
            raw={"tags": tags},
        ))
    return out


def fetch_by_tags(tags: List[str], *, limit: int = 200) -> List[Job]:
    """Keyword/tag search against the public feed (no per-tag endpoint, so we
    pull the feed once and filter client-side by tag/title match)."""
    try:
        jobs = fetch_all()
    except httpx.HTTPError as e:
        print(f"[remoteok] {e}")
        return []
    if not tags:
        return jobs[:limit]
    wanted = {t.strip().lower() for t in tags if t and t.strip()}
    out: List[Job] = []
    for j in jobs:
        hay_tags = {t.lower() for t in (j.raw or {}).get("tags", [])}
        title = j.title.lower()
        if (wanted & hay_tags) or any(w in title for w in wanted):
            out.append(j)
    return out[:limit]


class RemoteOKSource:
    name = "remoteok"

    def fetch(self, *, query: str = "", limit: int = 1000) -> List[Job]:
        try:
            jobs = fetch_all()
        except httpx.HTTPError as e:
            print(f"[remoteok] {e}")
            return []
        if query:
            q = query.lower()
            jobs = [j for j in jobs if q in j.title.lower() or q in j.description.lower()
                    or any(q in t.lower() for t in (j.raw or {}).get("tags", []))]
        return jobs[:limit]
