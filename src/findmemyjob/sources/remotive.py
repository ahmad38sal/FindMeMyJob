"""Remotive — public remote-jobs API, no auth.

API: https://remotive.com/api/remote-jobs?search=<kw>&limit=<n>
Returns {"jobs": [...]} with id, title, company_name, candidate_required_location,
description (HTML), url, salary, publication_date (ISO-8601), job_type.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from findmemyjob.models import Job

_UA = {"User-Agent": "FindMeMyJob/0.1 (personal job-search tool)"}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text("\n", strip=True)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def fetch_search(search: str = "", *, limit: int = 100) -> List[Job]:
    params = {"limit": str(limit)}
    if search:
        params["search"] = search
    resp = httpx.get(
        "https://remotive.com/api/remote-jobs", params=params, timeout=30, headers=_UA
    )
    resp.raise_for_status()
    data = resp.json()
    out: List[Job] = []
    for j in data.get("jobs", []):
        title = j.get("title") or ""
        company = j.get("company_name") or ""
        if not title or not company:
            continue
        out.append(Job(
            source="remotive",
            source_id=str(j.get("id") or j.get("url") or title),
            title=title,
            company=company,
            team=j.get("category") or None,
            location=j.get("candidate_required_location") or "Remote",
            work_mode="remote",
            description=_strip_html(j.get("description", "")),
            url=j.get("url") or "",
            posted_at=_parse_dt(j.get("publication_date")),
            fetched_at=datetime.utcnow(),
            raw={"job_type": j.get("job_type"), "salary": j.get("salary")},
        ))
    return out


class RemotiveSource:
    name = "remotive"

    def fetch(self, *, query: str = "", limit: int = 100) -> List[Job]:
        try:
            return fetch_search(query, limit=limit)
        except httpx.HTTPError as e:
            print(f"[remotive] {e}")
            return []
