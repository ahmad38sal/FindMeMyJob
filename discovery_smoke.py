"""Discovery-engine smoke test (SQLite, sources + LLM mocked).

Exercises end-to-end:
  - search-profile derivation (LLM path + heuristic fallback on empty profile)
  - sourcing (mocked), dedupe, upsert (ORM autoincrement), freshness partition
  - blended fit ranking (preference alignment is deterministic; skill score mocked)
  - run_discovery returns/records NEW top matches
  - routes: GET /jobs/top-picks, POST /jobs/discover, POST /jobs/api/discover

Run with:  OPENAI_API_KEY=dummy .venv/bin/python discovery_smoke.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

_tmpdir = tempfile.mkdtemp(prefix="fmj_disc_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import findmemyjob.discovery as discovery  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine  # noqa: E402
from findmemyjob.matching import ScoreResult  # noqa: E402
from findmemyjob.models import (  # noqa: E402
    DiscoveryRun, Job, Profile, SearchProfile,
)

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


# ---------------------------------------------------------------------------
# Seed a profile
# ---------------------------------------------------------------------------
with Session(engine) as s:
    p = Profile(
        id=1,
        contact={"name": "Grace Hopper", "email": "grace@example.com"},
        summary="Backend engineer, distributed systems.",
        work_history=[{"company": "Navy", "title": "Senior Backend Engineer",
                       "bullets": ["Built compilers"], "skills": ["python", "go"]}],
        skills=[{"name": "python", "evidence": "10y"}, {"name": "kubernetes"},
                {"name": "go"}, {"name": "distributed systems"}],
        preferences={"salary_min": 150000, "salary_target": 180000, "currency": "USD",
                     "work_modes": ["remote"], "seniority_levels": ["senior"],
                     "external_companies": ["greenhouse:acme", "ashby:beta"]},
    )
    s.add(p)
    s.commit()

# ---------------------------------------------------------------------------
# 1. Search-profile derivation — mock the LLM
# ---------------------------------------------------------------------------
print("\n[search-profile derivation]")
_derived_json = (
    '{"titles":["Senior Backend Engineer","Platform Engineer"],'
    '"keywords":["python","go","kubernetes","distributed systems"],'
    '"seniority":"senior","remote_pref":"remote","locations":[],'
    '"salary_min":150000,"salary_target":180000,"currency":"USD",'
    '"summary":"Remote senior backend role."}'
)
discovery.llm.complete_with_cached_profile = lambda **kw: _derived_json

with Session(engine) as s:
    sp = discovery.get_or_create_search_profile(s, regenerate=True)
    ok("python" in sp.keywords, "derived keywords include profile skill")
    ok(sp.salary_target == 180000, "user salary_target preferred over LLM")
    ok(sp.remote_pref == "remote", "remote pref derived")
    ok(sp.titles, "titles derived")

# Heuristic fallback on empty profile
empty_sp = discovery.derive_search_profile({})
ok(empty_sp["summary"].startswith("Heuristic"), "empty profile -> heuristic fallback")

# LLM failure -> heuristic
def _boom(**kw):
    raise RuntimeError("model busy")
discovery.llm.complete_with_cached_profile = _boom
fallback = discovery.derive_search_profile({"skills": [{"name": "rust"}]})
ok("rust" in fallback["keywords"], "LLM failure falls back to heuristic keywords")

# ---------------------------------------------------------------------------
# 2. Dedupe + freshness (pure functions)
# ---------------------------------------------------------------------------
print("\n[dedupe + freshness]")
dup = [
    Job(source="a", source_id="1", title="Eng", company="X", url="http://j/1"),
    Job(source="b", source_id="2", title="Eng", company="X", url="http://j/1"),  # dup URL
    Job(source="c", source_id="3", title="Eng", company="X", url=""),            # dup title+co
    Job(source="d", source_id="4", title="SRE", company="Y", url="http://j/4"),
]
deduped = discovery.dedupe(dup)
ok(len(deduped) == 2, f"dedupe collapses URL+title/co dups (got {len(deduped)})")

now = datetime.utcnow()
fresh_jobs = [
    Job(source="a", source_id="f", title="Fresh", company="X", posted_at=now - timedelta(days=2)),
    Job(source="a", source_id="s", title="Stale", company="X", posted_at=now - timedelta(days=40)),
    Job(source="a", source_id="u", title="Undated", company="X", posted_at=None),
]
fresh, stale = discovery.freshness_partition(fresh_jobs, max_age_days=14)
ok(len(fresh) == 2 and len(stale) == 1, "freshness: fresh+undated kept, stale dropped")
ok(any(j.title == "Undated" for j in fresh), "undated job kept (not dropped)")

# ---------------------------------------------------------------------------
# 3. Preference alignment + blend (deterministic)
# ---------------------------------------------------------------------------
print("\n[preference alignment + blend]")
sp_obj = SearchProfile(remote_pref="remote", salary_target=180000, salary_min=150000)
good = Job(source="a", source_id="g", title="x", company="y", work_mode="remote",
           salary_max=200000)
bad = Job(source="a", source_id="b", title="x", company="y", work_mode="onsite",
          salary_max=90000)
g_score, _ = discovery.preference_alignment(sp_obj, good)
b_score, _ = discovery.preference_alignment(sp_obj, bad)
ok(g_score > b_score, f"aligned job scores higher ({g_score:.2f} > {b_score:.2f})")
blended = discovery.blend(80.0, g_score, undated=False)
blended_undated = discovery.blend(80.0, g_score, undated=True)
ok(blended_undated < blended, "undated job gets small freshness penalty")

# ---------------------------------------------------------------------------
# 4. Full run_discovery — mock sources + skill scoring
# ---------------------------------------------------------------------------
print("\n[run_discovery end-to-end]")

def _fake_source_jobs(search_profile, prefs):
    jobs = [
        Job(source="remoteok", source_id="r1", title="Senior Backend Engineer",
            company="Acme", work_mode="remote", salary_max=190000,
            description="python go kubernetes", url="http://acme/r1",
            posted_at=now - timedelta(days=1)),
        Job(source="remoteok", source_id="r1b", title="Senior Backend Engineer",
            company="Acme", work_mode="remote", url="http://acme/r1",  # dup URL
            posted_at=now - timedelta(days=1)),
        Job(source="ashby", source_id="a1", title="Platform Engineer", company="Beta",
            work_mode="onsite", salary_max=120000, description="java",
            url="http://beta/a1", posted_at=None),  # undated
        Job(source="greenhouse", source_id="g_old", title="Old Role", company="Gamma",
            url="http://g/old", posted_at=now - timedelta(days=60)),  # stale
    ]
    return jobs, ["remoteok", "ashby", "greenhouse"]

discovery.source_jobs = _fake_source_jobs

async def _fake_bulk(profile_dict, jobs, concurrency=5):
    out = {}
    for j in jobs:
        score = 85.0 if "Backend" in j.title else 50.0
        out[j.id] = ScoreResult(score=score, reasoning=f"matched {j.title}",
                                 gaps=[] if score > 80 else ["java"],
                                 stretch_required=False, matched_skills=["python"])
    return out

discovery.score_jobs_bulk = _fake_bulk
# get_or_create_search_profile uses the stored one (already derived above)
discovery.llm.complete_with_cached_profile = lambda **kw: _derived_json

with Session(engine) as s:
    run = discovery.run_discovery(s, max_age_days=14, top_n=10)
    ok(run.error is None, f"run completed without error (error={run.error})")
    ok(run.fetched_count == 4, f"fetched all 4 raw (got {run.fetched_count})")
    ok(run.new_count == 3, f"3 new after dedupe (got {run.new_count})")
    ok(run.fresh_count == 2, f"2 fresh after freshness filter (got {run.fresh_count})")
    ok(len(run.top_matches) == 2, f"top_matches recorded (got {len(run.top_matches)})")
    if run.top_matches:
        top = run.top_matches[0]
        ok(top["title"] == "Senior Backend Engineer", "best match ranked first")
        ok("score" in top and "reasoning" in top and "url" in top,
           "top match has score/reasoning/url for cron")
    # Job rows got fit fields persisted
    backend = s.exec(select(Job).where(Job.title == "Senior Backend Engineer")).first()
    ok(backend.fit_score is not None, "fit_score persisted on Job row")
    undated_job = s.exec(select(Job).where(Job.title == "Platform Engineer")).first()
    ok(undated_job.undated is True, "undated job flagged on row")

    # Idempotency: a second run inserts no new jobs
    run2 = discovery.run_discovery(s, max_age_days=14, top_n=10)
    ok(run2.new_count == 0, f"second run idempotent, 0 new (got {run2.new_count})")

# ---------------------------------------------------------------------------
# 5. Routes
# ---------------------------------------------------------------------------
print("\n[routes]")
client = TestClient(app, raise_server_exceptions=True)
r = client.get("/jobs/top-picks")
ok(r.status_code == 200, f"GET /jobs/top-picks -> {r.status_code}")
ok("Top Picks" in r.text, "top-picks page renders heading")
ok("Senior Backend Engineer" in r.text, "top-picks lists ranked job")

r = client.post("/jobs/discover", data={}, follow_redirects=False)
ok(r.status_code == 303, f"POST /jobs/discover -> {r.status_code} (redirect)")

r = client.post("/jobs/api/discover")
ok(r.status_code == 200, f"POST /jobs/api/discover -> {r.status_code}")
body = r.json()
ok("top_matches" in body and "run_id" in body, "api/discover returns run summary JSON")
ok(isinstance(body["sources_used"], list), "api/discover reports sources_used")

r = client.post("/jobs/search-profile/regenerate", follow_redirects=False)
ok(r.status_code == 303, f"POST /jobs/search-profile/regenerate -> {r.status_code}")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL DISCOVERY SMOKE TESTS PASSED.")
