"""Deterministic resume-formatting smoke test (pure, no DB/LLM/PDF).

Covers findmemyjob.resume_format — the pass that ENFORCES resume best practices
on tailored content regardless of LLM drift or heuristic fallback:

  (a) a role with 11 long bullets is capped per recency rules; no bullet exceeds
      the hard max length and none is truncated mid-word.
  (b) recency weighting: the most-recent role keeps more bullets than an old one.
  (c) skills: sentence/requirement entries are reduced to a tag or dropped;
      dedupe/synonym-merge works; categories are assigned (not all "Other").
  (d) format_resume_content returns a dict and is idempotent.

Run with:  .venv/bin/python resume_format_smoke.py
"""
import sys

sys.path.insert(0, "src")

from findmemyjob.resume_format import (  # noqa: E402
    BULLET_HARD_MAX,
    bullet_cap,
    categorize_skill,
    format_resume_content,
    normalize_skills,
    order_work_history,
    tighten_bullet,
)

failures = []


def ok(cond, desc):
    if cond:
        print(f"  OK   {desc}")
    else:
        failures.append(desc)
        print(f"  FAIL {desc}")


# ---------------------------------------------------------------------------
# (a) 11 long bullets on one role -> capped, length-guarded, no mid-word cut
# ---------------------------------------------------------------------------
print("\n[bullets: cap + length]")
LONG = (
    "Responsible for architecting and operating a large-scale distributed data "
    "pipeline that processed over 2 billion events per day across multiple AWS "
    "regions with high reliability and low latency for every downstream team"
)
eleven = [f"{LONG} (variant {i})" for i in range(11)]
JOB = "Senior data engineer. Build distributed pipelines on AWS. Python, Kafka."

content = {
    "summary": "Engineer.",
    "work_history": [
        {"company": "Recent Co", "title": "Staff Engineer", "bullets": list(eleven)},
    ],
    "skills": [],
    "education": [],
}
out = format_resume_content(content, job_text=JOB, page_length="auto")
role0 = out["work_history"][0]
ok(len(role0["bullets"]) == bullet_cap(0, "auto") == 6,
   f"11 bullets capped to 6 on recent role (got {len(role0['bullets'])})")
ok(all(len(b) <= BULLET_HARD_MAX for b in role0["bullets"]),
   f"every bullet <= {BULLET_HARD_MAX} chars")
ok(all(not b.lower().startswith("responsible for") for b in role0["bullets"]),
   "weak opener 'Responsible for' stripped")
# No mid-word truncation: the last token of each bullet is a whole word from source.
src_tokens = {w.strip(".,()").lower() for w in (LONG + " variant 0 1 2 3 4 5 6 7 8 9 10").split()}
ok(all(b.split()[-1].strip(".,()").lower() in src_tokens for b in role0["bullets"]),
   "no bullet ends mid-word (last token is a full source word)")

# tighten_bullet directly
short = tighten_bullet("Worked on migrating the billing service to Kubernetes")
ok(short.startswith("Migrating") and len(short) <= BULLET_HARD_MAX,
   "tighten_bullet strips 'Worked on' + capitalizes")

# ---------------------------------------------------------------------------
# (b) recency weighting: recent role keeps more bullets than an old one
# ---------------------------------------------------------------------------
print("\n[bullets: recency weighting]")
many = [f"Delivered project {i} improving throughput by {i*5} percent" for i in range(8)]
content_b = {
    "work_history": [
        {"company": "A", "title": "Recent", "bullets": list(many)},
        {"company": "B", "title": "Mid", "bullets": list(many)},
        {"company": "C", "title": "Old", "bullets": list(many)},
        {"company": "D", "title": "Older", "bullets": list(many)},
    ],
    "skills": [],
}
out_b = format_resume_content(content_b, job_text="throughput project", page_length="auto")
counts = [len(r["bullets"]) for r in out_b["work_history"]]
ok(counts[0] > counts[-1], f"recent role keeps more bullets than old ({counts[0]} > {counts[-1]})")
ok(counts == [6, 4, 3, 3], f"counts follow auto recency caps [6,4,3,3] (got {counts})")
ok(all(c <= 6 for c in counts) and all(c >= 2 for c in counts),
   "hard cap 6 / floor 2 respected")
# Floor: a role with only 1 source bullet keeps 1 (never fabricates up to the floor).
out_floor = format_resume_content(
    {"work_history": [{"company": "X", "title": "T", "bullets": ["Only one bullet here"]}], "skills": []},
    job_text="", page_length="auto")
ok(len(out_floor["work_history"][0]["bullets"]) == 1, "role with 1 source bullet stays 1 (no fabrication)")

# page_length biases counts
out_1 = format_resume_content(content_b, job_text="throughput project", page_length="1")
ok(len(out_1["work_history"][0]["bullets"]) == 4, "page_length=1 caps recent role at 4")

# ---------------------------------------------------------------------------
# (c) skills: sentence -> tag/drop, dedupe, categorize (not all Other)
# ---------------------------------------------------------------------------
print("\n[skills: tag extraction + categorize]")
skills_in = [
    {"name": "3-5+ years of experience in DTC creative strategy or performance marketing", "category": "Other"},
    {"name": "Experience building and testing creative concepts tied to direct-response marketing (hook, problem, mechanism, proof, CTA)", "category": "Other"},
    {"name": "React.js"},
    {"name": "ReactJS"},           # dedupes with React.js -> React
    {"name": "Docker"},
    {"name": "Python"},
    {"name": "Meta Ads"},
]
tags = normalize_skills(skills_in)
names = [t["name"] for t in tags]
cats = {t["category"] for t in tags}
ok("DTC Creative Strategy" in names and "Performance Marketing" in names,
   "requirement sentence reduced to core tags")
ok(not any("experience" in n.lower() or "years" in n.lower() for n in names),
   "no requirement-sentence entries survive")
ok(names.count("React") == 1 and "React.js" not in names,
   "React.js / ReactJS dedupe+merge to single 'React'")
ok(cats != {"Other"} and len(cats) >= 2, f"multiple real categories assigned ({cats})")
ok(categorize_skill("Python") == "Languages", "Python -> Languages")
ok(categorize_skill("Docker") == "Cloud & DevOps", "Docker -> Cloud & DevOps")
ok(categorize_skill("Meta Ads") == "Marketing/Domain", "Meta Ads -> Marketing/Domain")
ok(all(len(n.split()) <= 4 for n in names), "all skills are short tags (<=4 words)")

# ---------------------------------------------------------------------------
# (d) content stays a dict; pass is idempotent
# ---------------------------------------------------------------------------
print("\n[content shape + idempotency]")
ok(isinstance(out, dict) and isinstance(out["skills"], list), "format_resume_content returns a dict")
ok(all(isinstance(r, dict) for r in out["work_history"]), "work_history entries stay dicts")
once = format_resume_content(content, job_text=JOB, page_length="auto")
twice = format_resume_content(once, job_text=JOB, page_length="auto")
ok(once == twice, "pass is idempotent (running twice is a no-op)")
ok("summary" in out and out["summary"] == "Engineer.", "summary passes through untouched")

# ---------------------------------------------------------------------------
# (e) bullets never end mid-word or on a dangling connective (issue 1)
# ---------------------------------------------------------------------------
print("\n[bullets: clause-based trim, no mid-word / dangling connective]")
_CONNECTIVES = {
    "and", "or", "the", "a", "an", "to", "of", "for", "with", "in", "on", "by",
    "at", "as", "that", "which", "into", "from", "via", "&",
}
# A long bullet with natural clause boundaries (commas + "and").
BULLET_CLAUSES = (
    "Led end-to-end creative production for direct-response campaigns, owning "
    "initial audience research, scripting, and editing while translating the "
    "product/expert mechanism, proof, and offer into scroll-stopping hooks"
)
# A long run-on with NO clause boundary before the limit (forces word-cut path).
BULLET_RUNON = (
    "Developed a comprehensive scalable maintainable observability platform "
    "providing realtime actionable insights across distributed heterogeneous "
    "microservice deployments worldwide continuously without interruption"
)


def _ends_ok(b, source):
    if len(b) > BULLET_HARD_MAX:
        return False
    last = b.split()[-1].strip(".,;:()&-").lower()
    if not last or last in _CONNECTIVES:
        return False
    if b.rstrip()[-1] in ",;:-–—&":
        return False
    src_tokens = {w.strip(".,;:()/").lower() for w in source.split()}
    return last.split("/")[-1] in src_tokens or last in src_tokens


for raw in (BULLET_CLAUSES, BULLET_RUNON):
    t = tighten_bullet(raw)
    ok(_ends_ok(t, raw),
       f"bullet ends on a whole word, no dangling connective, <= {BULLET_HARD_MAX} "
       f"(got ...{t[-40:]!r}, len {len(t)})")

# ---------------------------------------------------------------------------
# (f) skill extraction: compound tokens, canonical tags, proper-noun casing
# ---------------------------------------------------------------------------
print("\n[skills: compound tokens + canonical tags + proper-noun casing]")

t1 = [s["name"] for s in normalize_skills(
    [{"name": "UX/UI collaboration and human-centered design experience"}])]
ok(t1 == ["UX/UI", "Human-Centered Design"],
   f"'UX/UI collaboration and human-centered design experience' -> {t1}")

t2 = [s["name"] for s in normalize_skills(
    [{"name": "Deep React component architecture and design system translation (Figma to React)"}])]
ok(t2 == ["React", "Design Systems", "Figma"],
   f"'Deep React ... (Figma to React)' -> {t2}")

ok("UX/UI" in t1 and "UI Collaboration" not in t1,
   "UX/UI kept intact as one tag (not split into 'UX' + 'UI ...')")

casing = {s["name"] for s in normalize_skills(
    [{"name": "CapCut"}, {"name": "capcut"}, {"name": "github"},
     {"name": "tiktok"}, {"name": "node.js"}, {"name": "postgresql"}])}
ok("CapCut" in casing and "Capcut" not in casing, "CapCut casing preserved (not 'Capcut')")
ok("GitHub" in casing and "TikTok" in casing, "GitHub / TikTok casing preserved")
ok("Node.js" in casing and "PostgreSQL" in casing, "Node.js / PostgreSQL casing preserved")

sk_in = [{"name": "UX/UI collaboration and human-centered design experience"},
         {"name": "Deep React component architecture and design system translation (Figma to React)"},
         {"name": "CapCut"}]
once_sk = normalize_skills(sk_in)
twice_sk = normalize_skills([{"name": s["name"]} for s in once_sk])
ok([s["name"] for s in once_sk] == [s["name"] for s in twice_sk],
   "skill normalization is idempotent on refined inputs")

# ---------------------------------------------------------------------------
# (g) full-pass idempotency incl. skill categorization (regression: id=37)
# ---------------------------------------------------------------------------
print("\n[idempotency: format_resume_content(x2) == format_resume_content(x1)]")

# Real id=37 skill set. Legacy free-text buckets on the raw rows previously
# flipped to "Other" on the second pass; now they must be a stable fixed point.
ID37 = {
    "summary": "Security engineer.",
    "work_history": [
        {"company": "Acme", "title": "Security Engineer", "bullets": [
            "Hardened Active Directory and remediated vulnerabilities across the fleet",
            "Automated SIEM alerting with Python and Bash",
        ]},
    ],
    "skills": [
        {"name": "Active Directory", "category": "tools"},
        {"name": "Cisco Meraki", "category": "tools"},
        {"name": "Jfrog Artifactory", "category": "tools"},
        {"name": "Azure", "category": "cloud"},
        {"name": "Jira", "category": "tools"},
        {"name": "SIEM", "category": "other"},
        {"name": "Vulnerability Remediation", "category": "other"},
        {"name": "Change Management", "category": "other"},
        {"name": "Python", "category": "language"},
        {"name": "Bash", "category": "language"},
        {"name": "SQL", "category": "language"},
        {"name": "JavaScript", "category": "language"},
        {"name": "Docker", "category": "cloud"},
        {"name": "Kubernetes", "category": "cloud"},
    ],
    "education": [],
}
JOB37 = "Security engineer. Active Directory, SIEM, vulnerability remediation, Python."

fixtures = [
    ("id=37 security skills", ID37, JOB37),
    ("case-a engineer", content, JOB),
    ("case-b recency", content_b, "throughput project"),
]
for label, fx, jt in fixtures:
    p1 = format_resume_content(fx, job_text=jt, page_length="auto")
    p2 = format_resume_content(p1, job_text=jt, page_length="auto")
    p3 = format_resume_content(p2, job_text=jt, page_length="auto")
    ok(p1 == p2 == p3, f"{label}: double/triple application == single application")

# Prove the exact reported regression is fixed and correctly categorized.
id37_p1 = format_resume_content(ID37, job_text=JOB37)["skills"]
id37_p2 = format_resume_content(
    {"skills": id37_p1}, job_text=JOB37)["skills"]
cats = {s["name"]: s["category"] for s in id37_p1}
print("  --- id=37 skill categories (pass 1) ---")
for s in id37_p1:
    print(f"      {s['name']:26s} {s['category']}")
ok(id37_p1 == id37_p2, "id=37 skill categorization is a stable fixed point (pass1 == pass2)")
ok(cats.get("Active Directory") == "Tools & Platforms"
   and cats.get("Cisco Meraki") == "Tools & Platforms"
   and cats.get("JFrog Artifactory") == "Tools & Platforms",
   "Active Directory / Cisco Meraki / JFrog Artifactory -> Tools & Platforms")
ok(cats.get("Docker") == "Cloud & DevOps" and cats.get("Kubernetes") == "Cloud & DevOps"
   and cats.get("Azure") == "Cloud & DevOps", "Docker / Kubernetes / Azure stay Cloud & DevOps")
ok(cats.get("Jira") == "Tools & Platforms", "Jira -> Tools & Platforms")
ok("JFrog Artifactory" in cats and "Jfrog Artifactory" not in cats,
   "JFrog casing preserved (not 'Jfrog')")

# ---------------------------------------------------------------------------
# (h) work-history ordering: pure reverse-chronological by START date, stable
# ---------------------------------------------------------------------------
print("\n[work-history: reverse-chronological ordering]")

# Exact resume-54 role set (see BUILD_SPEC_work_history_order.md). Sort is by
# START date only: the ongoing 2019 BrightMinds role sinks to the BOTTOM because
# it has the oldest start — ongoing status no longer floats it to the top.
RESUME54 = [
    {"company": "BrightMinds", "title": "Engineer", "start": "2019-06", "end": None},
    {"company": "Apple", "title": "Staff Engineer", "start": "2026-01", "end": "Present"},
    {"company": "Genius", "title": "Analyst", "start": "2024-02", "end": "2025-12"},
    {"company": "SOC", "title": "SOC Analyst", "start": "2023-06", "end": "2024-02"},
]
ordered = order_work_history(RESUME54)
order_names = [r["company"] for r in ordered]
ok(order_names == ["Apple", "Genius", "SOC", "BrightMinds"],
   f"resume-54 orders Apple->Genius->SOC->BrightMinds by start date (got {order_names})")

# Two ongoing roles order by start desc.
two_ongoing = order_work_history([
    {"company": "Old", "start": "2019-01", "end": None},
    {"company": "New", "start": "2023-05", "end": "Present"},
])
ok([r["company"] for r in two_ongoing] == ["New", "Old"],
   "two ongoing roles order by start descending")

# Equal start date: ongoing ranks above ended, then end date descending.
tie = order_work_history([
    {"company": "Ended", "start": "2020-01", "end": "2022-01"},
    {"company": "Ongoing", "start": "2020-01", "end": None},
])
ok([r["company"] for r in tie] == ["Ongoing", "Ended"],
   "equal start: ongoing role ranks above ended role")

# Idempotency + stability.
once_o = order_work_history(RESUME54)
twice_o = order_work_history(once_o)
ok(once_o == twice_o, "order_work_history is idempotent")
ok([r["company"] for r in twice_o] == ["Apple", "Genius", "SOC", "BrightMinds"],
   "re-ordering an ordered list is a no-op")

# Unparseable / missing START dates don't crash and sink to the end (stable).
messy = order_work_history([
    {"company": "Good", "start": "2022-03", "end": "2023-01"},
    {"company": "NoDates"},
    {"company": "Junk", "start": "not-a-date", "end": "whenever"},
])
mnames = [r["company"] for r in messy]
# "Good" has a parseable start -> top; "NoDates"/"Junk" have no parseable start
# -> sink to the bottom, keeping their original relative order.
ok("Good" in mnames and len(mnames) == 3, "unparseable dates tolerated, no role lost")
ok(mnames[0] == "Good", "role with a parseable start ranks above start-less roles")
ok(mnames.index("NoDates") < mnames.index("Junk"),
   "equal-key roles keep original relative order (stable)")

# No role fields lost through the full format pass.
fmt54 = format_resume_content(
    {"work_history": RESUME54, "skills": [], "summary": "x"}, job_text="")
ok([r["company"] for r in fmt54["work_history"]] == ["Apple", "Genius", "SOC", "BrightMinds"],
   "format_resume_content applies reverse-chronological ordering")
ok(len(fmt54["work_history"]) == 4 and all("title" in r for r in fmt54["work_history"]),
   "all roles + fields preserved through the format pass")
p1_54 = format_resume_content({"work_history": RESUME54, "skills": []}, job_text="")
p2_54 = format_resume_content(p1_54, job_text="")
ok(p1_54 == p2_54, "format pass with ordering is idempotent")

# ---------------------------------------------------------------------------
# (i) stringified-null end ("None"/"null"/"") -> ongoing + normalized to None
# ---------------------------------------------------------------------------
print("\n[work-history: stringified-null end treated as ongoing]")

# resume-55 shape: two ongoing roles whose end was str()'d to the literal "None".
RESUME55 = [
    {"company": "BrightMinds", "title": "Engineer", "start": "2019-06", "end": "None"},
    {"company": "Apple", "title": "Staff Engineer", "start": "2026-01", "end": "None"},
    {"company": "Genius", "title": "Analyst", "start": "2024-02", "end": "2025-12"},
    {"company": "SOC", "title": "SOC Analyst", "start": "2023-06", "end": "2024-02"},
]
ord55 = [r["company"] for r in order_work_history(RESUME55)]
ok(ord55 == ["Apple", "Genius", "SOC", "BrightMinds"],
   f"end='None' still treated as ongoing; sort by start date: {ord55}")

# Other nullish sentinels also count as ongoing.
for sentinel in ("none", "null", "", "N/A", "-"):
    o = order_work_history([
        {"company": "Ended", "start": "2010-01", "end": "2011-01"},
        {"company": "Live", "start": "2020-01", "end": sentinel},
    ])
    ok([r["company"] for r in o][0] == "Live",
       f"end={sentinel!r} treated as ongoing (sorts above ended role)")

# format_resume_content normalizes the sentinel strings to real Python None.
fmt55 = format_resume_content({"work_history": RESUME55, "skills": []}, job_text="")
apple = next(r for r in fmt55["work_history"] if r["company"] == "Apple")
bright = next(r for r in fmt55["work_history"] if r["company"] == "BrightMinds")
ok(apple["end"] is None and bright["end"] is None,
   "end 'None' (string) normalized to real None in content")
ok([r["company"] for r in fmt55["work_history"]] == ["Apple", "Genius", "SOC", "BrightMinds"],
   "resume-55 reorders correctly through the full format pass")

# The reformat pass MUST see this as a change (so stale resumes get fixed).
ok(fmt55["work_history"] != RESUME55,
   "resume-55 content changes under format pass (reformat will pick it up)")

# start='null'/'None' also cleaned to None; still idempotent.
fmt_null = format_resume_content(
    {"work_history": [{"company": "X", "title": "T", "start": "null", "end": "None"}], "skills": []},
    job_text="")
ok(fmt_null["work_history"][0]["start"] is None and fmt_null["work_history"][0]["end"] is None,
   "start 'null' and end 'None' both normalized to real None")
p1_55 = format_resume_content({"work_history": RESUME55, "skills": []}, job_text="")
p2_55 = format_resume_content(p1_55, job_text="")
ok(p1_55 == p2_55, "format pass idempotent after null-date normalization")

# Education gpa/start/end sentinels normalized to real None.
fmt_edu = format_resume_content(
    {"work_history": [],
     "education": [{"school": "MIT", "degree": "BS", "field": "CS",
                    "start": "2011", "end": "None", "gpa": "None"}],
     "skills": []},
    job_text="")
edu0 = fmt_edu["education"][0]
ok(edu0["gpa"] is None and edu0["end"] is None,
   "education gpa/end 'None' normalized to real None")

# ---------------------------------------------------------------------------
# (j) rendered resume.html never leaks the literal word "None"
# ---------------------------------------------------------------------------
print("\n[template render: no literal 'None' leaks]")
# Import the configured Jinja env (registers the `clean` filter).
from findmemyjob.pdf import _jinja  # noqa: E402
import re as _re  # noqa: E402

_tpl = _jinja.get_template("resume.html")
html = _tpl.render(
    contact={"name": "Grace Hopper", "email": "grace@example.com",
             "phone": None, "location": "None", "linkedin": "null"},
    summary="Engineer.",
    work_history=[
        {"title": "Staff Engineer", "company": "Apple", "start": "2026-01",
         "end": "None", "location": None, "bullets": ["Led migration to GKE", "None"]},
        {"title": "Analyst", "company": "Genius", "start": "2024-02",
         "end": "2025-12", "bullets": ["Built dashboards"]},
    ],
    # Education entry MISSING gpa (renders GPA: None before the fix).
    education=[{"school": "MIT", "degree": "BS", "field": "CS",
                "start": "2011", "end": "2015", "gpa": None}],
    skills=[{"name": "Python", "category": "Languages"},
            {"name": "None", "category": None}],
    # Certification MISSING date (renders trailing "None" before the fix).
    certifications=[{"name": "CompTIA A+", "issuer": "CompTIA", "date_earned": None},
                    {"name": "AZ-900", "issuer": "Microsoft", "date_earned": "None"}],
)

# No standalone word "None" (word-boundary) anywhere in the rendered output.
none_hits = _re.findall(r"\bNone\b", html)
ok(not none_hits, f"rendered resume contains no standalone 'None' (found {len(none_hits)})")
ok("null" not in html.lower().replace("nullish", ""), "no leaked 'null' in output")
ok("GPA:" not in html, "GPA line omitted entirely when gpa is missing")
ok("2026-01 – Present" in html, "ongoing role (end='None') renders as 'Present'")
ok("CompTIA A+" in html and "AZ-900" in html, "certifications still render their names")
ok("Led migration to GKE" in html, "real content still renders")

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL RESUME-FORMAT SMOKE TESTS PASSED.")
