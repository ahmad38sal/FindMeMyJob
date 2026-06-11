"""HN 'Ask HN: Who is hiring?' monthly thread parser.

Each month a sticky thread appears with hundreds of top-level comments, each a
job posting in free-form text. Format is convention, not structure — we use
Claude to extract structured Job fields per comment.

API:
  https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+is+hiring&tags=story
  https://hn.algolia.com/api/v1/items/<story_id>     -> tree with children
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from findmemyjob.llm import DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import Job


def _latest_thread_id() -> Optional[int]:
    resp = httpx.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": "10"},
        timeout=20,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    # Take the most recent "Who is hiring" thread by `whoishiring` author or title match.
    for h in hits:
        title = (h.get("title") or "").lower()
        if "who is hiring" in title and "ask hn" in title:
            return int(h["objectID"])
    return None


def _fetch_thread_tree(story_id: int) -> Dict[str, Any]:
    resp = httpx.get(f"https://hn.algolia.com/api/v1/items/{story_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


_EXTRACT_INSTRUCTIONS = """\
You convert a Hacker News "Who is hiring?" comment into a structured job record.
HN format: company name, location, on-site/remote, then prose with role(s),
stack, links. A single comment may list multiple roles — pick the FIRST/PRIMARY role.

Output STRICT JSON (no commentary, no markdown). If the comment isn't a real
job posting, return {"is_job": false}:
{
  "is_job": true,
  "title": "Primary role title",
  "company": "Company name",
  "location": "Location string (else null)",
  "work_mode": "remote|hybrid|onsite (else null)",
  "salary_min": 0 or null,
  "salary_max": 0 or null,
  "url": "Apply or company URL if mentioned (else null)",
  "description": "First ~500 chars of the relevant role description, plain text"
}
"""


def _extract_one(comment_text: str) -> Optional[Dict[str, Any]]:
    raw = llm.complete(
        system=[{"type": "text", "text": _EXTRACT_INSTRUCTIONS}],
        messages=[{"role": "user", "content": f"COMMENT:\n{comment_text}\n\nReturn JSON now."}],
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=1024,
        temperature=0.1,
    )
    try:
        d = json.loads(_strip_code_fence(raw))
    except Exception:
        return None
    return d if d.get("is_job") else None


def fetch_all(limit: int = 50) -> List[Job]:
    """Pull top-level comments from the latest 'Who is hiring' thread, LLM-extract.

    Capped at `limit` because each extraction is one LLM call. Default 50
    keeps a refresh under ~1 min.
    """
    story_id = _latest_thread_id()
    if not story_id:
        return []
    tree = _fetch_thread_tree(story_id)
    children = tree.get("children") or []
    # Top-level only (job postings); skip replies.
    out: List[Job] = []
    for c in children[:limit]:
        text_html = c.get("text") or ""
        if not text_html:
            continue
        text = BeautifulSoup(text_html, "html.parser").get_text("\n", strip=True)
        if len(text) < 50:
            continue
        try:
            extracted = _extract_one(text)
        except Exception as e:
            print(f"[hn] LLM extract failed: {e}")
            continue
        if not extracted:
            continue
        author = c.get("author") or ""
        out.append(Job(
            source="hn-whoishiring",
            source_id=str(c.get("id")),
            title=(extracted.get("title") or "").strip() or "Untitled",
            company=(extracted.get("company") or "").strip() or "Unknown",
            team=author or None,
            location=(extracted.get("location") or None),
            work_mode=(extracted.get("work_mode") or None),
            salary_min=extracted.get("salary_min") or None,
            salary_max=extracted.get("salary_max") or None,
            description=text,
            url=(extracted.get("url") or f"https://news.ycombinator.com/item?id={c.get('id')}"),
            fetched_at=datetime.utcnow(),
            raw={"hn_id": c.get("id"), "story_id": story_id},
        ))
    return out


class HNWhoIsHiringSource:
    name = "hn-whoishiring"

    def __init__(self, limit: int = 50) -> None:
        self.limit = limit

    def fetch(self, *, query: str = "", limit: int = 1000) -> List[Job]:
        jobs = fetch_all(limit=min(self.limit, limit))
        if query:
            q = query.lower()
            jobs = [j for j in jobs if q in j.title.lower() or q in j.description.lower()]
        return jobs[:limit]
