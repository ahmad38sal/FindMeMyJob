"""ATS autofill smoke test (SQLite, no LLM).

Covers:
  - ats.py pure normalizers: normalize_date (ISO, US, month-name, year-only,
    Present/Current, messy), normalize_phone, normalize_company, normalize_title,
    normalize_location, split_name.
  - build_application_data assembles a normalized structure from a Profile
    (+ optional tailored Resume overlay): contact w/ split name + normalized
    phone + location parts; work_history w/ MMM YYYY display + numeric
    month/year + current bool; education w/ same clean formats + GPA.
  - GET /api/ext/application-data/{job_id} returns that structure with the ext
    bearer-token auth (401 without token, 200 with).

Run with:  OPENAI_API_KEY=dummy .venv/bin/python ats_smoke.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="fmj_ats_")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ["DATA_DIR"] = _tmpdir
os.environ.pop("DATABASE_URL", None)  # force SQLite
os.environ["FINDMEMYJOB_EXT_TOKEN"] = "test-token"  # enable /api/ext

sys.path.insert(0, "src")
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("findmemyjob"):
        del sys.modules[mod_name]

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from findmemyjob import ats  # noqa: E402
from findmemyjob.config import settings  # noqa: E402
from findmemyjob.main import app  # noqa: E402
from findmemyjob.db import init_db, engine as db_engine  # noqa: E402
from findmemyjob.models import Job, Profile  # noqa: E402

settings.ext_token = "test-token"  # in case env was read before this import
init_db()
failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


# ---------------------------------------------------------------------------
# 1. normalize_date
# ---------------------------------------------------------------------------
print("\n[normalize_date]")
from datetime import date  # noqa: E402

cases_month_year = [
    ("2023-01", 1, 2023),
    ("2023-01-15", 1, 2023),
    ("2023/02", 2, 2023),
    ("01/2023", 1, 2023),
    ("1/2023", 1, 2023),
    ("01-2023", 1, 2023),
    ("January 2023", 1, 2023),
    ("Jan 2023", 1, 2023),
    ("Jan. 2023", 1, 2023),
    ("Sept 2023", 9, 2023),
    ("March 2020", 3, 2020),
    ("03/15/2023", 3, 2023),
    (date(2022, 7, 1), 7, 2022),
]
for raw, mo, yr in cases_month_year:
    d = ats.normalize_date(raw)
    ok(d["month"] == mo and d["year"] == yr and not d["current"],
       f"normalize_date({raw!r}) -> month={mo} year={yr}")

jan = ats.normalize_date("2023-01")
ok(jan["display"] == "Jan 2023", "normalize_date display is 'Jan 2023'")

for word in ("Present", "current", "NOW", "Ongoing"):
    d = ats.normalize_date(word)
    ok(d["current"] is True and d["month"] is None and d["year"] is None,
       f"normalize_date({word!r}) -> current=True")

yonly = ats.normalize_date("2023")
ok(yonly["year"] == 2023 and yonly["month"] is None and yonly["display"] == "2023",
   "normalize_date('2023') -> year-only")

# (a) Range strings in a single field: take the FIRST part, preserving month.
range_first_cases = [
    ("May 2020 - Present", 5, 2020),
    ("Jan 2021 – Mar 2023", 1, 2021),   # en dash
    ("Feb 2019 — Dec 2020", 2, 2019),   # em dash
    ("June 2018 to August 2019", 6, 2018),
    ("Apr 2017 | Jul 2018", 4, 2017),
]
for raw, mo, yr in range_first_cases:
    d = ats.normalize_date(raw)
    ok(d["month"] == mo and d["year"] == yr and not d["current"],
       f"normalize_date({raw!r}) -> first part month={mo} year={yr}")
# Bare-hyphen year range still splits to first year; ISO stays intact.
ok(ats.normalize_date("2020-2022")["year"] == 2020 and ats.normalize_date("2020-2022")["month"] is None,
   "normalize_date('2020-2022') -> first year 2020")
iso = ats.normalize_date("2023-01")
ok(iso["month"] == 1 and iso["year"] == 2023, "normalize_date('2023-01') stays ISO, not a range")

# (b)/(c) normalize_date_range splits both ends.
r1 = ats.normalize_date_range("May 2020 - Present")
ok(r1["start"]["month"] == 5 and r1["start"]["year"] == 2020 and not r1["start"]["current"],
   "normalize_date_range('May 2020 - Present') start=May 2020")
ok(r1["end"]["current"] is True, "normalize_date_range('May 2020 - Present') end current")
r2 = ats.normalize_date_range("Jan 2021 - Mar 2023")
ok(r2["start"]["month"] == 1 and r2["start"]["year"] == 2021, "normalize_date_range start Jan 2021")
ok(r2["end"]["month"] == 3 and r2["end"]["year"] == 2023 and not r2["end"]["current"],
   "normalize_date_range end Mar 2023")

for junk in ("", None, "n/a", "garbage"):
    d = ats.normalize_date(junk)
    ok(d["month"] is None and d["year"] is None and not d["current"],
       f"normalize_date({junk!r}) -> empty")

# ---------------------------------------------------------------------------
# 2. normalize_phone
# ---------------------------------------------------------------------------
print("\n[normalize_phone]")
ok(ats.normalize_phone("(555) 123-4567") == "+15551234567", "10-digit US -> +1XXXXXXXXXX")
ok(ats.normalize_phone("555.123.4567") == "+15551234567", "dotted 10-digit -> +1")
ok(ats.normalize_phone("1-555-123-4567") == "+15551234567", "1+10-digit -> +1")
ok(ats.normalize_phone("+44 20 7946 0958") == "+442079460958", "intl keeps country code")
ok(ats.normalize_phone("") == "" and ats.normalize_phone(None) == "", "empty phone -> ''")
ok(ats.normalize_phone("abc") == "", "no-digit phone -> ''")

# ---------------------------------------------------------------------------
# 3. company / title / location / name
# ---------------------------------------------------------------------------
print("\n[company/title/location/name]")
ok(ats.normalize_company("  Acme   Corp,  ") == "Acme Corp", "company collapses ws + trailing comma")
ok(ats.normalize_company('"Globex Inc."') == "Globex Inc.", "company strips quotes, keeps suffix")

ok(ats.normalize_title("Senior Engineer (Remote)") == "Senior Engineer", "title drops (Remote)")
ok(ats.normalize_title("Backend Dev | Acme") == "Backend Dev", "title drops | site")
ok(ats.normalize_title("Data Scientist - Acme Corp") == "Data Scientist", "title drops - Company")
ok(ats.normalize_title("iOS Engineer") == "iOS Engineer", "title preserves casing (iOS)")

loc = ats.normalize_location("San Francisco, CA, USA")
ok(loc["city"] == "San Francisco" and loc["region"] == "CA" and loc["country"] == "USA",
   "location splits city/region/country")
ok(ats.normalize_location("Remote")["city"] == "Remote", "single-token location -> city")
ok(ats.normalize_location("")["display"] == "", "empty location -> empty parts")

n = ats.split_name("Ada Lovelace")
ok(n["first_name"] == "Ada" and n["last_name"] == "Lovelace", "split_name two tokens")
n3 = ats.split_name("Jean Luc Picard")
ok(n3["first_name"] == "Jean" and n3["last_name"] == "Luc Picard", "split_name folds middle into last")
n1 = ats.split_name("Cher")
ok(n1["first_name"] == "Cher" and n1["last_name"] == "", "split_name single token")

# normalize_url: real URLs pass through, label-only values drop to "".
ok(ats.normalize_url("https://linkedin.com/in/ada") == "https://linkedin.com/in/ada",
   "normalize_url keeps full https URL")
ok(ats.normalize_url("linkedin.com/in/ada") == "linkedin.com/in/ada", "normalize_url keeps bare domain")
ok(ats.normalize_url("www.github.com/ada") == "www.github.com/ada", "normalize_url keeps www")
ok(ats.normalize_url("LinkedIn") == "", "normalize_url drops label-only 'LinkedIn'")
ok(ats.normalize_url("GitHub") == "" and ats.normalize_url("Portfolio") == "",
   "normalize_url drops 'GitHub'/'Portfolio' labels")

# ---------------------------------------------------------------------------
# 4. build_application_data (pure) — profile + resume overlay
# ---------------------------------------------------------------------------
print("\n[build_application_data]")


class FakeJob:
    id = 7
    title = "Staff Engineer"
    company = "Acme"
    url = "https://acme.example.com/job/7"


PROFILE = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "(555) 123-4567",
        "location": "London, England, UK",
        "linkedin": "https://linkedin.com/in/ada",
        "portfolio": "https://ada.dev",
    },
    "summary": "Engineer.",
    "work_history": [
        {"company": "Analytical Engines, Inc.", "title": "Lead (Remote)",
         "location": "London, UK", "start": "January 2020", "end": None,
         "bullets": ["Built things"], "skills": ["Python"]},
        {"company": "Babbage Co", "title": "Engineer", "location": "London",
         "start": "03/2016", "end": "12/2019", "bullets": []},
    ],
    "education": [
        {"school": "Cambridge", "degree": "BSc", "field": "Mathematics",
         "start": "2012", "end": "2016", "gpa": 3.9, "highlights": ["Honors"]},
    ],
    "skills": [{"name": "Python"}, {"name": "SQL"}],
}

data = ats.build_application_data(FakeJob(), PROFILE, resume_content=None)

ok(data["contact"]["first_name"] == "Ada" and data["contact"]["last_name"] == "Lovelace",
   "contact name split")
ok(data["contact"]["phone"] == "+15551234567", "contact phone normalized")
ok(data["contact"]["city"] == "London" and data["contact"]["country"] == "UK",
   "contact location parts")
ok(data["first_name"] == "Ada" and data["email"] == "ada@example.com",
   "flat identity keys present (autofill-payload compat)")

wh = data["work_history"]
ok(len(wh) == 2, "two work_history rows")
cur = wh[0]
ok(cur["current"] is True and cur["currently_work_here"] is True, "current role flagged (end=None)")
ok(cur["start_month"] == 1 and cur["start_year"] == 2020, "current role start month/year split")
ok(cur["start_display"] == "Jan 2020", "current role start display MMM YYYY")
ok(cur["end_month"] is None and cur["end_display"] == "", "current role has empty end")
ok(cur["title"] == "Lead", "work title normalized (dropped (Remote))")
ok(cur["company"] == "Analytical Engines, Inc.", "work company keeps legal suffix")

past = wh[1]
ok(past["current"] is False, "past role not current")
ok(past["start_month"] == 3 and past["start_year"] == 2016, "past role start 03/2016 split")
ok(past["end_month"] == 12 and past["end_year"] == 2019, "past role end 12/2019 split")

# (e) Real linkedin URL passes through unchanged (nested + flat keys).
ok(data["contact"]["linkedin"] == "https://linkedin.com/in/ada", "real linkedin URL passes through (contact)")
ok(data["linkedin_url"] == "https://linkedin.com/in/ada", "real linkedin URL passes through (flat)")

# (d) Label-only linkedin value drops to "" in both nested + flat keys.
PROFILE_LABEL = dict(PROFILE, contact=dict(PROFILE["contact"], linkedin="LinkedIn", github="GitHub"))
data_lbl = ats.build_application_data(FakeJob(), PROFILE_LABEL, resume_content=None)
ok(data_lbl["contact"]["linkedin"] == "" and data_lbl["contact"]["github"] == "",
   "label-only linkedin/github dropped to '' (contact)")
ok(data_lbl["linkedin_url"] == "" and data_lbl["github_url"] == "",
   "label-only linkedin/github dropped to '' (flat)")

# Combined range packed into a work-history start field with no separate end.
PROFILE_RANGE = dict(PROFILE, work_history=[
    {"company": "Range Co", "title": "Eng", "location": "NYC",
     "start": "Jan 2021 - Mar 2023", "end": None},
])
data_rng = ats.build_application_data(FakeJob(), PROFILE_RANGE, resume_content=None)
wr = data_rng["work_history"][0]
ok(wr["start_month"] == 1 and wr["start_year"] == 2021, "packed range start split (work)")
ok(wr["end_month"] == 3 and wr["end_year"] == 2023 and wr["current"] is False,
   "packed range end split, not current (work)")

edu = data["education"][0]
ok(edu["school"] == "Cambridge" and edu["field_of_study"] == "Mathematics", "education fields")
ok(edu["start_year"] == 2012 and edu["end_year"] == 2016, "education year-only dates split")
ok(edu["gpa"] == 3.9, "education gpa preserved")

# Resume overlay wins for work_history when populated.
RESUME = {"work_history": [
    {"company": "Tailored LLC", "title": "Principal", "location": "NYC",
     "start": "Feb 2021", "end": "Present"}
]}
data2 = ats.build_application_data(FakeJob(), PROFILE, resume_content=RESUME)
ok(len(data2["work_history"]) == 1 and data2["work_history"][0]["company"] == "Tailored LLC",
   "resume work_history overlays profile")
ok(data2["work_history"][0]["current"] is True, "resume 'Present' end -> current")
ok(data2["contact"]["email"] == "ada@example.com", "resume with no contact falls back to profile contact")

# ---------------------------------------------------------------------------
# 5. Endpoint: auth + normalized structure
# ---------------------------------------------------------------------------
print("\n[GET /api/ext/application-data/{job_id}]")
with Session(db_engine) as s:
    if s.get(Profile, 1) is None:
        s.add(Profile(id=1, contact=PROFILE["contact"], summary=PROFILE["summary"],
                      work_history=PROFILE["work_history"], education=PROFILE["education"],
                      skills=PROFILE["skills"], preferences={}))
    job = Job(source="pasted", source_id="ats|1", title="Staff Engineer",
              company="Acme", description="Python, SQL", url="https://acme.example.com/job/1")
    s.add(job)
    s.commit()
    s.refresh(job)
    job_id = job.id

client = TestClient(app, raise_server_exceptions=True)

r = client.get(f"/api/ext/application-data/{job_id}")
ok(r.status_code == 401, f"no token -> 401 (got {r.status_code})")

H = {"Authorization": "Bearer test-token"}
r = client.get(f"/api/ext/application-data/{job_id}", headers=H)
ok(r.status_code == 200, f"with token -> 200 (got {r.status_code})")
body = r.json()
ok(body["contact"]["first_name"] == "Ada" and body["contact"]["last_name"] == "Lovelace",
   "endpoint contact name split")
ok(body["contact"]["phone"] == "+15551234567", "endpoint phone normalized")
ok(len(body["work_history"]) == 2, "endpoint returns work_history rows")
w0 = body["work_history"][0]
ok(w0["start_month"] == 1 and w0["start_year"] == 2020 and w0["current"] is True,
   "endpoint work row has month/year split + current bool")
ok(body["education"][0]["start_year"] == 2012, "endpoint education dates normalized")
ok(body["job"]["id"] == job_id, "endpoint echoes job context")

r = client.get("/api/ext/application-data/999999", headers=H)
ok(r.status_code == 404, f"unknown job -> 404 (got {r.status_code})")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL ATS SMOKE TESTS PASSED.")
