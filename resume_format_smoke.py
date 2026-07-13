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

print("\n" + "=" * 40)
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL RESUME-FORMAT SMOKE TESTS PASSED.")
