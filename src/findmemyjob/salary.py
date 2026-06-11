"""Estimate a salary range when a job posting omits one.

External sources (Greenhouse / Lever / HN / RemoteOK) often don't surface a
range. We ask Claude for a market estimate from the title + seniority +
location + JD excerpt, plus a recommended target for the candidate based on
their years of experience and stated `salary_target` preference (if any).

Output is cached on the `Job.raw["salary_estimate"]` JSON dict so re-views of
the same job don't re-call the LLM. The detail page exposes a "Re-estimate"
button to refresh.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from findmemyjob.llm import DEFAULT_MATCH_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import Job


class SalaryEstimate(BaseModel):
    """Structured estimate. All values in `currency` major units (e.g. USD)."""

    market_min: int = Field(ge=0)
    market_median: int = Field(ge=0)
    market_max: int = Field(ge=0)
    # Where in the range this user should target. Either pulled from
    # profile.preferences.salary_target (source="profile") or LLM-estimated
    # based on the candidate's experience (source="estimated").
    user_target: int = Field(ge=0)
    user_target_source: str = "estimated"  # "profile" | "estimated"
    currency: str = "USD"
    rationale: str = ""


_SALARY_INSTRUCTIONS = """\
You estimate a market salary range for a job posting that didn't list one,
plus a recommended target for the specific candidate based on their experience.

Approach:
  - Use title, seniority, company size/tier, location (cost-of-living), and
    the JD excerpt to bound the market range. Cap location adjustments at
    reasonable multiples — e.g. SF/NYC tech ~ baseline, mid-market US ~ 0.85x,
    LCOL US ~ 0.7x, EU/India/LATAM scaled accordingly.
  - market_min / market_max should bracket roughly the 10th-90th percentile.
    market_median sits in between.
  - For user_target: anchor on the candidate's years of experience visible in
    `work_history` (count years across roles, weight recent more), plus any
    explicit `salary_target` in preferences. If preferences.salary_target is
    set, use it as user_target and set user_target_source = "profile".
    Otherwise estimate based on experience and set source = "estimated".

Be conservative — better to underestimate than to mislead. If you genuinely
can't bound a range (no signal at all), output a wide one and say so in the
rationale.

Return STRICT JSON, no commentary, no markdown, no code fence:
{
  "market_min": 120000,
  "market_median": 160000,
  "market_max": 210000,
  "user_target": 175000,
  "user_target_source": "profile" | "estimated",
  "currency": "USD",
  "rationale": "1-2 sentences"
}
"""


def estimate_salary(profile_dict: Dict[str, Any], job: Job) -> SalaryEstimate:
    """Estimate market range + user target for a job that has no posted salary."""
    prefs = profile_dict.get("preferences") or {}
    target = prefs.get("salary_target")
    salary_min_floor = prefs.get("salary_min")

    # Truncate the JD — long descriptions blow tokens for marginal signal.
    description = (job.description or "")[:3000]

    user_prompt = (
        f"JOB:\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Team: {job.team or '-'}\n"
        f"Location: {job.location or '-'} ({job.work_mode or 'mode unknown'})\n"
        f"Seniority: {job.seniority or '-'}\n\n"
        f"DESCRIPTION (excerpt):\n{description}\n\n"
        f"CANDIDATE PREFERENCE HINTS:\n"
        f"- preferences.salary_target: {target if target is not None else 'not set'}\n"
        f"- preferences.salary_min (floor):  {salary_min_floor if salary_min_floor is not None else 'not set'}\n\n"
        f"Estimate market range + this candidate's target. Output JSON only."
    )
    raw = llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_SALARY_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_MATCH_MODEL,
        max_tokens=512,
        temperature=0.2,
    )
    return SalaryEstimate.model_validate_json(_strip_code_fence(raw))


def position_pct(value: int, lo: int, hi: int) -> float:
    """Map `value` to a 0-100 % position on the [lo..hi] bar.

    Clamped to [0, 100] so a target outside the market range doesn't render
    off the bar — instead it pins to the edge with a visual hint.
    """
    if hi <= lo:
        return 50.0
    pct = (value - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, pct))


def fmt_money(value: Optional[int], currency: str = "USD") -> str:
    """$120,000 → '$120k'. Compact and readable on a meter."""
    if value is None:
        return "—"
    symbol = "$" if currency == "USD" else ""
    if value >= 1_000_000:
        return f"{symbol}{value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"{symbol}{value//1_000}k"
    return f"{symbol}{value}"
