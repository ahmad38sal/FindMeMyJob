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

from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Fair-ask recommendation
# ---------------------------------------------------------------------------

class FairAsk(BaseModel):
    """A recommended negotiation target for the candidate on a specific job.

    All values in `currency` major units. `position` describes where `target`
    sits relative to the posted band (or market estimate when no band exists).
    """

    ask_low: int = Field(ge=0)
    ask_target: int = Field(ge=0)
    ask_high: int = Field(ge=0)
    currency: str = "USD"
    rationale: str = ""
    # "below_band" | "low_in_band" | "mid_band" | "upper_band" | "above_band"
    # | "no_band" — describes target vs. the posted range.
    position: str = "mid_band"
    source: str = "llm"  # "llm" | "heuristic"


_FAIR_ASK_INSTRUCTIONS = """\
You recommend a salary ASK for a specific candidate applying to a specific job.
This is what they should aim to negotiate for — NOT a market average.

Blend three signals:
  (a) The candidate: years/seniority from work_history, skills, and any
      experience-bank notes. Where do THEY realistically land?
  (b) The role + posted range: if a salary band is posted, position the ask
      inside it. A strong candidate should aim upper-middle of the band, not
      the floor. Only exceed the band ceiling if the candidate is clearly
      above the role; only go below the floor if they are a stretch.
  (c) Location / market: remote roles pay near national tech rates; adjust
      for the candidate's stated location/remote preference and cost-of-living.

Rules:
  - ask_low <= ask_target <= ask_high.
  - When a posted band exists, keep the ask realistic relative to it; an ask
    far above the ceiling will get screened out.
  - ask_target is the single number to anchor the negotiation on.
  - position: one of "below_band","low_in_band","mid_band","upper_band",
    "above_band" relative to the POSTED band, or "no_band" if none was posted.
  - rationale: ONE line, plain language, references experience + market.

Return STRICT JSON, no commentary, no markdown, no code fence:
{
  "ask_low": 150000,
  "ask_target": 170000,
  "ask_high": 185000,
  "currency": "USD",
  "rationale": "1 sentence",
  "position": "upper_band"
}
"""


def _years_experience(profile_dict: Dict[str, Any]) -> float:
    """Rough total years across work_history. Best-effort, never raises."""
    total = 0.0
    for role in profile_dict.get("work_history") or []:
        try:
            start = str((role or {}).get("start") or "")[:4]
            end_raw = (role or {}).get("end")
            end = str(end_raw or "")[:4]
            sy = int(start) if start.isdigit() else None
            ey = int(end) if end.isdigit() else 2026  # current role -> now
            if sy:
                total += max(0, (ey or 2026) - sy)
        except Exception:
            continue
    return total


def heuristic_fair_ask(
    profile_dict: Dict[str, Any],
    job: Job,
    estimate: Optional[Dict[str, Any]] = None,
) -> FairAsk:
    """Deterministic fallback used when the LLM is unavailable or fails.

    Strategy:
      - Posted band: aim for the upper-middle (target ~= 60% into the band),
        with a tight sub-range around it. Strong/senior candidates nudge higher.
      - No band but a market estimate exists: anchor on the user_target /
        median from the estimate.
      - Nothing at all: fall back to the profile salary_target if set, else a
        conservative experience-based guess.
    """
    prefs = profile_dict.get("preferences") or {}
    currency = job.currency or prefs.get("currency") or "USD"
    pref_target = prefs.get("salary_target")
    years = _years_experience(profile_dict)
    # 0.55 (junior) .. 0.70 (senior) position within a posted band.
    band_pos = 0.55 + min(0.15, max(0.0, (years - 3) * 0.02))

    lo, hi = job.salary_min, job.salary_max
    if lo and hi and hi >= lo:
        target = int(lo + (hi - lo) * band_pos)
        spread = max(int((hi - lo) * 0.12), 3000)
        ask_low = max(lo, target - spread)
        ask_high = min(hi, target + spread)  # don't ask above the posted ceiling
        if band_pos >= 0.65:
            position = "upper_band"
        elif band_pos <= 0.45:
            position = "low_in_band"
        else:
            position = "mid_band"
        rationale = (
            f"With ~{int(years)}y experience, aim for the upper-middle of the "
            f"posted band rather than its floor."
        )
        return FairAsk(ask_low=ask_low, ask_target=target, ask_high=ask_high,
                       currency=currency, rationale=rationale,
                       position=position, source="heuristic")

    # Single posted endpoint (only min OR only max).
    single = lo or hi
    if single:
        target = int(single * 1.05)
        return FairAsk(ask_low=single, ask_target=target,
                       ask_high=int(single * 1.12), currency=currency,
                       position="no_band", source="heuristic",
                       rationale="Posting gave one number; aim slightly above it.")

    # No posted salary — lean on the market estimate, then profile, then guess.
    if estimate:
        ut = estimate.get("user_target") or estimate.get("market_median")
        med = estimate.get("market_median") or ut
        if ut:
            return FairAsk(
                ask_low=int(med or ut), ask_target=int(ut),
                ask_high=int((estimate.get("market_max") or ut * 1.1)),
                currency=estimate.get("currency", currency),
                position="no_band", source="heuristic",
                rationale="No posted range; anchored on the market estimate for your target.",
            )

    if pref_target:
        return FairAsk(ask_low=int(pref_target * 0.95), ask_target=int(pref_target),
                       ask_high=int(pref_target * 1.1), currency=currency,
                       position="no_band", source="heuristic",
                       rationale="No posted range or estimate; using your profile salary target.")

    base = int(80000 + years * 6000)
    return FairAsk(ask_low=int(base * 0.9), ask_target=base, ask_high=int(base * 1.15),
                   currency=currency, position="no_band", source="heuristic",
                   rationale="No salary signal available; rough experience-based estimate.")


def compute_fair_ask(
    profile_dict: Dict[str, Any],
    job: Job,
    estimate: Optional[Dict[str, Any]] = None,
) -> FairAsk:
    """LLM-recommended ask, with a deterministic heuristic fallback.

    Never raises — any LLM/parse failure degrades to `heuristic_fair_ask`.
    """
    prefs = profile_dict.get("preferences") or {}
    description = (job.description or "")[:3000]
    band = (
        f"{job.salary_min}–{job.salary_max}"
        if (job.salary_min or job.salary_max) else "none posted"
    )
    est_line = "none"
    if estimate:
        est_line = (
            f"market {estimate.get('market_min')}–{estimate.get('market_max')} "
            f"(median {estimate.get('market_median')}), "
            f"prior user_target {estimate.get('user_target')}"
        )

    user_prompt = (
        f"JOB:\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or '-'} ({job.work_mode or 'mode unknown'})\n"
        f"Seniority: {job.seniority or '-'}\n"
        f"Posted band: {band} {job.currency}\n"
        f"Market estimate (if any): {est_line}\n\n"
        f"DESCRIPTION (excerpt):\n{description}\n\n"
        f"CANDIDATE HINTS:\n"
        f"- preferences.salary_target: {prefs.get('salary_target', 'not set')}\n"
        f"- preferences.salary_min (floor): {prefs.get('salary_min', 'not set')}\n"
        f"- preferences.locations: {prefs.get('locations') or 'not set'}\n"
        f"- preferences.work_modes: {prefs.get('work_modes') or 'not set'}\n\n"
        f"Recommend the candidate's fair ask. Output JSON only."
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_FAIR_ASK_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=384,
            temperature=0.2,
        )
        ask = FairAsk.model_validate_json(_strip_code_fence(raw))
        # Sanity: enforce ordering; if the model emitted nonsense, fall back.
        if not (ask.ask_low <= ask.ask_target <= ask.ask_high):
            raise ValueError("ask bounds out of order")
        ask.source = "llm"
        return ask
    except Exception as e:  # noqa: BLE001 — must never 500
        print(f"[fair-ask] LLM failed, using heuristic: {e}")
        return heuristic_fair_ask(profile_dict, job, estimate)


# ---------------------------------------------------------------------------
# Unified meter view — assembled server-side so the template stays dumb
# ---------------------------------------------------------------------------

class SalaryView(BaseModel):
    """Everything the salary panel needs to render the meter for ANY job.

    The meter is drawn on a single [scale_lo .. scale_hi] axis. Posted band,
    market estimate, and fair-ask are positioned as percentages on that axis.
    """

    currency: str = "USD"
    has_posted: bool = False
    has_estimate: bool = False
    scale_lo: int = 0
    scale_hi: int = 0

    # Posted band (None when not posted).
    posted_min: Optional[int] = None
    posted_max: Optional[int] = None
    posted_lo_pct: Optional[float] = None
    posted_hi_pct: Optional[float] = None

    # Market estimate (None when not estimated).
    market_min: Optional[int] = None
    market_median: Optional[int] = None
    market_max: Optional[int] = None
    median_pct: Optional[float] = None

    # Fair ask.
    ask_low: Optional[int] = None
    ask_target: Optional[int] = None
    ask_high: Optional[int] = None
    ask_low_pct: Optional[float] = None
    ask_target_pct: Optional[float] = None
    ask_high_pct: Optional[float] = None
    ask_rationale: str = ""
    ask_position: str = "no_band"
    ask_source: str = "heuristic"


def build_salary_view(
    job: Job,
    estimate: Optional[Dict[str, Any]] = None,
    fair_ask: Optional[Dict[str, Any]] = None,
) -> SalaryView:
    """Assemble a SalaryView. Pure: no LLM, no DB. Safe on missing/odd data.

    `estimate` / `fair_ask` are the cached JSON dicts off `job.raw`.
    """
    currency = (
        (fair_ask or {}).get("currency")
        or (estimate or {}).get("currency")
        or job.currency
        or "USD"
    )
    has_posted = bool(job.salary_min or job.salary_max)
    has_estimate = bool(estimate)

    # Collect every known value to bound the meter axis.
    vals: List[int] = []
    for v in (job.salary_min, job.salary_max):
        if v:
            vals.append(int(v))
    if estimate:
        for k in ("market_min", "market_median", "market_max", "user_target"):
            v = estimate.get(k)
            if v:
                vals.append(int(v))
    if fair_ask:
        for k in ("ask_low", "ask_target", "ask_high"):
            v = fair_ask.get(k)
            if v:
                vals.append(int(v))

    if not vals:
        # No signal at all — leave a zero-width axis so the template suppresses
        # the meter entirely (it guards on scale_hi > scale_lo).
        return SalaryView(
            currency=currency, has_posted=has_posted, has_estimate=has_estimate,
            scale_lo=0, scale_hi=0,
        )

    raw_lo, raw_hi = min(vals), max(vals)
    # Pad the axis ~8% each side so edge markers aren't pinned to the rail.
    if raw_hi > raw_lo:
        pad = int((raw_hi - raw_lo) * 0.08) or 1000
    else:
        pad = max(int(raw_hi * 0.15), 5000) if raw_hi else 1000
    scale_lo = max(0, raw_lo - pad)
    scale_hi = raw_hi + pad

    def pct(v: Optional[int]) -> Optional[float]:
        if v is None:
            return None
        return position_pct(int(v), scale_lo, scale_hi)

    view = SalaryView(
        currency=currency,
        has_posted=has_posted,
        has_estimate=has_estimate,
        scale_lo=scale_lo,
        scale_hi=scale_hi,
    )

    if has_posted:
        # A single endpoint draws a thin band by reusing the value for both.
        pmin = job.salary_min or job.salary_max
        pmax = job.salary_max or job.salary_min
        view.posted_min = pmin
        view.posted_max = pmax
        view.posted_lo_pct = pct(pmin)
        view.posted_hi_pct = pct(pmax)

    if estimate:
        view.market_min = estimate.get("market_min")
        view.market_median = estimate.get("market_median")
        view.market_max = estimate.get("market_max")
        view.median_pct = pct(estimate.get("market_median"))

    if fair_ask:
        view.ask_low = fair_ask.get("ask_low")
        view.ask_target = fair_ask.get("ask_target")
        view.ask_high = fair_ask.get("ask_high")
        view.ask_low_pct = pct(fair_ask.get("ask_low"))
        view.ask_target_pct = pct(fair_ask.get("ask_target"))
        view.ask_high_pct = pct(fair_ask.get("ask_high"))
        view.ask_rationale = fair_ask.get("rationale", "")
        view.ask_position = fair_ask.get("position", "no_band")
        view.ask_source = fair_ask.get("source", "heuristic")

    return view
