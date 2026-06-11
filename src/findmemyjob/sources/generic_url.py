"""Generic LLM-extract for any job URL we don't otherwise recognize.

Strategy:
  - Fetch the page with httpx (most public job pages render fine without JS).
  - Strip nav/footer/script/style; keep the main content.
  - Send to Claude with a tight schema; parse JSON.

If httpx returns nothing useful (heavy SPA, login wall), the user can fall
back to manually copying the JD into the description field — but for the vast
majority of job pages this works.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from findmemyjob.llm import DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import Job


_EXTRACT_INSTRUCTIONS = """\
You convert raw job-posting HTML/text into a structured JSON job record.
Be conservative — don't invent fields. Leave them null/empty when unclear.

Output STRICT JSON (no commentary, no markdown):
{
  "title": "Job title",
  "company": "Company name",
  "team": "Team / department / org if mentioned (else null)",
  "location": "Primary location string (else null)",
  "work_mode": "remote|hybrid|onsite (else null)",
  "salary_min": 0 or null,
  "salary_max": 0 or null,
  "currency": "USD" or other (else USD),
  "seniority": "level/title hint (e.g. 'Senior', 'Staff', 'L5') or null",
  "description": "The full job description body, plain text, with newlines preserved"
}
"""


def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:30_000]  # cap so we don't blow the LLM context window


def fetch_one_by_url(url: str) -> Job:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        raise ValueError(f"Not an HTTP URL: {url}")

    resp = httpx.get(
        url, timeout=30, follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    resp.raise_for_status()
    text = _clean_html(resp.text)
    if not text.strip():
        raise RuntimeError(
            "Couldn't extract any text from this URL — it may be a heavy SPA or "
            "require login. Paste the job description manually instead."
        )

    raw = llm.complete(
        system=[{"type": "text", "text": _EXTRACT_INSTRUCTIONS}],
        messages=[{"role": "user", "content": f"URL: {url}\n\nPAGE TEXT:\n{text}\n\nReturn JSON now."}],
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=4096,
        temperature=0.1,
    )
    data: Dict[str, Any] = json.loads(_strip_code_fence(raw))

    host = parsed.netloc.lower()
    return Job(
        source="generic",
        # No stable upstream ID — use URL as the dedup key.
        source_id=url,
        title=(data.get("title") or "Untitled").strip(),
        company=(data.get("company") or host).strip(),
        team=(data.get("team") or None),
        location=(data.get("location") or None),
        work_mode=(data.get("work_mode") or None),
        salary_min=data.get("salary_min") or None,
        salary_max=data.get("salary_max") or None,
        currency=(data.get("currency") or "USD"),
        seniority=(data.get("seniority") or None),
        description=(data.get("description") or "").strip(),
        url=url,
        fetched_at=datetime.utcnow(),
        raw={"host": host, "manual_add": True},
    )
