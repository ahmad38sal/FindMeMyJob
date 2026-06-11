"""Match a job against the user's profile.

Two-stage pipeline:
  1. `prefilter` — cheap, deterministic rules (salary floor, location, work mode,
     blocklist). No LLM call. Drops obvious mismatches before they cost tokens.
  2. `score_job` — LLM-based scoring on what survives. The stretch slider feeds
     the prompt and decides how lenient the qualification check is.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from findmemyjob.llm import DEFAULT_MATCH_MODEL, llm
from findmemyjob.models import Job


class ScoreResult(BaseModel):
    score: float = Field(ge=0, le=100)
    reasoning: str
    gaps: List[str] = Field(default_factory=list)
    stretch_required: bool = False
    matched_skills: List[str] = Field(default_factory=list)


def prefilter(profile_dict: Dict[str, Any], job: Job) -> Optional[str]:
    """Return None if the job passes; otherwise a short reason it was dropped."""
    prefs = profile_dict.get("preferences", {}) or {}

    # Blocklist
    blocked = {c.lower() for c in prefs.get("exclude_companies", [])}
    if job.company.lower() in blocked:
        return f"company blocklisted ({job.company})"

    # Salary floor
    salary_min = prefs.get("salary_min")
    if salary_min and job.salary_max and job.salary_max < salary_min:
        return f"salary cap below floor ({job.salary_max} < {salary_min})"

    # Work mode
    accepted_modes = {m for m in prefs.get("work_modes", [])}
    if accepted_modes and job.work_mode and job.work_mode not in accepted_modes:
        return f"work mode {job.work_mode!r} not in accepted set"

    return None


_MATCH_INSTRUCTIONS = """\
You score how well a job matches a candidate profile.

Hard rule: NEVER assume the candidate has a skill, project, or qualification
that isn't represented in their profile. If something is missing, list it as
a gap. Do not invent experience.

Skill evidence: each entry in `skills` may have an `evidence` field — a
candidate-asserted, free-text claim about how they used that skill (e.g.
"4 yrs running K8s clusters at Apple"). Treat evidence as a first-class
representation of the skill: if a JD requirement is covered by a skill that
has evidence, do NOT list it as a gap, and credit it in `matched_skills`.
If evidence is absent, weigh the skill against bullets/work history as
usual.

The user's `stretch_slider` controls how generous you are about gaps:
  - 0   = only score highly when the candidate clearly meets every requirement
  - 30  = small adjacent stretches OK (related-language, related-domain)
  - 60  = significant stretches OK if the foundation is there
  - 100 = include moonshot reaches if the candidate could plausibly grow into it

Return STRICT JSON with this shape (no commentary, no markdown):
{
  "score": 0-100,
  "reasoning": "1-3 sentences",
  "gaps": ["missing skill 1", ...],
  "stretch_required": true|false,
  "matched_skills": ["skill from profile that's relevant", ...]
}
"""


def score_job(profile_dict: Dict[str, Any], job: Job) -> ScoreResult:
    """LLM-score a single job against the profile (with stretch slider applied)."""
    stretch = (profile_dict.get("preferences") or {}).get("stretch_slider", 30)

    user_prompt = (
        f"Stretch slider value: {stretch}\n\n"
        f"JOB:\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Team: {job.team or '-'}\n"
        f"Location: {job.location or '-'} ({job.work_mode or 'mode unknown'})\n"
        f"Seniority: {job.seniority or '-'}\n"
        f"Salary: {job.salary_min or '?'}–{job.salary_max or '?'} {job.currency}\n\n"
        f"DESCRIPTION:\n{job.description}\n\n"
        f"Score this match. Output JSON only."
    )

    raw = llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_MATCH_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_MATCH_MODEL,
        max_tokens=1024,
        temperature=0.2,
    )
    return ScoreResult.model_validate_json(_strip_code_fence(raw))


def _strip_code_fence(s: str) -> str:
    """LLMs sometimes wrap JSON in ```json blocks despite the instruction not to."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()
