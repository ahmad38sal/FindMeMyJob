"""Resume tailoring + cover letter generation + master/tailored diff.

Hard constraint (from user): never fabricate experience. The tailoring prompt
forbids invention; rewrites must trace back to a real bullet/skill in the
master profile. The diff view exists so the user can verify this visually.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from findmemyjob.llm import DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import ExperienceItem, Job
from findmemyjob.resume_format import format_resume_content


class TailoredResume(BaseModel):
    """Output of the tailoring step — same shape as profile sections, post-edit."""
    summary: str = ""
    work_history: List[Dict[str, Any]] = Field(default_factory=list)
    skills: List[Dict[str, Any]] = Field(default_factory=list)
    education: List[Dict[str, Any]] = Field(default_factory=list)
    keywords_targeted: List[str] = Field(default_factory=list)


class DiffEntry(BaseModel):
    """One bullet/section's before/after, for the truthfulness check."""
    section: str           # "work_history[0].bullets[2]", "summary", etc.
    before: str
    after: str
    kind: str              # "rephrased", "reordered", "kept", "dropped"


_TAILOR_INSTRUCTIONS = """\
You tailor a candidate's master resume to a specific job description, optimizing
for ATS keyword match while keeping the resume 100% truthful.

ABSOLUTE RULES:
  1. Never invent experience, skills, projects, or numbers. If the master profile
     doesn't claim it, the tailored resume cannot claim it.
  2. You MAY: rephrase bullets to surface JD keywords that map to real experience,
     reorder bullets/sections so relevant content is first, expand or compress.
  3. You MAY NOT: add new bullets, embellish scope, fabricate metrics, list
     unverified skills, or make adjacent claims ("worked with X" when the
     profile only says they worked with Y).
  4. If the JD wants a skill the candidate doesn't have, do NOT add it. The
     tailoring isn't where you compensate — the matching step already flagged
     the gap.

Skill evidence: each entry in `skills` may have an `evidence` field — a
candidate-asserted, free-text claim about how they used that skill (e.g.
"4 yrs running K8s clusters at Apple"). Treat evidence as a first-class
claim from the candidate, equivalent to a master-profile bullet:
  - You MAY include the skill in the tailored `skills` list (carry the
    `evidence` through unchanged).
  - You MAY surface evidence content in the summary or as a rephrased bullet
    on whichever role best fits, as long as you don't add details that aren't
    already in the evidence text.
  - Do NOT embellish beyond what the evidence text literally says.

Experience bank: the candidate may also supply an EXPERIENCE BANK — rough,
unpolished notes in their own words about skills and experiences (some not on
the resume). Treat these as real, candidate-asserted source material, on the
same footing as a master-profile bullet. When notes are provided:
  - SELECT only the notes relevant to THIS specific job; ignore the rest.
  - REWRITE/POLISH selected notes into professional, achievement-oriented resume
    bullets tailored to the role. NEVER copy the candidate's wording verbatim —
    always reframe into strong resume language.
  - Surface polished notes in the summary or as bullets on whichever existing
    role best fits (or the most recent role if none fits clearly).
  - Do NOT fabricate facts, metrics, or scope beyond what a note literally
    states. A note is a claim, not a license to embellish.
  - Notes tagged "(linked to THIS job)" are the most relevant — prioritize them.

RESUME FORMATTING BEST PRACTICES (follow for strong, ATS-friendly output):
  Bullets:
    - Bullet COUNT per role scales with recency/relevance: the most recent /
      most relevant role gets 4-6 bullets, mid roles 3-4, older/less-relevant
      roles 2-3. Never exceed 6 on any role. Keep the bullets most relevant to
      THIS job; drop the weakest rather than padding.
    - Each bullet is ONE concise line: aim ~90-140 characters, never exceed
      ~160. Tighten wordy bullets (keep the metric + impact) instead of running
      long.
    - Start every bullet with a strong past-tense action verb (Led, Built,
      Shipped, Reduced, Automated, Designed, Launched…). Avoid weak openers
      ("Responsible for", "Worked on", "Helped with") and first-person pronouns.
    - Prefer quantified impact (numbers/%/time) when the source has it; never
      invent metrics.
  Skills:
    - Emit SHORT tags only (1-4 words each): "React", "Docker", "DTC Creative
      Strategy", "Meta Ads Manager". NEVER copy job-requirement sentences or
      phrases like "3-5+ years of experience in ..." into the skills list.
    - Deduplicate and merge synonyms (React.js/ReactJS -> React). Aim for ~8-16
      tags total — don't pad or bloat.
    - CATEGORIZE each skill (set `category`) using sensible buckets such as
      Languages, Frameworks & Libraries, Tools & Platforms, Cloud & DevOps,
      Design, Marketing/Domain. Use "Other" only as a last resort.

Return STRICT JSON (no commentary, no markdown) with this shape:
{
  "summary": "tailored 'about me' paragraph, drawn from master summary + work history",
  "work_history": [ {company, title, location, start, end, bullets, skills}, ... ],
  "skills": [ {name, category, years, evidence}, ... ],
  "education": [ ... ],
  "keywords_targeted": ["keyword from JD that you surfaced", ...]
}
"""


def _format_experience_bank(
    items: Optional[List[ExperienceItem]], job: Job
) -> str:
    """Render active experience-bank notes as a prompt block.

    Items linked to THIS job come first and are flagged so the model prioritizes
    them; the rest follow as general context. Returns "" when there's nothing to
    add, so an empty bank leaves the prompt byte-for-byte identical to before.
    """
    active = [it for it in (items or []) if it.active and (it.raw_text or "").strip()]
    if not active:
        return ""

    linked = [it for it in active if it.job_id == job.id]
    others = [it for it in active if it.job_id != job.id]

    def render(it: ExperienceItem, *, this_job: bool) -> str:
        head_bits = []
        if it.label:
            head_bits.append(it.label.strip())
        if it.category:
            head_bits.append(f"[{it.category.strip()}]")
        if this_job:
            head_bits.append("(linked to THIS job)")
        head = " ".join(head_bits)
        prefix = f"- {head}: " if head else "- "
        return f"{prefix}{it.raw_text.strip()}"

    lines = [render(it, this_job=True) for it in linked]
    lines += [render(it, this_job=False) for it in others]
    return (
        "\n\nEXPERIENCE BANK (rough notes in the candidate's own words — select "
        "the relevant ones and REWRITE them into polished resume bullets, never "
        "verbatim):\n" + "\n".join(lines)
    )


# One-page trimming caps. Applied after parsing so the rendered PDF realistically
# fits when the user asks for a single page — the LLM can't perfectly control
# rendered page count, so prompt guidance + this content cap work together.
_ONE_PAGE_MAX_ROLES = 4
_ONE_PAGE_BULLET_CAPS = [4, 3, 3, 2]  # bullets kept per role, by position


def _options_block(include_summary: bool, page_length: str) -> str:
    """Prompt guidance for the two tailor options.

    Returns "" when both options are at their defaults (summary on, automatic
    length), so the default path's prompt is byte-for-byte unchanged.
    """
    parts: List[str] = []
    if not include_summary:
        parts.append(
            'SUMMARY: Do NOT include a professional summary section. Set the '
            '"summary" field to an empty string "".'
        )
    if page_length == "1":
        parts.append(
            "LENGTH: Target a strict ONE-PAGE resume. Be aggressive about brevity — "
            "keep only the most relevant roles and the strongest bullets (about 3-4 "
            "bullets on the most relevant role, fewer on older ones), condense phrasing, "
            "and drop the least-relevant experience. Prioritize content matching the JD."
        )
    elif page_length == "2":
        parts.append(
            "LENGTH: Target roughly TWO PAGES. You may include fuller detail and more "
            "bullets per role, while staying truthful and relevant."
        )
    if not parts:
        return ""
    return "\n\nOUTPUT OPTIONS (follow exactly):\n" + "\n".join(f"- {p}" for p in parts)


def _apply_options(
    tailored: TailoredResume, include_summary: bool, page_length: str
) -> TailoredResume:
    """Enforce the options on parsed output (defensive + best-effort length).

    Defaults (summary on, auto length) are a no-op, so the default path returns
    exactly what the model produced.
    """
    if not include_summary:
        tailored.summary = ""
    if page_length == "1":
        trimmed: List[Dict[str, Any]] = []
        for i, role in enumerate(tailored.work_history[:_ONE_PAGE_MAX_ROLES]):
            role = dict(role)
            cap = _ONE_PAGE_BULLET_CAPS[i] if i < len(_ONE_PAGE_BULLET_CAPS) else 2
            bullets = role.get("bullets") or []
            if len(bullets) > cap:
                role["bullets"] = bullets[:cap]
            trimmed.append(role)
        tailored.work_history = trimmed
    return tailored


def tailor_resume(
    profile_dict: Dict[str, Any],
    job: Job,
    experience_items: Optional[List[ExperienceItem]] = None,
    *,
    include_summary: bool = True,
    page_length: str = "auto",
) -> TailoredResume:
    if page_length not in ("1", "2"):
        page_length = "auto"
    user_prompt = (
        f"JOB DESCRIPTION:\n"
        f"{job.title} @ {job.company}\n"
        f"{job.description}\n\n"
        f"Tailor the candidate's resume to this job. Output JSON only."
    )
    user_prompt += _format_experience_bank(experience_items, job)
    user_prompt += _options_block(include_summary, page_length)
    raw = llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_TAILOR_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=8192,
        temperature=0.4,
    )
    tailored = _parse_tailored(raw, profile_dict)
    tailored = _apply_options(tailored, include_summary, page_length)
    # Enforce resume best practices deterministically — even when the LLM drifts
    # or the parser fell back to the raw master profile.
    job_text = f"{job.title or ''} {job.description or ''}"
    formatted = format_resume_content(
        tailored.model_dump(), job_text=job_text, page_length=page_length
    )
    return TailoredResume(**formatted)


def _parse_tailored(raw: str, profile_dict: Dict[str, Any]) -> TailoredResume:
    """Parse the tailoring reply, tolerant of code fences / thinking preamble.

    If the model output can't be parsed at all (e.g. truncated), fall back to a
    safe copy of the master profile sections so the user still gets a usable
    resume instead of a 500. They can re-tailor to refine.
    """
    cleaned = _strip_code_fence(raw or "").strip()
    try:
        return TailoredResume.model_validate_json(cleaned)
    except (ValidationError, ValueError):
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return TailoredResume.model_validate_json(match.group(0))
        except (ValidationError, ValueError):
            pass
    # Last resort: echo the master profile (still 100% truthful, no fabrication).
    return TailoredResume(
        summary=profile_dict.get("summary", "") or "",
        work_history=profile_dict.get("work_history", []) or [],
        skills=profile_dict.get("skills", []) or [],
        education=profile_dict.get("education", []) or [],
        keywords_targeted=[],
    )


_COVER_LETTER_INSTRUCTIONS = """\
You write short, specific cover letters (under 250 words) for a candidate
applying to a job. Stay truthful — the same rule from resume tailoring applies:
draw only from what's in the profile.

Voice: confident, direct, not over-formal. No "I am writing to apply for the
position of..." opener. Lead with why this role specifically. End with a clear
ask for next steps.

Output: just the letter text. No preamble, no signature block (we'll add one).
"""


def generate_cover_letter(
    profile_dict: Dict[str, Any],
    job: Job,
    experience_items: Optional[List[ExperienceItem]] = None,
) -> str:
    user_prompt = (
        f"JOB:\n{job.title} @ {job.company}\n\n"
        f"DESCRIPTION:\n{job.description}\n\n"
        f"Write the cover letter."
    )
    user_prompt += _format_experience_bank(experience_items, job)
    return llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_COVER_LETTER_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=1024,
        temperature=0.6,
    ).strip()


def compute_diff(master: Dict[str, Any], tailored: TailoredResume) -> List[DiffEntry]:
    """Bullet-level diff between master profile and tailored resume.

    The user reviews this in the UI before approving — visual proof that
    nothing was fabricated.
    """
    diffs: List[DiffEntry] = []

    master_summary = master.get("summary", "") or ""
    if tailored.summary != master_summary:
        diffs.append(DiffEntry(
            section="summary",
            before=master_summary,
            after=tailored.summary,
            kind="rephrased" if master_summary else "kept",
        ))

    master_jobs = master.get("work_history", []) or []
    for i, t_job in enumerate(tailored.work_history):
        m_job = next(
            (m for m in master_jobs
             if m.get("company") == t_job.get("company") and m.get("title") == t_job.get("title")),
            None,
        )
        if not m_job:
            # Tailored claims a job that isn't in master — truthfulness violation.
            diffs.append(DiffEntry(
                section=f"work_history[{i}]",
                before="(not in master)",
                after=f"{t_job.get('title')} @ {t_job.get('company')}",
                kind="FABRICATED",
            ))
            continue
        m_bullets = m_job.get("bullets", []) or []
        t_bullets = t_job.get("bullets", []) or []
        for j, t_bullet in enumerate(t_bullets):
            if t_bullet in m_bullets:
                kind = "kept"
                before = t_bullet
            else:
                kind = "rephrased"
                before = m_bullets[j] if j < len(m_bullets) else "(no master bullet)"
            if before != t_bullet:
                diffs.append(DiffEntry(
                    section=f"work_history[{i}].bullets[{j}]",
                    before=before,
                    after=t_bullet,
                    kind=kind,
                ))

    return diffs
