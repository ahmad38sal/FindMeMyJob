"""Apple internal careers source — Playwright with persisted session.

`careers.apple.com` is the AppleConnect-gated employee careers portal. Auth
flow: open a real browser once, user signs in via IDMS, save the storage state
(cookies + localStorage) to disk. Subsequent fetches launch headless Chromium
with that storage state and scrape the search results.

The page is server-rendered HTML — each job card has class `job-list-item`
with `|`-delimited text:
    "{title} | {org} | {posted_date} | Location | {city} | Actions | ... | {job_id}"

CLI:
    uv run python -m findmemyjob.sources.apple_internal login
    uv run python -m findmemyjob.sources.apple_internal fetch [--no-detail]
    uv run python -m findmemyjob.sources.apple_internal detail <job_id> <team_code>
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from findmemyjob.config import settings
from findmemyjob.models import Job

CAREERS_URL = "https://careers.apple.com/en-us/search"
DETAIL_URL_BASE = "https://careers.apple.com"


def _session_path() -> Path:
    return settings.data_dir / "apple_session.json"


def _debug_dir() -> Path:
    d = settings.data_dir / "apple_debug"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Login (interactive)
# ---------------------------------------------------------------------------

def login_interactive(timeout_minutes: int = 5) -> Path:
    from playwright.sync_api import sync_playwright

    target = _session_path()
    print(f"Opening browser. Sign in via IDMS, then close the window once you reach the search page.")
    print(f"Session will be saved to {target}.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(CAREERS_URL)
        try:
            page.wait_for_url("**/careers.apple.com/**/search**", timeout=timeout_minutes * 60_000)
        except Exception:
            print("Didn't see the search URL within the timeout — saving whatever state we have.")
        target.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(target))
        browser.close()
    print(f"Saved session to {target}")
    return target


# ---------------------------------------------------------------------------
# Listing parser (pure — testable against saved HTML)
# ---------------------------------------------------------------------------

# Card text uses " | " as a separator. After splitting we expect the segments
# to start with: title, org, posted_date, "Location", city, ...
# The trailing segments include "Actions", "Add to Favorites <title> <id>",
# "Removed from favorites" — we discard those.
_DATE_PAT = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")
_JOB_ID_PAT = re.compile(r"\b(\d{9}-\d{4})\b")


def _parse_card_text(text: str) -> Dict[str, Optional[str]]:
    parts = [p.strip() for p in text.split("|") if p.strip()]
    out: Dict[str, Optional[str]] = {
        "title": None, "org": None, "posted_date": None, "location": None,
    }
    if parts:
        out["title"] = parts[0]
    if len(parts) >= 2:
        out["org"] = parts[1]
    for i, p in enumerate(parts):
        if _DATE_PAT.match(p):
            out["posted_date"] = p
        if p == "Location" and i + 1 < len(parts):
            out["location"] = parts[i + 1]
    return out


def parse_listing_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.job-list-item")
    seen: set = set()
    jobs: List[Dict[str, Any]] = []
    for card in cards:
        link = card.select_one("a[href*='/details/']")
        if link is None:
            continue
        href = link.get("href", "")
        # /en-us/details/<roleId>-<positionId>/<slug>?team=<TEAM>
        m = re.search(r"/details/(\d+-\d+)/", href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        team_code = parse_qs(urlparse(href).query).get("team", [None])[0]
        text = card.get_text(" | ", strip=True)
        parsed = _parse_card_text(text)
        jobs.append({
            "source_id": job_id,
            "title": parsed["title"] or link.get_text(strip=True) or "Untitled",
            "org": parsed["org"],
            "posted_date": parsed["posted_date"],
            "location": parsed["location"],
            "team_code": team_code,
            "url": href if href.startswith("http") else DETAIL_URL_BASE + href,
        })
    return jobs


# ---------------------------------------------------------------------------
# Detail page fetcher (full job description)
# ---------------------------------------------------------------------------

def _parse_detail_html(html: str) -> Dict[str, Optional[str]]:
    """Pull the full description body + structured fields from a detail page.

    Selectors are best-effort — refine based on real fetch output.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Apple careers detail pages typically have headed sections with these IDs/headings.
    sections: Dict[str, str] = {}
    for header in soup.find_all(["h2", "h3"]):
        title = header.get_text(strip=True).lower()
        body_parts: List[str] = []
        for sib in header.find_next_siblings():
            if sib.name in {"h2", "h3"}:
                break
            txt = sib.get_text(" ", strip=True)
            if txt:
                body_parts.append(txt)
        if body_parts:
            sections[title] = "\n\n".join(body_parts)

    # Concat the most useful sections, falling back to the whole main content.
    description_parts: List[str] = []
    for key in ("description", "summary", "key qualifications", "minimum qualifications",
                "preferred qualifications", "education & experience", "additional requirements"):
        for k, v in sections.items():
            if key in k:
                description_parts.append(f"## {k.title()}\n\n{v}")
    description = "\n\n".join(description_parts)
    if not description:
        main = soup.find("main") or soup.body
        description = main.get_text("\n", strip=True) if main else ""

    # Salary range — Apple posts ranges in the detail page footer.
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_match = re.search(r"\$([\d,]+)\s*(?:and|-|–|to)\s*\$([\d,]+)", description)
    if salary_match:
        salary_min = int(salary_match.group(1).replace(",", ""))
        salary_max = int(salary_match.group(2).replace(",", ""))

    return {
        "description": description.strip(),
        "salary_min": salary_min,
        "salary_max": salary_max,
    }


def fetch_detail(page, url: str) -> Dict[str, Optional[str]]:
    page.goto(url)
    page.wait_for_load_state("networkidle", timeout=30_000)
    html = page.content()
    return _parse_detail_html(html)


# ---------------------------------------------------------------------------
# Top-level fetch
# ---------------------------------------------------------------------------

def fetch_jobs(
    start_url: Optional[str] = None,
    query: str = "",
    max_pages: int = 5,
    limit: int = 1000,
    with_descriptions: bool = False,
    debug: bool = True,
) -> List[Job]:
    """Listing fetch with pagination via the Next button.

    `start_url` lets you pass a filtered search URL (copied from the browser
    after applying category/location filters). Falls back to `CAREERS_URL` +
    optional ?search query.

    Descriptions are NOT fetched by default (slow for many jobs). Call
    `hydrate_job_description` lazily — the matching path does this before
    scoring if the stored description is empty.
    """
    from playwright.sync_api import sync_playwright

    session = _session_path()
    if not session.exists():
        raise RuntimeError(
            f"No saved session at {session}. Run "
            "`uv run python -m findmemyjob.sources.apple_internal login` first."
        )

    if start_url is None:
        start_url = f"{CAREERS_URL}?search={query}" if query else CAREERS_URL

    all_cards: List[Dict[str, Any]] = []
    seen_ids: set = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(session))
        page = context.new_page()
        page.goto(start_url)
        page.wait_for_load_state("networkidle", timeout=30_000)

        if "idmsac.apple.com" in page.url or "/login/" in page.url:
            browser.close()
            raise RuntimeError(
                "Saved session expired (got redirected to IDMS). Re-run the login command."
            )

        for page_num in range(1, max_pages + 1):
            html = page.content()
            cards = parse_listing_html(html)
            new_count = 0
            for c in cards:
                if c["source_id"] in seen_ids:
                    continue
                seen_ids.add(c["source_id"])
                all_cards.append(c)
                new_count += 1
            print(f"  page {page_num}: {new_count} new (total {len(all_cards)})", file=sys.stderr)
            if len(all_cards) >= limit:
                break

            # Click Next Page if available + enabled
            next_btn = page.query_selector('button[aria-label="Next Page"]')
            if next_btn is None:
                break
            try:
                if next_btn.is_disabled():
                    break
            except Exception:
                pass
            try:
                next_btn.click()
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception as e:
                print(f"  pagination stopped: {e}", file=sys.stderr)
                break

        if debug:
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            (_debug_dir() / f"page-{ts}.html").write_text(html)

        details: Dict[str, Dict[str, Optional[str]]] = {}
        if with_descriptions:
            for c in all_cards[:limit]:
                try:
                    details[c["source_id"]] = fetch_detail(page, c["url"])
                except Exception as e:
                    print(f"  detail fetch failed for {c['source_id']}: {e}", file=sys.stderr)

        browser.close()

    now = datetime.utcnow()
    jobs: List[Job] = []
    for c in all_cards[:limit]:
        d = details.get(c["source_id"], {})
        jobs.append(Job(
            source="apple_internal",
            source_id=c["source_id"],
            title=c["title"],
            company="Apple",
            team=c.get("org") or c.get("team_code"),
            location=c.get("location"),
            description=d.get("description") or "",
            salary_min=d.get("salary_min"),
            salary_max=d.get("salary_max"),
            url=c["url"],
            fetched_at=now,
            raw={**c, **d},
        ))
    return jobs


def hydrate_job_description(job: Job) -> Job:
    """Fetch the detail page for one job and fill in description/salary in place.

    Used by the matching path when scoring a job that was stored from a
    listing-only fetch. Cheap to call repeatedly — opens one Playwright
    session per call, returns the same Job instance with fields populated.
    """
    from playwright.sync_api import sync_playwright

    session = _session_path()
    if not session.exists():
        raise RuntimeError("No saved session — run login first.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(session))
        page = context.new_page()
        d = fetch_detail(page, job.url)
        browser.close()

    job.description = d.get("description") or job.description
    if d.get("salary_min"):
        job.salary_min = d["salary_min"]
    if d.get("salary_max"):
        job.salary_max = d["salary_max"]
    job.raw = {**(job.raw or {}), **d}
    return job


def fetch_one_by_url(url: str) -> Job:
    """Fetch a single job by detail URL (paste a careers.apple.com link).

    Extracts the source_id from the URL, then scrapes title/team/location/
    description/salary from the detail page using the saved session.
    """
    from playwright.sync_api import sync_playwright

    m = re.search(r"/details/(\d+-\d+)/([a-z0-9-]+)", url)
    if not m:
        raise ValueError(
            f"Doesn't look like a careers.apple.com job detail URL: {url}"
        )
    source_id = m.group(1)
    slug = m.group(2)
    team_code = parse_qs(urlparse(url).query).get("team", [None])[0]

    session = _session_path()
    if not session.exists():
        raise RuntimeError("No saved session — run login first.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(session))
        page = context.new_page()
        page.goto(url)
        page.wait_for_load_state("networkidle", timeout=30_000)

        if "idmsac.apple.com" in page.url or "/login/" in page.url:
            browser.close()
            raise RuntimeError("Saved session expired — re-run login.")

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one("h1") or soup.select_one("h2")
        title = title_el.get_text(strip=True) if title_el else slug.replace("-", " ").title()

        # Team / location are usually shown near the title.
        meta_text = ""
        for sel in (".job-meta", ".jd-meta", "header", "main > div:first-child"):
            el = soup.select_one(sel)
            if el:
                meta_text = el.get_text(" | ", strip=True)
                if meta_text:
                    break

        location = None
        for line in meta_text.split("|"):
            line = line.strip()
            if line and any(c.isalpha() for c in line) and len(line) < 60 and not line.lower().startswith(("apple", "share", "weeklyhours", "submit")):
                # Best-effort location heuristic; refine after first run.
                if any(kw in line.lower() for kw in ("cupertino", "austin", "seattle", "remote", "ca", "new york", "tx")):
                    location = line
                    break

        d = _parse_detail_html(html)
        browser.close()

    return Job(
        source="apple_internal",
        source_id=source_id,
        title=title,
        company="Apple",
        team=team_code,
        location=location,
        description=d.get("description") or "",
        salary_min=d.get("salary_min"),
        salary_max=d.get("salary_max"),
        url=url,
        fetched_at=datetime.utcnow(),
        raw={"manual_add": True, "team_code": team_code, **d},
    )


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------

class AppleInternalSource:
    name = "apple_internal"

    def __init__(
        self,
        queries: Optional[List[str]] = None,
        start_url: Optional[str] = None,
        max_pages: int = 5,
    ) -> None:
        self.queries = queries or []
        self.start_url = start_url
        self.max_pages = max_pages

    def fetch(self, *, query: str = "", limit: int = 1000) -> List[Job]:
        """Run the configured queries (or fall back to start_url / single query) and dedup."""
        try:
            if self.queries:
                seen: set = set()
                results: List[Job] = []
                for q in self.queries:
                    print(f"[apple_internal] query: {q!r}", file=sys.stderr)
                    try:
                        per_q = fetch_jobs(
                            query=q,
                            max_pages=self.max_pages,
                            limit=limit,
                            with_descriptions=False,
                            debug=True,
                        )
                    except Exception as e:
                        # Per-query failure (blocked domain, network, parser) shouldn't
                        # take out the rest of the queries.
                        print(f"[apple_internal] query {q!r} failed: {e}", file=sys.stderr)
                        continue
                    for j in per_q:
                        if j.source_id in seen:
                            continue
                        seen.add(j.source_id)
                        results.append(j)
                return results
            return fetch_jobs(
                start_url=self.start_url,
                query=query,
                max_pages=self.max_pages,
                limit=limit,
                with_descriptions=False,
                debug=True,
            )
        except Exception as e:
            print(f"[apple_internal] {e}", file=sys.stderr)
            return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "fetch"
    if cmd == "login":
        login_interactive()
    elif cmd == "fetch":
        max_pages = 5
        start_url: Optional[str] = None
        with_desc = "--with-detail" in args
        for a in args[1:]:
            if a.startswith("--max-pages="):
                max_pages = int(a.split("=", 1)[1])
            elif a.startswith("--url="):
                start_url = a.split("=", 1)[1]
        jobs = fetch_jobs(start_url=start_url, max_pages=max_pages, with_descriptions=with_desc)
        print(f"Fetched {len(jobs)} jobs (pages={max_pages}, descriptions={with_desc})")
        for j in jobs[:25]:
            sal = f" [${j.salary_min}–${j.salary_max}]" if j.salary_min else ""
            print(f"  - {j.title}  ({j.team or '-'}, {j.location or '-'}){sal}")
    elif cmd == "parse-saved":
        # Re-parse the most recent saved listing HTML — useful for iterating
        # on the parser without re-fetching.
        latest = max(_debug_dir().glob("page-*.html"))
        cards = parse_listing_html(latest.read_text())
        print(f"Parsed {len(cards)} from {latest.name}")
        for c in cards:
            print(f"  - {c['title']}  ({c.get('org') or c.get('team_code')}, {c.get('location')})")
    else:
        sys.exit(f"Unknown command: {cmd}. Use 'login', 'fetch', or 'parse-saved'.")
