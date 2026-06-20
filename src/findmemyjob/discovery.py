"""Discovery engine: 'perfect-fit jobs available right now'.

Pipeline (one callable: ``run_discovery``):
  1. Derive (or load) a SearchProfile from the user's Profile via the LLM.
  2. Source live postings — broad keyword feeds (RemoteOK / Remotive / HN) AND
     target-company board crawls (Greenhouse / Lever / Ashby).
  3. Merge + dedupe (by source+source_id, then by URL, then title+company).
  4. Upsert into Job via the ORM (DB assigns ids — no manual id juggling).
  5. Freshness filter (default 14 days); undated jobs are flagged, not dropped.
  6. Blended fit ranking = LLM skill-match + preference alignment.
  7. Record the NEW top matches in a DiscoveryRun row (what cron reports).

Everything is defensive: a failing source is logged and skipped, the LLM
derivation falls back to a deterministic heuristic, and bad scores never raise.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from findmemyjob.llm import DEFAULT_MATCH_MODEL, _strip_code_fence, llm
from findmemyjob.matching import ScoreResult, prefilter, score_jobs_bulk
from findmemyjob.models import DiscoveryRun, Job, Profile, SearchProfile

# ---------------------------------------------------------------------------
# 1. Search-profile derivation
# ---------------------------------------------------------------------------

_DERIVE_INSTRUCTIONS = """\
You derive an "ideal-role search profile" from a candidate's resume profile, so
a job-discovery engine can find roles that fit them RIGHT NOW.

Infer everything from the profile — do NOT use a fixed list of role categories.
Read their work history, skills, education and preferences, then output the
roles and keywords THEY would realistically land or stretch into.

Guidance:
  - titles: 3-8 concrete job titles to search for (what appears in postings,
    e.g. "Senior Backend Engineer", "ML Platform Engineer"). Order by fit.
  - keywords: 5-15 short search tokens / tech terms / skills (e.g. "python",
    "kubernetes", "react", "data pipeline"). These feed keyword feeds.
  - seniority: one of junior | mid | senior | staff | lead | principal |
    manager (best single guess from years of experience), or null if unclear.
  - remote_pref: remote | hybrid | onsite | any  (from preferences.work_modes;
    "any" if none stated).
  - locations: list of acceptable locations from preferences (may be empty).
  - salary_min / salary_target: integers in major currency units, from
    preferences if present else your best inference from seniority+market
    (null if you truly can't tell).
  - currency: ISO code (default USD).
  - summary: one sentence describing the ideal role.

If the profile is sparse/empty, still produce a sensible generic-but-honest
profile and say so in the summary.

Return STRICT JSON, no markdown, no commentary:
{
  "titles": [...],
  "keywords": [...],
  "seniority": "senior" | null,
  "remote_pref": "remote",
  "locations": [...],
  "salary_min": 0 | null,
  "salary_target": 0 | null,
  "currency": "USD",
  "summary": "..."
}
"""


def _heuristic_search_profile(profile_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic fallback when the LLM is unavailable or the profile is empty."""
    prefs = profile_dict.get("preferences") or {}
    work = profile_dict.get("work_history") or []
    skills = profile_dict.get("skills") or []

    titles: List[str] = []
    for w in work[:4]:
        t = (w.get("title") or "").strip()
        if t and t not in titles:
            titles.append(t)

    keywords: List[str] = []
    for s in skills[:15]:
        n = (s.get("name") or "").strip()
        if n and n.lower() not in {k.lower() for k in keywords}:
            keywords.append(n)
    if not keywords:
        # last resort: pull words off the most recent title
        keywords = [w for w in (titles[0].split() if titles else []) if len(w) > 2]

    modes = prefs.get("work_modes") or []
    remote_pref = "remote" if "remote" in modes else (modes[0] if modes else "any")

    return {
        "titles": titles,
        "keywords": keywords,
        "seniority": (prefs.get("seniority_levels") or [None])[0],
        "remote_pref": remote_pref,
        "locations": prefs.get("locations") or [],
        "salary_min": prefs.get("salary_min"),
        "salary_target": prefs.get("salary_target"),
        "currency": prefs.get("currency") or "USD",
        "summary": "Heuristic profile (LLM unavailable or sparse profile).",
    }


def _coerce_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and str(v).strip() not in ("", "null") else None
    except (ValueError, TypeError):
        return None


def derive_search_profile(profile_dict: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-derive the ideal-role search profile; fall back to a heuristic."""
    # Empty profile → heuristic immediately (don't waste an LLM call).
    if not (profile_dict.get("work_history") or profile_dict.get("skills")):
        return _heuristic_search_profile(profile_dict)

    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_DERIVE_INSTRUCTIONS,
            user_prompt="Derive the ideal-role search profile. Output JSON only.",
            model=DEFAULT_MATCH_MODEL,
            max_tokens=1500,
            temperature=0.3,
        )
        data = json.loads(_strip_code_fence(raw))
    except Exception as e:  # noqa: BLE001 - any LLM/parse failure → heuristic
        print(f"[discovery] derive failed, using heuristic: {type(e).__name__}: {e}")
        return _heuristic_search_profile(profile_dict)

    prefs = profile_dict.get("preferences") or {}
    titles = [t for t in (data.get("titles") or []) if isinstance(t, str) and t.strip()]
    keywords = [k for k in (data.get("keywords") or []) if isinstance(k, str) and k.strip()]
    if not titles and not keywords:
        return _heuristic_search_profile(profile_dict)

    return {
        "titles": titles[:8],
        "keywords": keywords[:15],
        "seniority": (data.get("seniority") or None),
        "remote_pref": (data.get("remote_pref") or "any"),
        "locations": data.get("locations") or prefs.get("locations") or [],
        # Prefer the user's own stated numbers over the LLM's inference.
        "salary_min": _coerce_int(prefs.get("salary_min")) or _coerce_int(data.get("salary_min")),
        "salary_target": _coerce_int(prefs.get("salary_target")) or _coerce_int(data.get("salary_target")),
        "currency": prefs.get("currency") or data.get("currency") or "USD",
        "summary": (data.get("summary") or "").strip(),
    }


def get_or_create_search_profile(
    session: Session, *, regenerate: bool = False
) -> SearchProfile:
    """Return the stored SearchProfile, deriving it if missing or forced."""
    sp = session.get(SearchProfile, 1)
    if sp is not None and not regenerate:
        return sp

    profile = session.get(Profile, 1)
    profile_dict = profile.model_dump() if profile else {}
    derived = derive_search_profile(profile_dict)

    if sp is None:
        sp = SearchProfile(id=1)
    sp.titles = derived["titles"]
    sp.keywords = derived["keywords"]
    sp.seniority = derived["seniority"]
    sp.remote_pref = derived["remote_pref"]
    sp.locations = derived["locations"]
    sp.salary_min = derived["salary_min"]
    sp.salary_target = derived["salary_target"]
    sp.currency = derived["currency"]
    sp.summary = derived["summary"]
    sp.raw = {"derived_from_profile_at": datetime.utcnow().isoformat(timespec="seconds")}
    sp.generated_at = datetime.utcnow()
    session.add(sp)
    session.commit()
    session.refresh(sp)
    return sp


# ---------------------------------------------------------------------------
# 2. Sourcing (broad feeds + target boards)
# ---------------------------------------------------------------------------

def _board_targets(prefs: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """Parse the user's target-company list into (greenhouse, lever, ashby) slugs.

    Entries are 'greenhouse:slug' / 'lever:slug' / 'ashby:org', reusing the
    existing `external_companies` pref plus a new `discovery_companies` pref.
    """
    gh: List[str] = []
    lv: List[str] = []
    ash: List[str] = []
    entries = list(prefs.get("external_companies") or []) + list(
        prefs.get("discovery_companies") or []
    )
    for entry in entries:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        src, slug = entry.split(":", 1)
        src, slug = src.strip().lower(), slug.strip().lower()
        if not slug:
            continue
        if src == "greenhouse":
            gh.append(slug)
        elif src == "lever":
            lv.append(slug)
        elif src == "ashby":
            ash.append(slug)
    return sorted(set(gh)), sorted(set(lv)), sorted(set(ash))


def source_jobs(
    search_profile: SearchProfile, prefs: Dict[str, Any]
) -> Tuple[List[Job], List[str]]:
    """Fetch from broad feeds + target boards. Returns (jobs, sources_used).

    Resilient: each source is independently guarded so one failure never
    aborts the run.
    """
    from findmemyjob.sources import remoteok as remoteok_src
    from findmemyjob.sources import remotive as remotive_src
    from findmemyjob.sources.ashby import AshbySource
    from findmemyjob.sources.greenhouse import GreenhouseSource
    from findmemyjob.sources.hn_whoishiring import HNWhoIsHiringSource
    from findmemyjob.sources.lever import LeverSource

    keywords = search_profile.keywords or []
    titles = search_profile.titles or []
    jobs: List[Job] = []
    used: List[str] = []

    def _guard(name: str, fn) -> None:
        try:
            fetched = fn() or []
            if fetched:
                jobs.extend(fetched)
            used.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[discovery] source {name} failed, skipping: {type(e).__name__}: {e}")

    # --- Broad keyword feeds (default on; respect explicit disables) ---
    if prefs.get("discovery_enable_remoteok", True):
        _guard("remoteok", lambda: remoteok_src.fetch_by_tags(keywords, limit=300))

    if prefs.get("discovery_enable_remotive", True):
        def _remotive():
            out: List[Job] = []
            terms = (titles[:3] or keywords[:3]) or [""]
            for term in terms:
                out.extend(remotive_src.fetch_search(term, limit=80))
            return out
        _guard("remotive", _remotive)

    if prefs.get("discovery_enable_hn"):  # off by default (1 LLM call per comment)
        _guard("hn-whoishiring", lambda: HNWhoIsHiringSource(
            limit=int(prefs.get("hn_limit") or 40)).fetch())

    # --- Target-company board crawls ---
    gh, lv, ash = _board_targets(prefs)
    if gh:
        _guard("greenhouse", lambda: GreenhouseSource(gh).fetch(limit=2000))
    if lv:
        _guard("lever", lambda: LeverSource(lv).fetch(limit=2000))
    if ash:
        _guard("ashby", lambda: AshbySource(ash).fetch(limit=2000))

    return jobs, used


# ---------------------------------------------------------------------------
# 3. Dedupe
# ---------------------------------------------------------------------------

def dedupe(jobs: List[Job]) -> List[Job]:
    """Drop duplicates within a batch by URL, then by (title, company)."""
    seen_url: set = set()
    seen_tc: set = set()
    out: List[Job] = []
    for j in jobs:
        url_key = (j.url or "").strip().lower().rstrip("/")
        tc_key = ((j.title or "").strip().lower(), (j.company or "").strip().lower())
        if url_key and url_key in seen_url:
            continue
        if tc_key in seen_tc and tc_key != ("", ""):
            continue
        if url_key:
            seen_url.add(url_key)
        seen_tc.add(tc_key)
        out.append(j)
    return out


# ---------------------------------------------------------------------------
# 4. Upsert (ORM assigns ids)
# ---------------------------------------------------------------------------

def upsert_jobs(session: Session, jobs: List[Job]) -> Tuple[List[Job], List[Job]]:
    """Insert new jobs, refresh metadata on existing. Returns (all_persisted, new_jobs).

    Dedup against the DB by (source, source_id) first, then by normalized URL.
    Never sets Job.id manually — the DB autoincrements, avoiding the Postgres
    sequence-desync UniqueViolation.
    """
    persisted: List[Job] = []
    new_jobs: List[Job] = []
    now = datetime.utcnow()
    for job in jobs:
        existing = session.exec(
            select(Job).where(Job.source == job.source).where(Job.source_id == job.source_id)
        ).first()
        if existing is None and job.url:
            url_norm = job.url.strip().rstrip("/")
            existing = session.exec(select(Job).where(Job.url == url_norm)).first()
            if existing is None and url_norm != job.url:
                existing = session.exec(select(Job).where(Job.url == job.url)).first()

        if existing is None:
            job.discovered_at = now
            if job.posted_at is None:
                job.undated = True
            session.add(job)
            session.commit()
            session.refresh(job)
            new_jobs.append(job)
            persisted.append(job)
        else:
            # Refresh light metadata; don't clobber an existing description/score.
            if job.description and len(job.description) > len(existing.description or ""):
                existing.description = job.description
            if job.posted_at and not existing.posted_at:
                existing.posted_at = job.posted_at
                existing.undated = False
            if existing.discovered_at is None:
                existing.discovered_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            persisted.append(existing)
    return persisted, new_jobs


# ---------------------------------------------------------------------------
# 5. Freshness
# ---------------------------------------------------------------------------

def freshness_partition(
    jobs: List[Job], *, max_age_days: int = 14
) -> Tuple[List[Job], List[Job]]:
    """Split into (fresh, stale). Undated jobs go into `fresh` (kept, flagged),
    so they're ranked — just lower — rather than silently dropped."""
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    fresh: List[Job] = []
    stale: List[Job] = []
    for j in jobs:
        if j.posted_at is None:
            fresh.append(j)  # undated — keep, flagged via job.undated
        elif j.posted_at >= cutoff:
            fresh.append(j)
        else:
            stale.append(j)
    return fresh, stale


# ---------------------------------------------------------------------------
# 6. Blended fit ranking
# ---------------------------------------------------------------------------

def preference_alignment(
    search_profile: SearchProfile, job: Job
) -> Tuple[float, List[str]]:
    """Deterministic preference score in [0,1] + human-readable notes.

    Blends: remote/location match and salary-vs-target. Generous when signal is
    missing (absence isn't a penalty).
    """
    notes: List[str] = []
    score = 0.0
    weight = 0.0

    # Remote / work-mode alignment (weight 0.5)
    weight += 0.5
    pref = (search_profile.remote_pref or "any").lower()
    jm = (job.work_mode or "").lower()
    if pref == "any" or not jm:
        score += 0.5 * 0.7  # neutral-positive
        notes.append("work-mode: no hard preference")
    elif pref == jm:
        score += 0.5
        notes.append(f"work-mode match ({jm})")
    else:
        notes.append(f"work-mode mismatch (want {pref}, job {jm})")

    # Salary alignment (weight 0.5)
    weight += 0.5
    target = search_profile.salary_target or search_profile.salary_min
    if not target or not job.salary_max:
        score += 0.5 * 0.7  # unknown salary — don't punish
        notes.append("salary: not enough data")
    elif job.salary_max >= target:
        score += 0.5
        notes.append(f"salary at/above target ({job.salary_max} ≥ {target})")
    elif search_profile.salary_min and job.salary_max >= search_profile.salary_min:
        score += 0.5 * 0.6
        notes.append("salary within range but below target")
    else:
        notes.append(f"salary below floor ({job.salary_max} < {target})")

    return (score / weight if weight else 0.7), notes


def blend(skill_score: float, pref_score_0_1: float, undated: bool) -> float:
    """Blend LLM skill-match (0-100) with preference alignment (0-1).

    70% skill / 30% preference. Undated jobs get a small freshness penalty so
    dated, equally-good jobs rank above them.
    """
    blended = 0.70 * skill_score + 0.30 * (pref_score_0_1 * 100.0)
    if undated:
        blended *= 0.95
    return round(max(0.0, min(100.0, blended)), 1)


async def rank_jobs(
    profile_dict: Dict[str, Any],
    search_profile: SearchProfile,
    jobs: List[Job],
    *,
    concurrency: int = 5,
) -> Dict[int, Dict[str, Any]]:
    """Blended-rank jobs. Returns {job_id: {fit_score, reasoning, gaps, ...}}."""
    scorable = [j for j in jobs if j.id is not None]
    if not scorable:
        return {}
    skill_results: Dict[int, ScoreResult] = await score_jobs_bulk(
        profile_dict, scorable, concurrency=concurrency
    )

    ranked: Dict[int, Dict[str, Any]] = {}
    for job in scorable:
        sr = skill_results.get(job.id)
        if sr is None:
            continue
        pref_score, pref_notes = preference_alignment(search_profile, job)
        fit = blend(sr.score, pref_score, bool(job.undated))
        reasoning = sr.reasoning
        if pref_notes:
            reasoning = f"{reasoning} | Preferences: {'; '.join(pref_notes)}"
        ranked[job.id] = {
            "fit_score": fit,
            "skill_score": sr.score,
            "pref_score": round(pref_score * 100, 1),
            "reasoning": reasoning,
            "gaps": sr.gaps,
            "stretch_required": sr.stretch_required,
        }
    return ranked


# ---------------------------------------------------------------------------
# 7. The single callable: run_discovery
# ---------------------------------------------------------------------------

def run_discovery(
    session: Session,
    *,
    regenerate_search_profile: bool = False,
    max_age_days: int = 14,
    top_n: int = 20,
    score_concurrency: int = 5,
) -> DiscoveryRun:
    """Run the full discovery pipeline. Idempotent; returns a DiscoveryRun row.

    The returned ``DiscoveryRun`` is persisted and carries ``top_matches`` — a
    list of dicts (job_id, title, company, url, score, reasoning, posted_at,
    undated) describing the NEW top matches surfaced this run, so an external
    cron can report them. "New" = jobs first discovered in THIS run.
    """
    run = DiscoveryRun(started_at=datetime.utcnow())
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        profile = session.get(Profile, 1)
        profile_dict = profile.model_dump() if profile else {}
        prefs = (profile_dict.get("preferences") or {})

        search_profile = get_or_create_search_profile(
            session, regenerate=regenerate_search_profile
        )

        fetched, used = source_jobs(search_profile, prefs)
        run.sources_used = used
        run.fetched_count = len(fetched)

        batch = dedupe(fetched)
        persisted, new_jobs = upsert_jobs(session, batch)
        run.new_count = len(new_jobs)
        newly_inserted_ids = {j.id for j in new_jobs}

        fresh, _stale = freshness_partition(persisted, max_age_days=max_age_days)
        run.fresh_count = len(fresh)

        ranked = asyncio.run(
            rank_jobs(profile_dict, search_profile, fresh, concurrency=score_concurrency)
        )
        run.scored_count = len(ranked)

        # Persist fit fields onto the Job rows.
        id_to_job = {j.id: j for j in fresh}
        for job_id, r in ranked.items():
            job = id_to_job.get(job_id)
            if job is None:
                continue
            job.fit_score = r["fit_score"]
            job.fit_reasoning = r["reasoning"]
            job.fit_gaps = r["gaps"]
            session.add(job)
        session.commit()

        # NEW top matches = newly-inserted jobs that got ranked, sorted by fit.
        new_ranked = [
            (id_to_job[jid], ranked[jid])
            for jid in ranked
            if jid in newly_inserted_ids and jid in id_to_job
        ]
        new_ranked.sort(key=lambda t: t[1]["fit_score"], reverse=True)

        top_matches = []
        for job, r in new_ranked[:top_n]:
            top_matches.append({
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "score": r["fit_score"],
                "reasoning": r["reasoning"],
                "gaps": r["gaps"],
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "undated": bool(job.undated),
            })
        run.top_matches = top_matches
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
    except Exception as e:  # noqa: BLE001 - record failure, never crash caller
        session.rollback()
        run.error = f"{type(e).__name__}: {e}"
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
        print(f"[discovery] run failed: {run.error}")
    return run
