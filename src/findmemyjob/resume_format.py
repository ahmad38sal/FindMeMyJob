"""Deterministic resume-formatting rules (pure, no DB/LLM/IO).

The tailoring LLM is *asked* to follow resume best practices (see the prompt in
``tailoring.py``), but LLMs drift and the heuristic fallback path doesn't ask an
LLM at all. So these pure helpers ENFORCE the rules on the tailored ``content``
dict after generation — and are reused by ``scripts/reformat_resumes.py`` to
clean existing rows without any LLM call.

Rules enforced:
  - Bullets per role capped by recency/relevance (recent role keeps more), with
    a hard cap of 6 and a floor of 2 (never fabricate to reach the floor).
  - When trimming, keep the bullets most relevant to the target job; drop the
    weakest. Original order among the kept bullets is preserved for readability.
  - Each bullet is tightened: weak openers stripped, whitespace collapsed, and
    never truncated mid-word (a too-long bullet is cut at a word boundary).
  - Skills become short, deduped, categorized tags. Sentence/requirement-style
    entries are reduced to a core noun-phrase tag or dropped.

Everything here is idempotent: running the pass twice yields the same result.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Bullets
# ---------------------------------------------------------------------------

BULLET_TARGET_LEN = 140
BULLET_HARD_MAX = 160

# Bullet caps by role position (index 0 = most recent / most relevant). Different
# page-length targets bias toward fewer/more bullets. Hard cap 6, floor 2.
_BULLET_CAPS = {
    "1": [4, 3, 3, 2],
    "2": [6, 5, 4, 3],
    "auto": [6, 4, 3, 3],
}
_BULLET_CAP_TAIL = {"1": 2, "2": 3, "auto": 3}

_WEAK_OPENERS = [
    "was responsible for", "responsible for", "worked on", "helped with",
    "helped to", "help with", "tasked with", "duties included", "duties include",
    "in charge of", "assisted with", "assisted in", "involved in",
]

# Trailing tokens that shouldn't be left dangling after a word-boundary trim.
_TRAILING_DROP = {
    "and", "or", "the", "a", "an", "to", "of", "for", "with", "in", "on", "by",
    "at", "as", "that", "which", "into", "from", "via",
}


def _clean_ws(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def bullet_cap(index: int, page_length: str = "auto") -> int:
    """Max bullets kept for the role at *index* under a page-length target."""
    key = page_length if page_length in _BULLET_CAPS else "auto"
    caps = _BULLET_CAPS[key]
    return caps[index] if index < len(caps) else _BULLET_CAP_TAIL[key]


def _strip_weak_opener(s: str) -> str:
    low = s.lower()
    for opener in _WEAK_OPENERS:
        if low.startswith(opener):
            rest = s[len(opener):].lstrip(" :,-")
            return rest or s
    return s


def tighten_bullet(text: Any, hard_max: int = BULLET_HARD_MAX) -> str:
    """Clean one bullet: strip weak opener, collapse ws, capitalize, cap length.

    Never truncates mid-word — if the bullet is still over ``hard_max`` after
    cleanup, it's cut at the last word boundary and trailing connectives are
    dropped so the result reads as a complete (if shorter) phrase.
    """
    s = _clean_ws(text)
    if not s:
        return ""
    s = _strip_weak_opener(s)
    s = s[0].upper() + s[1:]
    if len(s) <= hard_max:
        return s
    cut = s[:hard_max]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip(" ,;:.-–—")
    words = cut.split()
    while words and words[-1].lower().strip(".,;:") in _TRAILING_DROP:
        words.pop()
    return " ".join(words)


_STOPWORDS = {
    "the", "and", "for", "with", "you", "our", "are", "will", "have", "has",
    "this", "that", "your", "who", "all", "can", "from", "not", "but", "job",
    "role", "work", "team", "years", "year", "experience", "including", "such",
    "help", "using", "into", "per", "etc", "able", "ability", "strong",
}


def job_keyword_set(job_text: str) -> set:
    """Lowercased content tokens from the job text, for relevance ranking."""
    toks = re.findall(r"[a-z0-9+#\.]{3,}", (job_text or "").lower())
    return {t for t in toks if t not in _STOPWORDS}


def _bullet_score(bullet: str, keywords: set) -> int:
    low = bullet.lower()
    toks = set(re.findall(r"[a-z0-9+#\.]{3,}", low))
    overlap = len(toks & keywords)
    has_metric = 1 if re.search(r"\d", bullet) else 0
    return overlap * 2 + has_metric


def select_bullets(bullets: List[str], keywords: set, cap: int) -> List[str]:
    """Keep the *cap* most job-relevant bullets, in their original order."""
    clean = [b for b in bullets if isinstance(b, str) and b.strip()]
    if len(clean) <= cap:
        return clean
    ranked = sorted(range(len(clean)), key=lambda i: (-_bullet_score(clean[i], keywords), i))
    keep = set(ranked[:cap])
    return [clean[i] for i in range(len(clean)) if i in keep]


# ---------------------------------------------------------------------------
# Skills → short, categorized, deduped tags
# ---------------------------------------------------------------------------

# Canonical display forms for common surface variants (merged synonyms).
_CANON = {
    "react.js": "React", "reactjs": "React", "react": "React",
    "vue.js": "Vue", "vuejs": "Vue", "vue": "Vue",
    "node": "Node.js", "nodejs": "Node.js", "node.js": "Node.js",
    "js": "JavaScript", "javascript": "JavaScript",
    "ts": "TypeScript", "typescript": "TypeScript",
    "golang": "Go", "go": "Go", "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "gcp": "GCP", "google cloud": "GCP", "google cloud platform": "GCP",
    "aws": "AWS", "amazon web services": "AWS",
    "ci/cd": "CI/CD", "cicd": "CI/CD",
    "rest": "REST APIs", "restful": "REST APIs", "rest apis": "REST APIs",
    "power bi": "Power BI", "powerbi": "Power BI",
    "meta ads": "Meta Ads", "facebook ads": "Meta Ads",
    "tiktok": "TikTok", "tiktok ads": "TikTok Ads", "linkedin": "LinkedIn",
    "google ads": "Google Ads", "dtc": "DTC",
    "performance marketing": "Performance Marketing",
    "dtc creative strategy": "DTC Creative Strategy",
}

_ACRONYMS = {
    "DTC", "CTA", "SEO", "SEM", "PPC", "ROI", "ROAS", "KPI", "B2B", "B2C",
    "CRM", "AI", "ML", "NLP", "API", "UI", "UX", "AWS", "GCP", "SQL", "CSS",
    "HTML", "DR", "SaaS", "QA", "ETL", "LLM", "LLMs",
}

_CATEGORY_SETS = {
    "Languages": {
        "python", "javascript", "typescript", "java", "go", "rust", "c++",
        "c#", "ruby", "php", "swift", "kotlin", "scala", "sql", "bash", "r",
        "objective-c",
    },
    "Frameworks & Libraries": {
        "react", "vue", "angular", "node.js", "django", "flask", "fastapi",
        "spring", "rails", ".net", "next.js", "graphql", "rest apis", "express",
        "svelte", "pandas", "numpy", "pytorch", "tensorflow",
    },
    "Cloud & DevOps": {
        "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
        "ci/cd", "jenkins", "linux", "kafka", "redis", "serverless",
        "microservices", "observability", "devops",
    },
    "Tools & Platforms": {
        "git", "jira", "tableau", "power bi", "excel", "salesforce", "hubspot",
        "snowflake", "bigquery", "redshift", "airflow", "dbt", "looker",
        "segment", "google analytics", "ga4", "mixpanel", "amplitude", "notion",
    },
    "Design": {
        "figma", "sketch", "photoshop", "illustrator", "adobe", "ui design",
        "ux design", "wireframing", "prototyping", "indesign", "after effects",
    },
    "Marketing/Domain": {
        "seo", "sem", "ppc", "google ads", "meta ads", "performance marketing",
        "dtc creative strategy", "direct-response marketing", "email marketing",
        "content marketing", "brand strategy", "growth marketing", "crm",
        "copywriting", "paid social", "paid media", "conversion rate optimization",
        "a/b testing", "media buying", "creative strategy", "dtc", "roas",
    },
}

# Legacy free-text category names -> our canonical category buckets.
_LEGACY_CATEGORY = {
    "language": "Languages", "languages": "Languages",
    "framework": "Frameworks & Libraries", "frameworks": "Frameworks & Libraries",
    "library": "Frameworks & Libraries", "libraries": "Frameworks & Libraries",
    "tool": "Tools & Platforms", "tools": "Tools & Platforms",
    "platform": "Tools & Platforms", "platforms": "Tools & Platforms",
    "data": "Tools & Platforms", "cloud": "Cloud & DevOps",
    "devops": "Cloud & DevOps", "infrastructure": "Cloud & DevOps",
    "design": "Design", "marketing": "Marketing/Domain", "domain": "Marketing/Domain",
}

# Requirement/sentence markers: an entry containing these is not a clean tag.
_SENTENCE_MARKERS = (
    "experience", "years", "ability to", "proven", "demonstrated",
    "knowledge of", "proficiency", "proficient", "familiarity", "understanding of",
    "track record", "strong ", "expertise", "hands-on", "hands on", "background in",
)

_OK_LEADING_ING = {"marketing", "advertising", "engineering", "consulting"}


def _display_tag(tag: str) -> str:
    t = _clean_ws(tag).strip(".,;:")
    if not t:
        return ""
    low = t.lower()
    if low in _CANON:
        return _CANON[low]
    words = []
    for w in t.split():
        core = w.strip(".,")
        if core.upper() in _ACRONYMS:
            words.append(core.upper())
        elif w.isupper() and w.isalpha() and len(w) <= 4:
            words.append(w)
        elif any(ch in w for ch in ("+", "#", ".", "/")):
            words.append(w)  # keep tokens like c++, node.js, ci/cd as-is
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return " ".join(words)


def categorize_skill(tag: str) -> str:
    """Bucket a clean skill tag into a resume category ("Other" as last resort)."""
    low = _clean_ws(tag).lower()
    for cat, members in _CATEGORY_SETS.items():
        if low in members:
            return cat
    # Substring heuristics for domain phrases we don't enumerate exhaustively.
    if any(k in low for k in ("marketing", "creative", "ads", "seo", "sem", "brand", "campaign", "copywriting", "media buying")):
        return "Marketing/Domain"
    if any(k in low for k in ("design", "figma", "adobe", "prototyp", "wireframe")):
        return "Design"
    if any(k in low for k in ("aws", "cloud", "docker", "kubernetes", "devops", "ci/cd")):
        return "Cloud & DevOps"
    return "Other"


def _is_sentence_like(name: str) -> bool:
    words = name.split()
    if len(words) > 5:
        return True
    low = name.lower()
    if any(m in low for m in _SENTENCE_MARKERS):
        return True
    if "(" in name and "," in name:  # parenthetical enumerations
        return True
    return False


_REQ_PREFIX_RES = [
    re.compile(r"^\s*\d+\s*[-–]?\s*\d*\+?\s*years?\s+of\s+experience\s+(?:in|with|building|using|of)?\s*", re.I),
    re.compile(r"^\s*(?:experience|expertise|proficiency|proficient|familiarity|knowledge|ability|strong|proven|demonstrated|understanding|hands[- ]on|background)\s+(?:in|with|of|to|building|using)?\s*", re.I),
]


def _extract_tags_from_sentence(sentence: str) -> List[str]:
    s = re.sub(r"\([^)]*\)", "", _clean_ws(sentence))  # drop parentheticals
    for rx in _REQ_PREFIX_RES:
        s = rx.sub("", s)
    parts = re.split(r"\s*(?:,| or | and |/)\s*", s, flags=re.I)
    tags: List[str] = []
    for p in parts:
        p = _clean_ws(p).strip(".")
        if not p:
            continue
        pw = p.split()
        if len(pw) > 4:
            continue  # still a phrase, not a tag -> drop
        first = pw[0].lower()
        if first.endswith("ing") and first not in _OK_LEADING_ING:
            continue  # verb-ing lead -> not a noun-phrase skill
        tag = _display_tag(p)
        if tag:
            tags.append(tag)
    return tags


def normalize_skills(skills: Any, max_tags: int = 16) -> List[Dict[str, str]]:
    """Turn a messy skills list into short, deduped, categorized tag dicts."""
    out: List[Dict[str, str]] = []
    seen = set()
    for entry in skills or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            legacy_cat = str(entry.get("category") or "").strip()
        else:
            name = str(entry or "").strip()
            legacy_cat = ""
        if not name:
            continue
        candidates = (
            _extract_tags_from_sentence(name) if _is_sentence_like(name)
            else [_display_tag(name)]
        )
        for tag in candidates:
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            cat = categorize_skill(tag)
            if cat == "Other" and legacy_cat and legacy_cat.lower() != "other":
                cat = _LEGACY_CATEGORY.get(legacy_cat.lower(), cat)
            out.append({"name": tag, "category": cat})
            if len(out) >= max_tags:
                return out
    return out


# ---------------------------------------------------------------------------
# Top-level pass
# ---------------------------------------------------------------------------

def format_resume_content(
    content: Dict[str, Any], *, job_text: str = "", page_length: str = "auto"
) -> Dict[str, Any]:
    """Enforce the formatting rules on a tailored resume ``content`` dict.

    Pure and idempotent. Only ``work_history`` bullets and ``skills`` are
    rewritten; summary/education/keywords pass through untouched.
    """
    out = dict(content or {})
    keywords = job_keyword_set(job_text)

    new_wh: List[Dict[str, Any]] = []
    for i, role in enumerate(out.get("work_history") or []):
        if not isinstance(role, dict):
            continue
        role = dict(role)
        bullets = role.get("bullets") or []
        cap = bullet_cap(i, page_length)
        kept = select_bullets(bullets, keywords, cap)
        role["bullets"] = [b for b in (tighten_bullet(x) for x in kept) if b]
        new_wh.append(role)
    out["work_history"] = new_wh

    out["skills"] = normalize_skills(out.get("skills"))
    return out
