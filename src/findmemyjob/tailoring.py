"""Resume tailoring + cover letter generation + master/tailored diff.

Hard constraint (from user): never fabricate experience. The tailoring prompt
forbids invention; rewrites must trace back to a real bullet/skill in the
master profile. The diff view exists so the user can verify this visually.
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from findmemyjob.llm import DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence
from findmemyjob.models import Job


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

Return STRICT JSON (no commentary, no markdown) with this shape:
{
  "summary": "tailored 'about me' paragraph, drawn from master summary + work history",
  "work_history": [ {company, title, location, start, end, bullets, skills}, ... ],
  "skills": [ {name, category, years, evidence}, ... ],
  "education": [ ... ],
  "keywords_targeted": ["keyword from JD that you surfaced", ...]
}
"""


def tailor_resume(profile_dict: Dict[str, Any], job: Job) -> TailoredResume:
    user_prompt = (
        f"JOB DESCRIPTION:\n"
        f"{job.title} @ {job.company}\n"
        f"{job.description}\n\n"
        f"Tailor the candidate's resume to this job. Output JSON only."
    )
    raw = llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_TAILOR_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=4096,
        temperature=0.4,
    )
    return TailoredResume.model_validate_json(_strip_code_fence(raw))


_COVER_LETTER_INSTRUCTIONS = """\
You write short, specific cover letters (under 250 words) for a candidate
applying to a job. Stay truthful — the same rule from resume tailoring applies:
draw only from what's in the profile.

Voice: confident, direct, not over-formal. No "I am writing to apply for the
position of..." opener. Lead with why this role specifically. End with a clear
ask for next steps.

Output: just the letter text. No preamble, no signature block (we'll add one).
"""


def generate_cover_letter(profile_dict: Dict[str, Any], job: Job) -> str:
    user_prompt = (
        f"JOB:\n{job.title} @ {job.company}\n\n"
        f"DESCRIPTION:\n{job.description}\n\n"
        f"Write the cover letter."
    )
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
