"""Skill Growth Engine.

Three jobs, all degrading gracefully to heuristics when the LLM is unavailable:

1. Weighted skill-gap analysis across the whole job pipeline. Heuristic
   extraction (a curated vocabulary scanned over titles/descriptions/gaps) does
   the heavy lifting so it stays fast over ~1700 jobs; the LLM is used only to
   write short rationales for the top candidates in one batched call.
2. Per-skill learning content: a curated path (milestones + resources + a time
   estimate) and interactive practice (quiz + flashcards + drills).
3. An AI tutor chat per skill (mirrors the interview engine) and a resume-loop
   suggestion (skill entry + polished bullets) once a skill reaches fluency.

Nothing here raises to the caller — a busy/absent model produces a sensible
canned result rather than a 500.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from findmemyjob.llm import DEFAULT_MATCH_MODEL, DEFAULT_TAILOR_MODEL, _strip_code_fence, llm

# ---------------------------------------------------------------------------
# Heuristic skill vocabulary
#
# Multi-word entries are matched first (so "machine learning" wins over "learning").
# The canonical form on the right is what we cluster to; the keys are the
# lowercase surface forms we look for as whole-word matches.
# ---------------------------------------------------------------------------

_SKILL_CANON: Dict[str, str] = {}


def _register(canonical: str, *aliases: str) -> None:
    _SKILL_CANON[canonical.lower()] = canonical
    for a in aliases:
        _SKILL_CANON[a.lower()] = canonical


# Languages
_register("Python", "python3")
_register("JavaScript", "javascript")
_register("TypeScript", "typescript")
_register("Java")
_register("Go", "golang")
_register("Rust")
_register("C++", "cpp")
_register("C#", "c-sharp", "csharp")
_register("Ruby")
_register("PHP")
_register("Swift")
_register("Kotlin")
_register("Scala")
_register("SQL")
_register("Bash", "shell scripting")
# Frameworks / libraries
_register("React", "react.js", "reactjs")
_register("Vue", "vue.js", "vuejs")
_register("Angular")
_register("Node.js", "node", "nodejs")
_register("Django")
_register("Flask")
_register("FastAPI")
_register("Spring", "spring boot")
_register("Rails", "ruby on rails")
_register(".NET", "dotnet", "asp.net")
_register("Next.js", "nextjs")
_register("GraphQL")
_register("REST APIs", "rest", "restful")
# Data / ML
_register("Machine Learning", "machine learning")
_register("Deep Learning")
_register("TensorFlow")
_register("PyTorch")
_register("Pandas")
_register("NumPy", "numpy")
_register("Spark", "apache spark", "pyspark")
_register("Airflow", "apache airflow")
_register("dbt")
_register("Data Engineering")
_register("Data Analysis")
_register("Data Visualization")
_register("ETL", "elt")
_register("Statistics")
_register("NLP", "natural language processing")
_register("LLMs", "large language models", "genai", "generative ai")
_register("Tableau")
_register("Power BI", "powerbi")
_register("Snowflake")
_register("BigQuery")
_register("Redshift")
# Cloud / infra / devops
_register("AWS", "amazon web services")
_register("Azure")
_register("GCP", "google cloud", "google cloud platform")
_register("Docker")
_register("Kubernetes", "k8s")
_register("Terraform")
_register("Ansible")
_register("CI/CD", "ci-cd", "continuous integration")
_register("Linux")
_register("Git")
_register("Jenkins")
_register("Kafka", "apache kafka")
_register("Redis")
_register("Elasticsearch")
_register("Microservices")
_register("Serverless")
_register("DevOps")
_register("Observability", "monitoring")
# Databases
_register("PostgreSQL", "postgres")
_register("MySQL")
_register("MongoDB", "mongo")
_register("DynamoDB")
_register("Cassandra")
# Product / design / process
_register("Agile", "scrum")
_register("Product Management")
_register("Project Management")
_register("UX Design", "user experience")
_register("UI Design")
_register("Figma")
_register("System Design")
_register("Distributed Systems")
_register("API Design")
_register("Security", "cybersecurity", "infosec")
_register("Testing", "unit testing", "test automation")
# Soft / leadership
_register("Leadership")
_register("Communication")
_register("Mentorship", "mentoring")
_register("Stakeholder Management")

# Longest surface forms first so multi-word phrases win.
_SURFACE_FORMS = sorted(_SKILL_CANON.keys(), key=len, reverse=True)


def _compile_patterns() -> List[tuple]:
    pats = []
    for surface in _SURFACE_FORMS:
        # Word-ish boundaries that tolerate the punctuation in c++, ci/cd, node.js.
        esc = re.escape(surface)
        pat = re.compile(r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])", re.IGNORECASE)
        pats.append((pat, _SKILL_CANON[surface]))
    return pats


_PATTERNS = _compile_patterns()


def extract_skills(text: str) -> List[str]:
    """Return the set of canonical skills mentioned in *text* (heuristic)."""
    if not text:
        return []
    found = set()
    for pat, canon in _PATTERNS:
        if pat.search(text):
            found.add(canon)
    return sorted(found)


# ---------------------------------------------------------------------------
# Weighted gap analysis
# ---------------------------------------------------------------------------

def _profile_skill_names(profile: Dict[str, Any]) -> set:
    names = set()
    for s in profile.get("skills") or []:
        nm = (s.get("name") if isinstance(s, dict) else str(s)) or ""
        canon = _SKILL_CANON.get(nm.strip().lower())
        names.add((canon or nm).strip().lower())
    return {n for n in names if n}


def analyze_skill_gaps(
    jobs: List[Any],
    applications: List[Any],
    profile: Dict[str, Any],
    *,
    use_llm: bool = True,
    top_n: int = 20,
) -> List[Dict[str, Any]]:
    """Rank skills across the pipeline, weighted by frequency, fit, and gap.

    `jobs` are Job rows, `applications` are Application rows. Returns a list of
    dicts ready to persist as SkillInsight rows, ranked best-first. Never raises.
    """
    applied_job_ids = {getattr(a, "job_id", None) for a in (applications or [])}
    # Gaps the user has already been told about (job.fit_gaps + application.gaps).
    gap_terms: set = set()
    for j in jobs or []:
        for g in (getattr(j, "fit_gaps", None) or []):
            gap_terms |= set(extract_skills(str(g)))
    for a in applications or []:
        for g in (getattr(a, "gaps", None) or []):
            gap_terms |= set(extract_skills(str(g)))

    have = _profile_skill_names(profile)

    freq: Dict[str, int] = defaultdict(int)
    weight: Dict[str, float] = defaultdict(float)
    in_target: Dict[str, bool] = defaultdict(bool)
    samples: Dict[str, List[str]] = defaultdict(list)

    for j in jobs or []:
        text = " ".join(str(x or "") for x in (
            getattr(j, "title", ""), getattr(j, "description", ""),
            " ".join(getattr(j, "fit_gaps", None) or []),
        ))
        skills = extract_skills(text)
        if not skills:
            continue
        fit = getattr(j, "fit_score", None)
        is_applied = getattr(j, "id", None) in applied_job_ids
        # Base weight scaled by how much this job matters to the user.
        w = 1.0
        if isinstance(fit, (int, float)):
            w *= 1.0 + (float(fit) / 100.0)
        if is_applied:
            w += 0.75
        target = (isinstance(fit, (int, float)) and float(fit) >= 70.0) or is_applied
        for sk in skills:
            freq[sk] += 1
            weight[sk] += w
            if target:
                in_target[sk] = True
            if len(samples[sk]) < 5:
                title = getattr(j, "title", None)
                if title and title not in samples[sk]:
                    samples[sk].append(title)

    results: List[Dict[str, Any]] = []
    for sk, f in freq.items():
        key = sk.lower()
        is_gap = (key not in have) or (sk in gap_terms)
        w = weight[sk]
        # Gap boost + target boost — surface high-value, learnable gaps first.
        score = w
        if is_gap:
            score *= 1.6
        if sk in gap_terms:
            score *= 1.25
        if in_target[sk]:
            score *= 1.15
        results.append({
            "name": sk,
            "frequency": f,
            "weighted_score": round(score, 2),
            "appears_in_target": bool(in_target[sk]),
            "is_gap": bool(is_gap),
            "sample_job_titles": samples[sk][:5],
            "rationale": "",
        })

    results.sort(key=lambda r: r["weighted_score"], reverse=True)
    results = results[:top_n]

    _attach_rationales(results, profile, have, use_llm=use_llm)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return results


def _heuristic_rationale(r: Dict[str, Any], have: set) -> str:
    bits = [f"Appears in {r['frequency']} of your saved jobs"]
    if r["appears_in_target"]:
        bits.append("including high-fit / applied roles")
    if r["is_gap"]:
        if r["name"].lower() in have:
            bits.append("flagged as a gap in your matches")
        else:
            bits.append("not yet on your resume")
    else:
        bits.append("already on your resume — worth deepening")
    return "; ".join(bits) + "."


def _attach_rationales(
    results: List[Dict[str, Any]], profile: Dict[str, Any], have: set, *, use_llm: bool,
) -> None:
    """Fill each result's rationale. One batched LLM call; heuristic on failure."""
    for r in results:
        r["rationale"] = _heuristic_rationale(r, have)
    if not use_llm or not results:
        return
    skills_list = "\n".join(
        f"- {r['name']} (in {r['frequency']} jobs, gap={r['is_gap']}, "
        f"high-fit={r['appears_in_target']})"
        for r in results
    )
    instructions = (
        "You are a career coach. For each skill below, write ONE short sentence "
        "(max 22 words) on why learning it would most improve this candidate's job "
        "prospects, grounded in the frequency/gap/fit signals given. Be concrete and "
        "encouraging.\n\n"
        "Return STRICT JSON, no markdown, no code fence:\n"
        '{"rationales": {"<skill name>": "<one sentence>"}}'
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=instructions,
            user_prompt=f"SKILLS:\n{skills_list}\n\nOutput JSON only.",
            model=DEFAULT_MATCH_MODEL,
            max_tokens=1200,
            temperature=0.4,
        )
        data = _parse_json(raw) or {}
        rats = data.get("rationales")
        if isinstance(rats, dict):
            for r in results:
                val = rats.get(r["name"])
                if isinstance(val, str) and val.strip():
                    r["rationale"] = val.strip()[:300]
    except Exception as e:  # noqa: BLE001 — never 500 the re-analyze
        print(f"[skills] rationale LLM failed: {e}")


# ---------------------------------------------------------------------------
# Tolerant JSON parsing (shared shape with interview.py)
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    cleaned = _strip_code_fence(raw or "").strip()
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Learning path
# ---------------------------------------------------------------------------

_PATH_INSTRUCTIONS = """\
You are an expert curriculum designer. Build a concrete learning path to take a
motivated professional from beginner to fluent in ONE named skill, tailored to
their background (given as the profile). Three milestones: Beginner,
Intermediate, Fluent. Each milestone has 2-4 concrete objectives and 1-3 real,
well-known resources (name them; only include a link if it's a canonical site
like the official docs — never invent URLs). Give a realistic total time estimate.

Return STRICT JSON, no markdown, no code fence:
{
  "skill": "<skill>",
  "time_to_fluency": "e.g. 6-8 weeks part-time",
  "milestones": [
    {"level": "Beginner", "objectives": ["..."],
     "resources": [{"name": "...", "kind": "free|paid|docs", "link": ""}]},
    {"level": "Intermediate", "objectives": ["..."], "resources": [...]},
    {"level": "Fluent", "objectives": ["..."], "resources": [...]}
  ]
}
"""


def generate_learning_path(skill: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-authored learning path for *skill*; deterministic fallback on failure."""
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=_PATH_INSTRUCTIONS,
            user_prompt=f"SKILL: {skill}\n\nBuild the path. Output JSON only.",
            model=DEFAULT_MATCH_MODEL,
            max_tokens=1400,
            temperature=0.5,
        )
        data = _parse_json(raw)
        if data:
            norm = _normalize_path(data, skill)
            if norm["milestones"]:
                return norm
    except Exception as e:  # noqa: BLE001
        print(f"[skills] learning path LLM failed: {e}")
    return _fallback_path(skill)


def _normalize_path(data: Dict[str, Any], skill: str) -> Dict[str, Any]:
    milestones = []
    for m in (data.get("milestones") or []):
        if not isinstance(m, dict):
            continue
        objectives = [str(o)[:240] for o in (m.get("objectives") or []) if str(o).strip()][:6]
        resources = []
        for res in (m.get("resources") or []):
            if isinstance(res, dict) and res.get("name"):
                resources.append({
                    "name": str(res.get("name"))[:160],
                    "kind": str(res.get("kind") or "free")[:12],
                    "link": str(res.get("link") or "")[:300],
                })
        milestones.append({
            "level": str(m.get("level") or "")[:40] or "Milestone",
            "objectives": objectives,
            "resources": resources[:4],
        })
    return {
        "skill": skill,
        "time_to_fluency": str(data.get("time_to_fluency") or "")[:80] or "6-8 weeks part-time",
        "milestones": milestones[:5],
        "generated_by": "ai",
    }


def _fallback_path(skill: str) -> Dict[str, Any]:
    return {
        "skill": skill,
        "time_to_fluency": "6-8 weeks part-time",
        "generated_by": "fallback",
        "milestones": [
            {"level": "Beginner",
             "objectives": [
                 f"Learn the core concepts and vocabulary of {skill}.",
                 f"Follow an introductory tutorial and build one tiny {skill} example.",
             ],
             "resources": [
                 {"name": f"Official {skill} documentation / getting-started guide",
                  "kind": "docs", "link": ""},
                 {"name": "A well-reviewed beginner course (Coursera / freeCodeCamp / YouTube)",
                  "kind": "free", "link": ""},
             ]},
            {"level": "Intermediate",
             "objectives": [
                 f"Build a small end-to-end project using {skill}.",
                 f"Read others' {skill} code and learn common patterns and pitfalls.",
             ],
             "resources": [
                 {"name": f"A project-based {skill} course or book", "kind": "paid", "link": ""},
                 {"name": "Open-source repos that use it in production", "kind": "free", "link": ""},
             ]},
            {"level": "Fluent",
             "objectives": [
                 f"Ship a portfolio-quality project demonstrating {skill}.",
                 f"Teach it back — write up or explain a {skill} concept to cement mastery.",
             ],
             "resources": [
                 {"name": f"Advanced {skill} references and community forums", "kind": "free", "link": ""},
             ]},
        ],
    }


# ---------------------------------------------------------------------------
# Interactive practice (quiz + flashcards + drills)
# ---------------------------------------------------------------------------

_PRACTICE_INSTRUCTIONS = """\
You are an expert tutor. Create interactive practice for ONE named skill:
- A quiz: 4 multiple-choice questions. Each has "question", "options" (exactly 4
  strings), and "answer" (the 0-based index of the correct option).
- Flashcards: 4 {front, back} concept pairs.
- Drills: 3 short hands-on exercises (concept or coding) as strings.
Keep it accurate and genuinely useful for reaching fluency.

Return STRICT JSON, no markdown, no code fence:
{
  "quiz": [{"question": "...", "options": ["a","b","c","d"], "answer": 0}],
  "flashcards": [{"front": "...", "back": "..."}],
  "drills": ["...", "..."]
}
"""


def generate_practice(skill: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-authored practice set for *skill*; deterministic fallback on failure."""
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=_PRACTICE_INSTRUCTIONS,
            user_prompt=f"SKILL: {skill}\n\nCreate the practice set. Output JSON only.",
            model=DEFAULT_MATCH_MODEL,
            max_tokens=1600,
            temperature=0.5,
        )
        data = _parse_json(raw)
        if data:
            norm = _normalize_practice(data, skill)
            if norm["quiz"] or norm["flashcards"]:
                return norm
    except Exception as e:  # noqa: BLE001
        print(f"[skills] practice LLM failed: {e}")
    return _fallback_practice(skill)


def _normalize_practice(data: Dict[str, Any], skill: str) -> Dict[str, Any]:
    quiz = []
    for q in (data.get("quiz") or []):
        if not isinstance(q, dict):
            continue
        opts = [str(o)[:200] for o in (q.get("options") or []) if str(o).strip()]
        if len(opts) < 2 or not q.get("question"):
            continue
        try:
            ans = int(q.get("answer", 0))
        except (TypeError, ValueError):
            ans = 0
        ans = max(0, min(len(opts) - 1, ans))
        quiz.append({"question": str(q["question"])[:400], "options": opts[:4], "answer": ans})
    cards = []
    for c in (data.get("flashcards") or []):
        if isinstance(c, dict) and c.get("front") and c.get("back"):
            cards.append({"front": str(c["front"])[:300], "back": str(c["back"])[:600]})
    drills = [str(d)[:400] for d in (data.get("drills") or []) if str(d).strip()]
    return {
        "quiz": quiz[:8],
        "flashcards": cards[:8],
        "drills": drills[:6],
        "generated_by": "ai",
    }


def _fallback_practice(skill: str) -> Dict[str, Any]:
    return {
        "generated_by": "fallback",
        "quiz": [
            {"question": f"What is the best first step when learning {skill}?",
             "options": [
                 "Skim the official documentation and build a tiny example",
                 "Memorize every API method before writing any code",
                 "Avoid tutorials entirely",
                 "Only read theory, never practice",
             ], "answer": 0},
            {"question": f"Which habit most reinforces fluency in {skill}?",
             "options": [
                 "Deliberate practice on real projects",
                 "Reading passively once",
                 "Waiting until you feel ready",
                 "Skipping the fundamentals",
             ], "answer": 0},
        ],
        "flashcards": [
            {"front": f"Why learn {skill}?",
             "back": f"It appears frequently in roles you're targeting and strengthens your profile."},
            {"front": f"How do you reach fluency in {skill}?",
             "back": "Milestone-based practice: learn concepts, build projects, then teach it back."},
        ],
        "drills": [
            f"Write a one-paragraph explanation of {skill} as if teaching a peer.",
            f"Build a minimal project that uses {skill} end to end.",
            f"Find and read one real-world {skill} codebase or case study.",
        ],
    }


def grade_quiz(practice: Dict[str, Any], answers: Dict[int, int]) -> Dict[str, Any]:
    """Grade submitted quiz answers against the stored practice set.

    `answers` maps question index -> chosen option index. Deterministic; never
    raises. Returns {score, total, pct, per_question:[{correct, chosen, answer}]}.
    """
    quiz = (practice or {}).get("quiz") or []
    total = len(quiz)
    correct = 0
    per_q = []
    for i, q in enumerate(quiz):
        chosen = answers.get(i)
        ans = q.get("answer", 0)
        is_right = chosen is not None and int(chosen) == int(ans)
        if is_right:
            correct += 1
        per_q.append({"chosen": chosen, "answer": ans, "correct": is_right})
    pct = round(100 * correct / total) if total else 0
    return {"score": correct, "total": total, "pct": pct, "per_question": per_q}


# ---------------------------------------------------------------------------
# AI tutor chat (mirrors interview.py)
# ---------------------------------------------------------------------------

_TUTOR_INSTRUCTIONS = """\
You are a friendly, expert one-on-one tutor for ONE specific skill. You teach,
explain with concrete examples, quiz the student, and adapt to their level and
progress. Keep replies focused and conversational (2-6 sentences). When the
student answers a question, react honestly, correct gently, then move them one
step forward. Occasionally pose a small check-for-understanding question.
Respond in plain prose (no JSON, no markdown headers).
"""


def tutor_opening(skill: str, profile: Dict[str, Any], progress_note: str = "") -> str:
    """First tutor message for a skill session. Falls back to a canned greeting."""
    user_prompt = (
        f"SKILL TO TUTOR: {skill}\n"
        f"STUDENT PROGRESS: {progress_note or 'just getting started'}\n\n"
        f"Greet the student warmly, say what you'll help them master, and ask ONE "
        f"question to gauge their current level with {skill}."
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=_TUTOR_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=300,
            temperature=0.6,
        )
        msg = (raw or "").strip()
        if msg:
            return msg[:2000]
    except Exception as e:  # noqa: BLE001
        print(f"[skills] tutor opener LLM failed: {e}")
    return (
        f"Hi! I'm your {skill} tutor. We'll go from the fundamentals to fluency, "
        f"one step at a time. To start: how would you rate your current experience "
        f"with {skill}, and what do you most want to be able to do with it?"
    )


def tutor_reply(
    skill: str,
    profile: Dict[str, Any],
    prior_turns: List[Any],
    student_message: str,
    progress_note: str = "",
) -> str:
    """Next tutor message given the transcript + the student's latest message."""
    lines = []
    for t in (prior_turns or [])[-12:]:
        who = "TUTOR" if getattr(t, "role", "") == "tutor" else "STUDENT"
        lines.append(f"{who}: {getattr(t, 'content', '')}")
    transcript = "\n".join(lines) if lines else "(no prior turns)"
    user_prompt = (
        f"SKILL: {skill}\n"
        f"STUDENT PROGRESS: {progress_note or 'in progress'}\n\n"
        f"TRANSCRIPT SO FAR:\n{transcript}\n\n"
        f"STUDENT'S LATEST MESSAGE:\n{student_message or '(no message)'}\n\n"
        f"Reply as the tutor — teach the next step."
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=_TUTOR_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=400,
            temperature=0.6,
        )
        msg = (raw or "").strip()
        if msg:
            return msg[:2000]
    except Exception as e:  # noqa: BLE001
        print(f"[skills] tutor reply LLM failed: {e}")
    return (
        "The AI tutor is unavailable right now (check the API key). In the "
        f"meantime, keep working through the {skill} learning path and practice "
        "above — try the next drill and note any questions to ask when it's back."
    )


def tutor_available(profile: Dict[str, Any]) -> bool:
    """Cheap probe: is the LLM reachable? Used to show a degraded-mode banner."""
    try:
        raw = llm.complete_with_cached_profile(
            profile={}, instructions="Reply with the single word: ok",
            user_prompt="ping", model=DEFAULT_MATCH_MODEL, max_tokens=5, temperature=0.0,
        )
        return bool((raw or "").strip())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Resume loop — suggest edits when a skill reaches fluency
# ---------------------------------------------------------------------------

_RESUME_SUGGEST_INSTRUCTIONS = """\
You help a candidate add a newly-mastered skill to their resume. Propose:
1. A skills-section entry (name + category).
2. 1-2 polished resume bullet points demonstrating the skill, in the candidate's
   voice, consistent with their existing resume style.

CRITICAL: Do NOT fabricate specific employers, dates, or invented metrics. Frame
bullets generically, e.g. "Applied {skill} to build ..." or "Developed ... using
{skill}". Polish only — no made-up numbers.

Return STRICT JSON, no markdown, no code fence:
{
  "skill_entry": {"name": "<skill>", "category": "<one of: Languages, Frameworks, Tools, Data, Cloud, Other>"},
  "bullets": ["...", "..."]
}
"""


def suggest_resume_edits(skill: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Propose a skill entry + polished bullets. Deterministic fallback on failure."""
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile,
            instructions=_RESUME_SUGGEST_INSTRUCTIONS,
            user_prompt=f"SKILL NOW FLUENT: {skill}\n\nPropose the edits. Output JSON only.",
            model=DEFAULT_TAILOR_MODEL,
            max_tokens=500,
            temperature=0.5,
        )
        data = _parse_json(raw)
        if data:
            norm = _normalize_resume_suggestion(data, skill)
            if norm["bullets"]:
                return norm
    except Exception as e:  # noqa: BLE001
        print(f"[skills] resume suggestion LLM failed: {e}")
    return _fallback_resume_suggestion(skill)


def _normalize_resume_suggestion(data: Dict[str, Any], skill: str) -> Dict[str, Any]:
    entry = data.get("skill_entry") or {}
    name = str(entry.get("name") or skill)[:80] if isinstance(entry, dict) else skill
    category = str(entry.get("category") or "Other")[:40] if isinstance(entry, dict) else "Other"
    bullets = [str(b)[:300] for b in (data.get("bullets") or []) if str(b).strip()][:3]
    return {
        "skill_entry": {"name": name, "category": category},
        "bullets": bullets,
        "generated_by": "ai",
    }


def _fallback_resume_suggestion(skill: str) -> Dict[str, Any]:
    return {
        "skill_entry": {"name": skill, "category": "Other"},
        "bullets": [
            f"Applied {skill} to design and build working projects, from fundamentals to production-ready features.",
            f"Deepened expertise in {skill} through hands-on practice and self-directed study to fluency.",
        ],
        "generated_by": "fallback",
    }
