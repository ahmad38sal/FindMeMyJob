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
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from findmemyjob.llm import DEFAULT_MATCH_MODEL, DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import Job


# Extraction is a simple structured-output task — use the cheaper, more stable
# "match" model (lite) as the primary, and fall back to the heavier tailor model
# only if the lite one returns unparseable output. The lite model also tends to
# be far less likely to hit "model overloaded" (503) than the flagship.
_PRIMARY_EXTRACT_MODEL = DEFAULT_MATCH_MODEL
_FALLBACK_EXTRACT_MODEL = DEFAULT_TAILOR_MODEL


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


def _extract_json(raw: str) -> Dict[str, Any]:
    """Parse the model's reply into a dict.

    Gemini (especially thinking-enabled flagship models) sometimes emits a short
    reasoning preamble or trailing prose around the JSON. We first try a clean
    parse, then fall back to grabbing the outermost {...} block.
    """
    cleaned = _strip_code_fence(raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("No JSON object found in model output", cleaned, 0)


def _is_transient_llm_error(exc: Exception) -> bool:
    """True for model-overloaded / rate-limit style errors worth retrying."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    return (
        "503" in msg
        or "unavailable" in msg
        or "overloaded" in msg
        or "high demand" in msg
        or "429" in msg
        or "resource_exhausted" in msg
        or "rate limit" in msg
        or "servererror" in name
    )


def _llm_extract(text: str, *, url: Optional[str] = None) -> Dict[str, Any]:
    """Call the LLM to extract a job record, with retry/backoff and a model
    fallback. Raises RuntimeError with a user-friendly message on failure.

    `url` is optional context — present for the add-by-url path, absent when the
    user pastes raw posting text. Either way the prompt and parsing are shared.
    """
    if url:
        user_msg = f"URL: {url}\n\nPAGE TEXT:\n{text}\n\nReturn JSON now."
    else:
        user_msg = f"JOB POSTING TEXT:\n{text}\n\nReturn JSON now."
    models = [_PRIMARY_EXTRACT_MODEL]
    if _FALLBACK_EXTRACT_MODEL != _PRIMARY_EXTRACT_MODEL:
        models.append(_FALLBACK_EXTRACT_MODEL)
    last_exc: Optional[Exception] = None

    for model in models:
        for attempt in range(3):
            try:
                raw = llm.complete(
                    system=[{"type": "text", "text": _EXTRACT_INSTRUCTIONS}],
                    messages=[{"role": "user", "content": user_msg}],
                    model=model,
                    max_tokens=4096,
                    temperature=0.1,
                )
                return _extract_json(raw)
            except json.JSONDecodeError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_transient_llm_error(exc) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break

    if last_exc is not None and _is_transient_llm_error(last_exc):
        raise RuntimeError(
            "The AI model is busy right now (high demand). Please wait a moment "
            "and try adding this job again."
        ) from last_exc
    if isinstance(last_exc, json.JSONDecodeError):
        raise RuntimeError(
            "Couldn't read the job details from this page. Try pasting the job "
            "description manually instead."
        ) from last_exc
    raise RuntimeError(
        f"Couldn't extract this job via AI: {last_exc}"
    ) from last_exc


def build_job_from_text(text: str, *, url: Optional[str] = None) -> Job:
    """Extract a structured Job from raw posting text via the shared LLM extractor.

    Used by the "Paste a job" feature for postings that can't be fetched by URL
    (login-walled sites, PDFs, emails). `url`, if the user pasted one alongside,
    is used as the dedup key and stored on the Job. Raises ValueError for
    too-short input and RuntimeError (friendly message) on LLM failure.
    """
    text = (text or "").strip()
    if len(text) < 40:
        raise ValueError(
            "That looks too short to be a job posting. Paste the full job "
            "description (title, company, and details)."
        )

    data: Dict[str, Any] = _llm_extract(text[:30_000], url=url or None)

    clean_url = (url or "").strip()
    parsed = urlparse(clean_url) if clean_url else None
    host = parsed.netloc.lower() if parsed and parsed.scheme.startswith("http") else ""

    title = (data.get("title") or "Untitled").strip()
    company = (data.get("company") or host or "Unknown").strip()
    # Without a URL, dedup by a stable title+company key so re-pasting the same
    # posting updates rather than duplicates.
    source_id = clean_url or f"{title.lower()}|{company.lower()}"

    return Job(
        source="pasted",
        source_id=source_id,
        title=title,
        company=company,
        team=(data.get("team") or None),
        location=(data.get("location") or None),
        work_mode=(data.get("work_mode") or None),
        salary_min=data.get("salary_min") or None,
        salary_max=data.get("salary_max") or None,
        currency=(data.get("currency") or "USD"),
        seniority=(data.get("seniority") or None),
        description=(data.get("description") or text).strip(),
        url=clean_url or None,
        fetched_at=datetime.utcnow(),
        raw={"host": host, "manual_add": True, "pasted": True},
    )


def fetch_one_by_url(url: str) -> Job:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        raise ValueError(f"Not an HTTP URL: {url}")

    try:
        resp = httpx.get(
            url, timeout=30, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise RuntimeError(
                "This page requires login or blocks automated access. Paste the "
                "job description manually instead."
            ) from exc
        if code == 404:
            raise RuntimeError(
                "That job URL returned 404 (Not Found). The posting may have been "
                "removed, or the link is wrong."
            ) from exc
        raise RuntimeError(
            f"Couldn't load that URL (HTTP {code}). Check the link and try again."
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Couldn't reach that URL ({type(exc).__name__}). Check the link and "
            "your connection, then try again."
        ) from exc

    text = _clean_html(resp.text)
    if not text.strip():
        raise RuntimeError(
            "Couldn't extract any text from this URL — it may be a heavy SPA or "
            "require login. Paste the job description manually instead."
        )

    data: Dict[str, Any] = _llm_extract(text, url=url)

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
