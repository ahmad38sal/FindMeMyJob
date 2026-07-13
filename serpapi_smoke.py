"""SerpApi Google Jobs source smoke test (NO network, NO real key).

Monkeypatches ``httpx.get`` with a canned two-page SerpApi response and asserts:
  - fetch() returns [] cleanly when SERPAPI_API_KEY is unset (no exception).
  - normalization: source/source_id, url prefers apply_options[0].link over
    share_link, work_mode inference, raw preserved, seniority from title.
  - posted_at relative parsing ("22 hours ago" -> ~now, "25 days ago" -> ~25d old).
  - stable-hash source_id fallback when job_id is missing.
  - pagination follows next_page_token and stops when it's absent.
  - the relative-time parser directly on several inputs.

Run with:  .venv/bin/python serpapi_smoke.py
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.pop("SERPAPI_API_KEY", None)  # start unconfigured
sys.path.insert(0, "src")

import findmemyjob.sources.serpapi as gj  # noqa: E402
from findmemyjob.sources.serpapi import (  # noqa: E402
    GoogleJobsSource,
    is_configured,
    parse_relative_posted_at,
)

failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


# ---------------------------------------------------------------------------
# Canned SerpApi payload (2 pages).
# ---------------------------------------------------------------------------
PAGE_1 = {
    "search_metadata": {"status": "Success"},
    "jobs_results": [
        {
            "title": "Senior Backend Engineer",
            "company_name": "Acme Corp",
            "location": "Connecticut, United States",
            "via": "via LinkedIn",
            "share_link": "https://www.google.com/search?q=acme+backend",
            "description": "Build distributed systems in Python.",
            "job_id": "acme-backend-123",
            "detected_extensions": {"posted_at": "22 hours ago",
                                    "schedule_type": "Full-time",
                                    "work_from_home": True},
            "extensions": ["22 hours ago", "Full-time", "Work from home"],
            "job_highlights": [
                {"title": "Qualifications", "items": ["5+ years Python", "Kafka"]},
                {"title": "Responsibilities", "items": ["Own the pipeline"]},
            ],
            "apply_options": [
                {"title": "Apply on ZipRecruiter", "link": "https://ziprecruiter.com/apply/acme"},
                {"title": "Apply on LinkedIn", "link": "https://linkedin.com/jobs/acme"},
            ],
        },
        {
            "title": "Staff Data Engineer",
            "company_name": "Globex",
            "location": "Boston, MA",
            "via": "via Indeed",
            "share_link": "https://www.google.com/search?q=globex+data",
            "description": "Own the data platform.",
            "job_id": "globex-data-456",
            "detected_extensions": {"posted_at": "25 days ago", "schedule_type": "Full-time"},
            "extensions": ["25 days ago", "Full-time"],
            "apply_options": [{"title": "Apply on Workday", "link": "https://globex.wd1.myworkdayjobs.com/x"}],
        },
    ],
    "serpapi_pagination": {"next_page_token": "TOKEN_PAGE_2"},
}

PAGE_2 = {
    "search_metadata": {"status": "Success"},
    "jobs_results": [
        {
            # NO job_id -> stable-hash fallback path.
            "title": "Backend Engineer",
            "company_name": "Initech",
            "location": "Remote",
            "via": "via ZipRecruiter",
            "share_link": "https://www.google.com/search?q=initech",
            "description": "Remote backend role.",
            "detected_extensions": {"posted_at": "3 days ago"},
            "extensions": ["3 days ago", "Remote"],
            # No apply_options -> url falls back to share_link.
        },
    ],
    # No serpapi_pagination -> pagination must stop here.
}


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


_requests = {"count": 0, "params": []}


def _fake_get(url, params=None, timeout=None, headers=None):
    _requests["count"] += 1
    _requests["params"].append(dict(params or {}))
    if (params or {}).get("next_page_token") == "TOKEN_PAGE_2":
        return _FakeResp(PAGE_2)
    return _FakeResp(PAGE_1)


gj.httpx.get = _fake_get

# ---------------------------------------------------------------------------
# 1. Unconfigured: fetch() returns [] with no exception, no HTTP call.
# ---------------------------------------------------------------------------
print("\n[unconfigured key -> [] no crash]")
ok(is_configured() is False, "is_configured() False when SERPAPI_API_KEY unset")
src = GoogleJobsSource(location="Connecticut, United States")
result = src.fetch(query="backend engineer", limit=50)
ok(result == [], "fetch() returns [] when key unset")
ok(_requests["count"] == 0, "no HTTP request made when unconfigured")

# ---------------------------------------------------------------------------
# 2. Relative-time parser direct checks.
# ---------------------------------------------------------------------------
print("\n[relative posted_at parser]")
now = datetime(2026, 7, 13, 12, 0, 0)
p22h = parse_relative_posted_at("22 hours ago", now=now)
ok(p22h == now - timedelta(hours=22), "'22 hours ago' -> now-22h")
p25d = parse_relative_posted_at("25 days ago", now=now)
ok(p25d == now - timedelta(days=25), "'25 days ago' -> now-25d")
ok(parse_relative_posted_at("30+ days ago", now=now) == now - timedelta(days=30),
   "'30+ days ago' handles the '+' -> now-30d")
ok(parse_relative_posted_at("yesterday", now=now) == now - timedelta(days=1), "'yesterday' -> now-1d")
ok(parse_relative_posted_at("today", now=now) == now, "'today' -> now")
ok(parse_relative_posted_at("a day ago", now=now) == now - timedelta(days=1), "'a day ago' -> now-1d")
ok(parse_relative_posted_at("2 weeks ago", now=now) == now - timedelta(weeks=2), "'2 weeks ago' -> now-14d")
ok(parse_relative_posted_at("garbage") is None, "unparseable -> None")
ok(parse_relative_posted_at(None) is None, "None -> None")

# ---------------------------------------------------------------------------
# 3. Configured: fetch parses + paginates.
# ---------------------------------------------------------------------------
print("\n[configured -> parse + paginate]")
os.environ["SERPAPI_API_KEY"] = "test-key-not-real"
ok(is_configured() is True, "is_configured() True once key is set")

jobs = src.fetch(query="backend engineer", limit=50)
ok(_requests["count"] == 2, f"followed pagination across 2 pages (got {_requests['count']} requests)")
ok(len(jobs) == 3, f"parsed all 3 jobs across both pages (got {len(jobs)})")
ok(_requests["params"][1].get("next_page_token") == "TOKEN_PAGE_2",
   "second request carried next_page_token")
ok("api_key" in _requests["params"][0] and _requests["params"][0]["engine"] == "google_jobs",
   "request used engine=google_jobs with api_key")
ok(_requests["params"][0].get("location") == "Connecticut, United States",
   "location param passed through")

acme = jobs[0]
ok(acme.source == "google_jobs", "source == google_jobs")
ok(acme.source_id == "acme-backend-123", "source_id uses job_id when present")
ok(acme.url == "https://ziprecruiter.com/apply/acme",
   f"url prefers apply_options[0].link (got {acme.url})")
ok(acme.work_mode == "remote", "work_from_home True -> work_mode remote")
ok(acme.seniority == "senior", "seniority inferred from 'Senior' title")
ok("Qualifications" in acme.description and "5+ years Python" in acme.description,
   "job_highlights folded into description for tailoring signal")
ok(acme.raw.get("schedule_type") == "Full-time", "schedule_type stashed in raw")
ok(acme.raw.get("job_id") == "acme-backend-123", "full item preserved in raw")
ok(acme.posted_at is not None and (datetime.utcnow() - acme.posted_at) < timedelta(days=1),
   "posted_at '22 hours ago' is recent (< 1 day old)")

globex = jobs[1]
ok(globex.url == "https://globex.wd1.myworkdayjobs.com/x", "Workday apply link preferred")
age_days = (datetime.utcnow() - globex.posted_at).days
ok(24 <= age_days <= 26, f"posted_at '25 days ago' ~25 days old (got {age_days}d)")
ok(globex.seniority == "staff", "seniority 'staff' inferred")

initech = jobs[2]
ok(initech.source_id.startswith("gj_"), "missing job_id -> stable-hash fallback source_id")
ok(initech.url == "https://www.google.com/search?q=initech",
   "no apply_options -> url falls back to share_link")
ok(initech.work_mode == "remote", "location 'Remote' -> work_mode remote")

# Stable-hash id is deterministic for the same item.
from findmemyjob.sources.serpapi import _stable_source_id  # noqa: E402
again = _stable_source_id(PAGE_2["jobs_results"][0])
ok(again == initech.source_id, "stable-hash source_id is deterministic (dedup-safe)")

# ---------------------------------------------------------------------------
# 4. API-error payload -> no crash, returns what was parsed so far.
# ---------------------------------------------------------------------------
print("\n[API error payload handled]")


def _err_get(url, params=None, timeout=None, headers=None):
    return _FakeResp({"search_metadata": {"status": "Error"},
                      "error": "Invalid API key."})


gj.httpx.get = _err_get
err_jobs = src.fetch(query="x", limit=10)
ok(err_jobs == [], "API error payload -> [] (no exception)")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL SERPAPI SMOKE TESTS PASSED.")
