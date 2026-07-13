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

import hashlib
import json
import re
from typing import Any, Dict, List


def resume_content_hash(content: Any) -> str:
    """Stable sha256 of a resume ``content`` dict, for cached-PDF invalidation.

    Canonicalizes to sorted-key, whitespace-free JSON so the same logical content
    always hashes identically regardless of key order or serialization. ``str``
    fallback covers dates/other non-JSON scalars.
    """
    canon = json.dumps(content or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Work-history ordering (reverse-chronological)
# ---------------------------------------------------------------------------

# An empty/absent end date, or these words, means the role is ongoing. Includes
# stringified-null artifacts ("none"/"null") from an earlier code path that
# str()'d Python None into the JSON content.
_ONGOING_END = {
    "", "none", "null", "n/a", "na", "-",
    "present", "current", "now", "ongoing", "presently",
}

# Date values that are really "no value" — normalized back to real None so the
# stored content is clean and the template never renders the word "None".
_NULLISH_DATE = {"", "none", "null", "n/a", "na", "-"}


def _clean_date_field(value: Any) -> Any:
    """Rewrite a stringified-null / sentinel date to real None; else pass through."""
    if value is None:
        return None
    if str(value).strip().lower() in _NULLISH_DATE:
        return None
    return value

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _is_ongoing(end: Any) -> bool:
    """True when a role has no end date (still ongoing)."""
    if end is None:
        return True
    return str(end).strip().lower() in _ONGOING_END


def _month_ordinal(value: Any) -> int | None:
    """Parse a resume date string to a comparable ``year*12 + month`` ordinal.

    Handles ``YYYY-MM``, ``YYYY/MM``, ``MM/YYYY``, ``YYYY``, and
    ``Mon YYYY`` / ``Month YYYY``. Returns None when unparseable (sorts oldest).
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in _ONGOING_END:
        return None

    # Month-name form: "Jan 2024", "March 2019".
    m = re.match(r"^([a-z]+)\.?\s+(\d{4})$", s)
    if m and m.group(1) in _MONTHS:
        return int(m.group(2)) * 12 + _MONTHS[m.group(1)]

    # Numeric forms with a separator.
    m = re.match(r"^(\d{1,4})[-/](\d{1,4})$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        # Decide which side is the year (4-digit or the larger value).
        if a >= 1000:            # YYYY-MM
            year, month = a, b
        elif b >= 1000:          # MM/YYYY
            year, month = b, a
        else:                    # ambiguous 2-digit/2-digit — treat first as year
            year, month = a, b
        month = month if 1 <= month <= 12 else 0
        return year * 12 + month

    # Bare year.
    m = re.match(r"^(\d{4})$", s)
    if m:
        return int(m.group(1)) * 12

    return None


def order_work_history(work_history: Any) -> List[Any]:
    """Return roles sorted strictly reverse-chronological (pure, stable, idempotent).

    Order:
      1. Ongoing roles (no end date) first, by START date descending.
      2. Then ended roles by END date descending; tie-break START date descending.
      3. Identical keys preserve original relative order (stable).

    Non-dict entries and unparseable dates are tolerated (sorted oldest, never
    dropped). No role fields are mutated — only the list order changes.
    """
    if not isinstance(work_history, list):
        return work_history

    _OLD = -1  # ordinal sentinel for a missing/unparseable date (sorts oldest)

    def sort_key(pair):
        idx, role = pair
        if not isinstance(role, dict):
            # Non-dict rows sink to the very end but keep their relative order.
            return (2, 0, 0, idx)
        ongoing = _is_ongoing(role.get("end"))
        start = _month_ordinal(role.get("start"))
        start = start if start is not None else _OLD
        if ongoing:
            return (0, -start, 0, idx)
        end = _month_ordinal(role.get("end"))
        end = end if end is not None else _OLD
        return (1, -end, -start, idx)

    return [role for _, role in sorted(enumerate(work_history), key=sort_key)]


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

# Trailing tokens that shouldn't be left dangling after a trim.
_TRAILING_DROP = {
    "and", "or", "the", "a", "an", "to", "of", "for", "with", "in", "on", "by",
    "at", "as", "that", "which", "into", "from", "via", "&",
}

# Natural clause boundaries: sentence end, comma/semicolon, dash, or "and"/"&".
# Kept as a capturing split so we can rebuild only the clauses that fit.
_CLAUSE_SPLIT_RE = re.compile(r"(\s*(?:[;,]|—|–|\band\b|&|\.)\s+)", re.I)


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


def _drop_dangling(s: str) -> str:
    """Strip trailing punctuation and any dangling connective words."""
    s = s.rstrip(" ,;:.-–—&")
    words = s.split()
    while words and words[-1].lower().strip(".,;:&") in _TRAILING_DROP:
        words.pop()
    return " ".join(words)


def _trim_to_clauses(s: str, hard_max: int) -> str:
    """Keep as many leading whole clauses as fit under *hard_max*.

    Splits on natural boundaries (sentence end, comma/semicolon, dash, "and")
    and greedily rebuilds from the front, so the result always ends on a
    complete clause — never a partial word or a dangling connective. Returns
    "" when the very first clause already exceeds the limit (no safe boundary).
    """
    parts = _CLAUSE_SPLIT_RE.split(s)
    if len(parts) <= 1:
        return ""
    # parts alternates: clause, delimiter, clause, delimiter, ..., clause.
    acc = ""       # confirmed-kept text incl. trailing delimiter for the next join
    result = ""    # confirmed-kept text without a trailing delimiter
    for i in range(0, len(parts), 2):
        clause = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        candidate = acc + clause
        if candidate.strip() and len(candidate) <= hard_max:
            result = candidate
            acc = candidate + delim
        else:
            break
    return result


def tighten_bullet(text: Any, hard_max: int = BULLET_HARD_MAX) -> str:
    """Clean one bullet: strip weak opener, collapse ws, capitalize, cap length.

    Never truncates mid-word. If the bullet is over ``hard_max`` after cleanup,
    whole trailing clauses are dropped at natural boundaries so it ends as a
    complete thought. Only if a single clause is itself too long do we fall back
    to a word-boundary cut, dropping any dangling connective.
    """
    s = _clean_ws(text)
    if not s:
        return ""
    s = _strip_weak_opener(s)
    s = s[0].upper() + s[1:]
    if len(s) <= hard_max:
        return s
    trimmed = _trim_to_clauses(s, hard_max)
    if trimmed:
        return _drop_dangling(trimmed)
    # Fallback: no clause boundary before the limit — cut at last whole word.
    cut = s[:hard_max]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return _drop_dangling(cut)


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

# Canonical display forms for common surface variants (merged synonyms). This is
# also where proper-noun tools get their exact internal capitalization, so we
# never naively title-case a name like CapCut -> "Capcut".
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
    # Proper-noun tools (correct internal capitalization).
    "capcut": "CapCut", "github": "GitHub", "gitlab": "GitLab",
    "davinci resolve": "DaVinci Resolve", "davinci": "DaVinci Resolve",
    "premiere pro": "Premiere Pro", "premiere": "Premiere Pro",
    "after effects": "After Effects", "photoshop": "Photoshop",
    "illustrator": "Illustrator", "indesign": "InDesign",
    "figma": "Figma", "sketch": "Sketch",
    "google analytics": "Google Analytics", "youtube": "YouTube",
    "wordpress": "WordPress", "mysql": "MySQL", "mongodb": "MongoDB",
    "dynamodb": "DynamoDB", "graphql": "GraphQL", "openai": "OpenAI",
    "chatgpt": "ChatGPT", "nextjs": "Next.js", "next.js": "Next.js",
    "active directory": "Active Directory", "cisco meraki": "Cisco Meraki",
    "jfrog artifactory": "JFrog Artifactory", "jfrog": "JFrog",
    "artifactory": "Artifactory", "siem": "SIEM",
    # Compound / phrase skills (kept intact, canonicalized).
    "ux/ui": "UX/UI", "ui/ux": "UX/UI",
    "human-centered design": "Human-Centered Design",
    "human centered design": "Human-Centered Design",
    "design system": "Design Systems", "design systems": "Design Systems",
    "a/b testing": "A/B Testing",
}

# Compound tokens whose internal "/" must NOT be treated as a split boundary.
_COMPOUND_TOKENS = ("ux/ui", "ui/ux", "ci/cd", "a/b", "tcp/ip", "i/o")

# Filler words removed from an extracted skill phrase before matching a tag.
_FILLER_WORDS = {
    "deep", "strong", "solid", "proven", "demonstrated", "advanced", "excellent",
    "good", "great", "extensive", "modern", "hands-on", "expertise", "expert",
    "knowledge", "proficiency", "proficient", "understanding", "familiarity",
    "experience", "experienced", "collaboration", "collaborating", "skills",
    "skill", "ability", "component", "components", "architecture", "translation",
    "based", "level", "years", "year", "cross-functional", "end-to-end",
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
        "git", "github", "gitlab", "jira", "tableau", "power bi", "excel",
        "salesforce", "hubspot", "snowflake", "bigquery", "redshift", "airflow",
        "dbt", "looker", "segment", "google analytics", "ga4", "mixpanel",
        "amplitude", "notion", "wordpress", "active directory", "cisco meraki",
        "jfrog artifactory", "artifactory", "servicenow", "splunk", "confluence",
    },
    "Design": {
        "figma", "sketch", "photoshop", "illustrator", "adobe", "ui design",
        "ux design", "wireframing", "prototyping", "indesign", "after effects",
        "ux/ui", "human-centered design", "design systems", "premiere pro",
        "davinci resolve", "capcut",
    },
    "Security": {
        "siem", "vulnerability remediation", "vulnerability management",
        "change management", "incident response", "penetration testing",
        "threat detection", "endpoint security", "network security",
        "identity management", "iam", "firewalls", "edr", "soc",
    },
    "Marketing/Domain": {
        "seo", "sem", "ppc", "google ads", "meta ads", "performance marketing",
        "dtc creative strategy", "direct-response marketing", "email marketing",
        "content marketing", "brand strategy", "growth marketing", "crm",
        "copywriting", "paid social", "paid media", "conversion rate optimization",
        "a/b testing", "media buying", "creative strategy", "dtc", "roas",
        "tiktok", "tiktok ads", "linkedin", "youtube",
    },
}

# The canonical bucket names we emit. Used so re-running the pass PRESERVES an
# already-assigned bucket instead of trying (and failing) to re-map it — the
# root of the old non-idempotency bug.
_CANON_CATEGORIES = set(_CATEGORY_SETS) | {"Other"}

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

# All enumerated skills across categories, flattened for a "is this a known
# skill?" membership test (used to salvage tags from noisy phrases).
_KNOWN_SKILLS = set().union(*_CATEGORY_SETS.values()) | set(_CANON)


def _is_known_skill(low: str) -> bool:
    return low in _CANON or low in _KNOWN_SKILLS


def _protect_compounds(s: str):
    """Mask the "/" inside known compound tokens so the splitter won't break them."""
    mapping: Dict[str, str] = {}
    for i, comp in enumerate(_COMPOUND_TOKENS):
        rx = re.compile(re.escape(comp), re.I)
        if rx.search(s):
            placeholder = f"\x00{i}\x00"
            s = rx.sub(placeholder, s)
            mapping[placeholder] = comp
    return s, mapping


def _restore(s: str, mapping: Dict[str, str]) -> str:
    for ph, comp in mapping.items():
        s = s.replace(ph, comp)
    return s


def _phrase_to_tag(phrase: str) -> str:
    """Reduce one extracted phrase to a clean canonical tag, or "" to drop it."""
    p = _clean_ws(phrase).strip(".")
    if not p:
        return ""
    if p.lower() in _CANON:
        return _CANON[p.lower()]
    words = [w for w in p.split() if w.lower().strip(".,") not in _FILLER_WORDS]
    if not words:
        return ""
    cleaned = " ".join(words)
    low = cleaned.lower()
    if low in _CANON:
        return _CANON[low]
    if _is_known_skill(low):
        return _display_tag(cleaned)
    first = words[0].lower()
    if first.endswith("ing") and first not in _OK_LEADING_ING:
        return ""  # verb-ing lead -> not a noun-phrase skill
    # Salvage a known tool/skill token embedded in a longer phrase.
    for w in words:
        wl = w.lower().strip(".,")
        if wl in _CANON:
            return _CANON[wl]
        if _is_known_skill(wl):
            return _display_tag(w)
    if len(words) <= 3:
        return _display_tag(cleaned)
    return ""  # still a phrase, not a tag -> drop


def _extract_known_from_fragment(fragment: str) -> List[str]:
    """From a parenthetical, keep only tokens that are already known skills."""
    out: List[str] = []
    for p in re.split(r"\s*(?:,| or | and | to |/)\s*", fragment, flags=re.I):
        p = _clean_ws(p).strip(".")
        if p and _is_known_skill(p.lower()):
            out.append(_display_tag(p))
    return out


def _extract_tags_from_sentence(sentence: str) -> List[str]:
    text = _clean_ws(sentence)
    # Parentheticals: keep only recognizable skills (e.g. "(Figma to React)"),
    # discard noise enumerations (e.g. "(hook, problem, mechanism, proof, CTA)").
    paren_tags: List[str] = []
    for frag in re.findall(r"\(([^)]*)\)", text):
        paren_tags.extend(_extract_known_from_fragment(frag))
    text = re.sub(r"\([^)]*\)", "", text)
    for rx in _REQ_PREFIX_RES:
        text = rx.sub("", text)
    protected, mapping = _protect_compounds(text)
    tags: List[str] = []
    for part in re.split(r"\s*(?:,| or | and |/)\s*", protected, flags=re.I):
        tag = _phrase_to_tag(_restore(part, mapping))
        if tag:
            tags.append(tag)
    tags.extend(paren_tags)
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
            else [_phrase_to_tag(name)]
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
                # Preserve a bucket we already assigned on a prior pass (keeps the
                # pass idempotent); otherwise map a legacy free-text category name.
                if legacy_cat in _CANON_CATEGORIES:
                    cat = legacy_cat
                else:
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

    # Reverse-chronological order first, so per-index bullet caps follow recency.
    ordered_wh = order_work_history(out.get("work_history") or [])

    new_wh: List[Dict[str, Any]] = []
    for i, role in enumerate(ordered_wh):
        if not isinstance(role, dict):
            continue
        role = dict(role)
        # Clean stringified-null date artifacts so data is correct going forward
        # and the template renders ongoing roles as "Present" (never "None").
        if "start" in role:
            role["start"] = _clean_date_field(role.get("start"))
        if "end" in role:
            role["end"] = _clean_date_field(role.get("end"))
        bullets = role.get("bullets") or []
        cap = bullet_cap(i, page_length)
        kept = select_bullets(bullets, keywords, cap)
        role["bullets"] = [b for b in (tighten_bullet(x) for x in kept) if b]
        new_wh.append(role)
    out["work_history"] = new_wh

    out["skills"] = normalize_skills(out.get("skills"))
    return out
