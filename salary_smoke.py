"""Salary meter + fair-ask smoke test (SQLite, LLM mocked).

Exercises:
  - build_salary_view: meter axis + percentages for a WIDE posted range,
    for a no-salary job, and for odd/missing data (never raises).
  - heuristic_fair_ask: deterministic fallback works with a posted band, with
    only an estimate, with neither (profile target / experience guess).
  - compute_fair_ask: falls back to the heuristic when the LLM errors, and
    uses the LLM JSON when it returns valid data.
  - routes: job detail renders the meter + fair-ask for BOTH a wide-posted-range
    job and a no-salary job; the fair-ask is cached on job.raw; the /fair-ask
    refresh endpoint works; no 500s anywhere; existing estimate flow intact.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python salary_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_salary_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

import findmemyjob.salary as salary  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.models import Job, Profile  # noqa: E402

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


PROFILE = {
    "summary": "Senior backend engineer.",
    "work_history": [
        {"company": "Acme", "title": "Senior Engineer", "start": "2016-01-01",
         "end": None, "bullets": ["Built distributed systems"], "skills": ["python", "go"]},
    ],
    "skills": [{"name": "python"}, {"name": "go"}],
    "education": [],
    "preferences": {"salary_target": 180000, "currency": "USD",
                    "locations": ["Remote"], "work_modes": ["remote"]},
}

# ---------------------------------------------------------------------------
# 1. build_salary_view (pure, no LLM)
# ---------------------------------------------------------------------------
print("\n[build_salary_view]")

# Wide posted range, no estimate, with a fair-ask dict.
wide = Job(source="t", source_id="t|1", title="Staff Eng", company="WideCo",
           salary_min=60000, salary_max=120000, currency="USD",
           description="Backend.")
fa = {"ask_low": 95000, "ask_target": 105000, "ask_high": 115000,
      "currency": "USD", "rationale": "Upper-middle of the band.",
      "position": "upper_band", "source": "heuristic"}
v = salary.build_salary_view(wide, estimate=None, fair_ask=fa)
ok(v.has_posted and not v.has_estimate, "wide: has_posted True, has_estimate False")
ok(v.scale_hi > v.scale_lo, "wide: meter axis has positive width")
ok(v.posted_lo_pct is not None and v.posted_hi_pct is not None,
   "wide: posted band positioned on meter")
ok(0 <= v.posted_lo_pct < v.posted_hi_pct <= 100,
   "wide: posted band lo<hi within [0,100]")
ok(v.ask_target_pct is not None, "wide: fair-ask marker positioned")
ok(v.scale_lo < 60000 and v.scale_hi > 120000, "wide: axis padded beyond band")

# No salary, with an estimate.
nosal = Job(source="t", source_id="t|2", title="Backend Eng", company="NoSalCo",
            currency="USD", description="Backend.")
est = {"market_min": 120000, "market_median": 160000, "market_max": 200000,
       "user_target": 175000, "user_target_source": "profile", "currency": "USD",
       "rationale": "x"}
v2 = salary.build_salary_view(nosal, estimate=est, fair_ask=None)
ok(not v2.has_posted and v2.has_estimate, "no-salary: has_posted False, has_estimate True")
ok(v2.median_pct is not None, "no-salary: market median positioned")
ok(v2.posted_lo_pct is None, "no-salary: no posted band")
ok(v2.scale_hi > v2.scale_lo, "no-salary: meter axis still valid")

# Degenerate / empty data must NOT raise and must not crash the meter.
empty = Job(source="t", source_id="t|3", title="Mystery", company="X", currency="USD")
v3 = salary.build_salary_view(empty, estimate=None, fair_ask=None)
ok(v3.scale_lo == 0 and v3.scale_hi == 0, "empty: zero axis (meter suppressed in template)")
ok(not v3.has_posted and not v3.has_estimate, "empty: no posted, no estimate")

# Single endpoint only.
single = Job(source="t", source_id="t|4", title="X", company="Y", salary_min=100000,
             currency="USD")
v4 = salary.build_salary_view(single, None, None)
ok(v4.has_posted and v4.posted_min == 100000 and v4.posted_max == 100000,
   "single endpoint: band collapses to one value")

# ---------------------------------------------------------------------------
# 2. heuristic_fair_ask (deterministic fallback)
# ---------------------------------------------------------------------------
print("\n[heuristic_fair_ask]")

h = salary.heuristic_fair_ask(PROFILE, wide, estimate=None)
ok(h.source == "heuristic", "heuristic: source tagged")
ok(60000 <= h.ask_low <= h.ask_target <= h.ask_high <= 120000,
   f"heuristic: ask within band ({h.ask_low}/{h.ask_target}/{h.ask_high})")
ok(h.ask_target > (60000 + 120000) / 2,
   "heuristic: target sits upper-middle of band, not the midpoint floor")
ok(h.position in ("mid_band", "upper_band", "low_in_band"), "heuristic: band position set")

h2 = salary.heuristic_fair_ask(PROFILE, nosal, estimate=est)
ok(h2.ask_target == 175000, "heuristic: no band -> anchors on estimate user_target")
ok(h2.position == "no_band", "heuristic: no band -> position no_band")

bare_profile = {"work_history": [], "preferences": {}}
h3 = salary.heuristic_fair_ask(bare_profile, empty, estimate=None)
ok(h3.ask_low <= h3.ask_target <= h3.ask_high and h3.ask_target > 0,
   "heuristic: no signal at all -> still produces a sane ordered ask")

h4 = salary.heuristic_fair_ask(PROFILE, empty, estimate=None)
ok(h4.ask_target == 180000, "heuristic: no band/estimate -> uses profile salary_target")

# ---------------------------------------------------------------------------
# 3. compute_fair_ask: LLM error -> heuristic; valid LLM -> used
# ---------------------------------------------------------------------------
print("\n[compute_fair_ask]")


def _boom(**kwargs):
    raise RuntimeError("LLM down")


_orig = salary.llm.complete_with_cached_profile
salary.llm.complete_with_cached_profile = _boom
ca = salary.compute_fair_ask(PROFILE, wide, estimate=None)
ok(ca.source == "heuristic", "compute: LLM error degrades to heuristic (no raise)")
ok(60000 <= ca.ask_target <= 120000, "compute: heuristic ask still within band")


def _good(**kwargs):
    return ('{"ask_low":150000,"ask_target":172000,"ask_high":190000,'
            '"currency":"USD","rationale":"Strong senior profile, remote.",'
            '"position":"upper_band"}')


salary.llm.complete_with_cached_profile = _good
cb = salary.compute_fair_ask(PROFILE, wide, estimate=None)
ok(cb.source == "llm" and cb.ask_target == 172000, "compute: valid LLM JSON used")
ok(cb.position == "upper_band", "compute: LLM position parsed")


def _backwards(**kwargs):  # out-of-order bounds -> must fall back
    return '{"ask_low":200000,"ask_target":100000,"ask_high":50000,"currency":"USD"}'


salary.llm.complete_with_cached_profile = _backwards
cc = salary.compute_fair_ask(PROFILE, wide, estimate=None)
ok(cc.source == "heuristic", "compute: nonsensical LLM bounds -> heuristic fallback")

salary.llm.complete_with_cached_profile = _orig

# ---------------------------------------------------------------------------
# 4. Routes (LLM mocked to the heuristic path for determinism)
# ---------------------------------------------------------------------------
print("\n[routes]")

# Force all fair-ask LLM calls through the heuristic so route tests are stable.
salary.llm.complete_with_cached_profile = _boom

with Session(engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact={"name": "Grace Hopper"},
                      summary=PROFILE["summary"], work_history=PROFILE["work_history"],
                      skills=PROFILE["skills"], education=[],
                      preferences=PROFILE["preferences"]))
    jw = Job(source="pasted", source_id="sal|wide", title="Staff Engineer",
             company="WideCo", salary_min=60000, salary_max=120000, currency="USD",
             description="Own backend systems.")
    jn = Job(source="pasted", source_id="sal|none", title="Backend Engineer",
             company="NoSalCo", currency="USD", description="Own backend systems.")
    s.add(jw)
    s.add(jn)
    s.commit()
    s.refresh(jw)
    s.refresh(jn)
    wide_id, none_id = jw.id, jn.id

client = TestClient(app, raise_server_exceptions=True)

# Wide posted-range job: meter + fair-ask must render, no 500.
r = client.get(f"/jobs/{wide_id}")
ok(r.status_code == 200, f"GET wide job -> {r.status_code}")
ok("salary-meter" in r.text, "wide job: meter rendered for a POSTED range")
ok("meter-band" in r.text, "wide job: posted band drawn on meter")
ok("Your fair ask" in r.text, "wide job: fair-ask recommendation shown")
ok("Posted range" in r.text, "wide job: legend distinguishes posted range")

# No-salary job: meter still renders (now driven by fair-ask axis), no 500.
r = client.get(f"/jobs/{none_id}")
ok(r.status_code == 200, f"GET no-salary job -> {r.status_code}")
ok("Your fair ask" in r.text, "no-salary job: fair-ask still shown")
ok("Estimate market range" in r.text, "no-salary job: estimate CTA still offered")

# Fair-ask was cached on job.raw after first view.
with Session(engine) as s:
    jw2 = s.get(Job, wide_id)
    ok((jw2.raw or {}).get("fair_ask") is not None, "fair-ask cached on job.raw")
    ok((jw2.raw or {}).get("fair_ask", {}).get("source") == "heuristic",
       "cached fair-ask used heuristic (LLM mocked off)")

# Refresh endpoint (HTMX) returns the panel partial, no 500.
r = client.post(f"/jobs/{wide_id}/fair-ask", headers={"hx-request": "true"})
ok(r.status_code == 200, f"POST /fair-ask (htmx) -> {r.status_code}")
ok("salary-panel" in r.text, "refresh returns the salary panel partial")

# Non-HTMX refresh redirects.
r = client.post(f"/jobs/{wide_id}/fair-ask", follow_redirects=False)
ok(r.status_code == 303, f"POST /fair-ask (no htmx) -> {r.status_code}")

# Estimate flow still works for the no-salary job and clears/refreshes fair-ask.
def _est(**kwargs):
    return ('{"market_min":120000,"market_median":160000,"market_max":200000,'
            '"user_target":175000,"user_target_source":"profile","currency":"USD",'
            '"rationale":"market"}')

salary.llm.complete_with_cached_profile = _est
r = client.post(f"/jobs/{none_id}/estimate-salary", headers={"hx-request": "true"})
ok(r.status_code == 200, f"POST /estimate-salary -> {r.status_code}")
ok("salary-meter" in r.text, "after estimate: meter renders for no-salary job")
ok("median" in r.text, "after estimate: market median shown on meter")
salary.llm.complete_with_cached_profile = _boom

# After estimate, re-view the page — still no 500 and fair-ask present.
r = client.get(f"/jobs/{none_id}")
ok(r.status_code == 200, f"GET no-salary job post-estimate -> {r.status_code}")
ok("Your fair ask" in r.text, "post-estimate: fair-ask still shown")

salary.llm.complete_with_cached_profile = _orig

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL SALARY SMOKE TESTS PASSED.")
