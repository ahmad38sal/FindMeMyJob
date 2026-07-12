"""Application Dashboard smoke test (SQLite).

Covers:
  - stats counters: total, applied-today, applied-this-week, applied-on-a-date
    (submitted_at as the applied timestamp, America/New_York), with a pending
    row (no submitted_at) correctly excluded from applied counts.
  - per-status counts.
  - response-rate / funnel math (response, interview, offer, rejection rates).
  - status update route sets last_status_change=now and, when moving INTO
    submitted, sets submitted_at (once) — HTMX returns the card fragment.
  - dashboard page renders: stats bar, charts (SVG + rates), date picker, board.
  - Application.created_at additive column exists + is backfilled non-null.

Run with:  OPENAI_API_KEY=dummy .venv/bin/python dashboard_smoke.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="fmj_dashboard_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine as db_engine  # noqa: E402
from findmemyjob.models import Application, ApplicationStatus, Job  # noqa: E402
from findmemyjob.routes import applications as appmod  # noqa: E402

init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


NY = appmod._NY


def ny_now():
    return datetime.now(NY)


# A stored (naive UTC) datetime whose NY calendar date equals *d*.
def utc_for_ny_date(d: date):
    # noon NY -> safely the same calendar day in NY; convert to naive UTC.
    dt_ny = datetime(d.year, d.month, d.day, 12, 0, tzinfo=NY)
    return dt_ny.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 1. Seed jobs + applications with known applied timestamps
# ---------------------------------------------------------------------------
print("\n[seed]")
today_ny = ny_now().date()
week_start = today_ny - timedelta(days=today_ny.weekday())
fixed_day = date(2026, 6, 15)  # a fixed past day for the on-a-date control

with Session(db_engine) as s:
    job = Job(source="pasted", source_id="dash|1", title="Backend Engineer", company="Acme")
    s.add(job)
    s.commit()
    s.refresh(job)
    jid = job.id

    seed = [
        # (status, submitted_at as ny date or None)
        (ApplicationStatus.submitted, today_ny),
        (ApplicationStatus.submitted, today_ny),
        (ApplicationStatus.responded, today_ny),
        (ApplicationStatus.offer, fixed_day),
        (ApplicationStatus.interview, fixed_day),
        (ApplicationStatus.rejected, today_ny - timedelta(days=20)),
        (ApplicationStatus.pending, None),  # never submitted
    ]
    for st, d in seed:
        sub = utc_for_ny_date(d) if d else None
        s.add(Application(job_id=jid, status=st, submitted_at=sub, match_score=70.0))
    s.commit()

ok(True, f"seeded 7 applications (today={today_ny}, fixed_day={fixed_day})")

# ---------------------------------------------------------------------------
# 2. Stats math via _compute_stats
# ---------------------------------------------------------------------------
print("\n[stats math]")
with Session(db_engine) as s:
    apps = list(s.exec(select(Application)).all())

stats_today = appmod._compute_stats(apps, today_ny)
ok(stats_today["total"] == 7, f"total == 7 (got {stats_today['total']})")
# submitted_at set on 6 of 7 (pending has none)
ok(stats_today["rates"]["submitted_total"] == 6,
   f"submitted_total == 6 (got {stats_today['rates']['submitted_total']})")
ok(stats_today["applied_today"] == 3, f"applied_today == 3 (got {stats_today['applied_today']})")
# this week includes all today rows (3); the 6-15 + 20-days-ago rows are older.
ok(stats_today["applied_this_week"] >= 3,
   f"applied_this_week >= 3 (got {stats_today['applied_this_week']})")
ok(stats_today["applied_this_week"] == 3,
   f"applied_this_week == 3 exactly (got {stats_today['applied_this_week']})")

stats_fixed = appmod._compute_stats(apps, fixed_day)
ok(stats_fixed["applied_on_date"] == 2,
   f"applied_on_date(2026-06-15) == 2 (got {stats_fixed['applied_on_date']})")
ok(stats_today["applied_on_date"] == 3,
   f"applied_on_date(today) == applied_today == 3 (got {stats_today['applied_on_date']})")

# Per-status counts
sc = stats_today["status_counts"]
ok(sc["submitted"] == 2 and sc["responded"] == 1 and sc["offer"] == 1
   and sc["interview"] == 1 and sc["rejected"] == 1 and sc["pending"] == 1,
   f"per-status counts correct (got {sc})")

# Response-rate math: denom = 6 submitted.
# responded=1, interview=1, offer=1, rejected=1
r = stats_today["rates"]
ok(r["response_rate"] == round(100 * (1 + 1 + 1) / 6),
   f"response_rate == {round(100*3/6)} (got {r['response_rate']})")
ok(r["interview_rate"] == round(100 * (1 + 1) / 6),
   f"interview_rate == {round(100*2/6)} (got {r['interview_rate']})")
ok(r["offer_rate"] == round(100 * 1 / 6), f"offer_rate == {round(100/6)} (got {r['offer_rate']})")
ok(r["rejection_rate"] == round(100 * 1 / 6),
   f"rejection_rate == {round(100/6)} (got {r['rejection_rate']})")

# 14-day series is present and sums include today's 3.
ok(len(stats_today["series"]) == 14, "trend series has 14 days")
ok(stats_today["series"][-1]["count"] == 3, "trend series last day (today) == 3")

# ---------------------------------------------------------------------------
# 3. created_at additive column backfilled
# ---------------------------------------------------------------------------
print("\n[created_at]")
with Session(db_engine) as s:
    apps = list(s.exec(select(Application)).all())
    ok(all(a.created_at is not None for a in apps), "every application has non-null created_at")

# ---------------------------------------------------------------------------
# 4. Routes — dashboard render + inline status update
# ---------------------------------------------------------------------------
print("\n[routes]")
client = TestClient(app, raise_server_exceptions=True)

r = client.get("/applications/")
ok(r.status_code == 200, f"GET /applications/ -> {r.status_code}")
ok("Total applications" in r.text, "dashboard shows Total applications counter")
ok("Applied today" in r.text and "Applied this week" in r.text, "dashboard shows applied counters")
ok("bar-chart" in r.text, "dashboard renders the trend SVG chart")
ok("Response rate" in r.text and "Rejection rate" in r.text, "dashboard renders response-rate funnel")
ok('type="date"' in r.text, "dashboard has the on-a-date picker")
ok("board-col" in r.text, "dashboard renders the kanban board")

# on-a-date query param
r = client.get("/applications/?on=2026-06-15")
ok(r.status_code == 200, f"GET /applications/?on=2026-06-15 -> {r.status_code}")

# Move a pending app INTO submitted -> submitted_at gets set, HTMX card returned.
with Session(db_engine) as s:
    pend = s.exec(select(Application).where(Application.status == ApplicationStatus.pending)).first()
    pend_id = pend.id
    ok(pend.submitted_at is None, "chosen pending app starts with submitted_at=None")

before = datetime.utcnow()
r = client.post(f"/applications/{pend_id}/status",
                data={"status": "submitted"}, headers={"hx-request": "true"})
ok(r.status_code == 200, f"POST status=submitted (HTMX) -> {r.status_code}")
ok("app-" in r.text, "status update returns the card fragment")

with Session(db_engine) as s:
    updated = s.get(Application, pend_id)
    ok(updated.status == ApplicationStatus.submitted, "status persisted as submitted")
    ok(updated.submitted_at is not None, "submitted_at set on move into submitted")
    ok(updated.last_status_change >= before, "last_status_change advanced to now")
    first_submit = updated.submitted_at

# Moving to another status and back must NOT overwrite the original submitted_at.
client.post(f"/applications/{pend_id}/status", data={"status": "interview"},
            headers={"hx-request": "true"})
client.post(f"/applications/{pend_id}/status", data={"status": "submitted"},
            headers={"hx-request": "true"})
with Session(db_engine) as s:
    again = s.get(Application, pend_id)
    ok(again.submitted_at == first_submit, "submitted_at preserved (set once) across status churn")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL DASHBOARD SMOKE TESTS PASSED.")
