"""Mock-interview engine.

Drives a realistic, back-and-forth interview for a *specific* job, blending the
job posting + the candidate's Profile + their ExperienceItem bank. The LLM plays
a single interviewer who reacts to each answer and asks natural follow-ups.

The interview flows through four round types in order:
    recruiter  -> behavioral -> technical -> company

Round progression is controlled deterministically in Python (a fixed question
budget per round) so the flow is predictable and testable, while the LLM
supplies the natural reactions, follow-ups, and coaching feedback.

Reliability: every LLM call goes through a tolerant parser and degrades to a
sensible canned response on failure. Nothing here raises to the caller — a busy
model must never 500 the interview.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from findmemyjob.llm import DEFAULT_MATCH_MODEL, _strip_code_fence, llm
from findmemyjob.models import ExperienceItem, InterviewSession, InterviewTurn, Job

# Round order + how many candidate answers each round takes before advancing.
ROUND_ORDER: List[str] = ["recruiter", "behavioral", "technical", "company"]
ROUND_LABELS: Dict[str, str] = {
    "recruiter": "Recruiter screen",
    "behavioral": "Behavioral (STAR)",
    "technical": "Role / technical",
    "company": "Company & motivation",
}
ROUND_FOCUS: Dict[str, str] = {
    "recruiter": (
        "Logistics and fit-at-a-glance. Ask things a recruiter screens for: "
        "'walk me through your background', salary expectations, availability / "
        "notice period, work-authorization or location/remote fit. Keep it light "
        "and conversational."
    ),
    "behavioral": (
        "STAR behavioral questions — 'tell me about a time...'. Probe ownership, "
        "conflict, failure, leadership, impact. PREFER topics you can tie to the "
        "candidate's experience bank notes below. Ask for specifics: situation, "
        "what THEY did, and the measurable result."
    ),
    "technical": (
        "Role-specific depth. Infer the discipline from the job title/description "
        "and ask accordingly: for engineering, a coding/system-design framing "
        "(reason aloud, no IDE needed); for data, an analysis/SQL/modeling "
        "scenario; for design/content/marketing, portfolio, process, and a "
        "craft critique. Match the question to THIS role, not a generic template."
    ),
    "company": (
        "Motivation and company fit. Use the posting: 'why this role', 'why this "
        "company', alignment with the team's mission/values, and a thoughtful "
        "'what questions do you have for us'. Reward specific, researched answers."
    ),
}

DEFAULT_QUESTIONS_PER_ROUND = 2


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_INTERVIEWER_INSTRUCTIONS = """\
You are a seasoned hiring interviewer running a realistic mock interview for one
specific job at one specific company. You are warm but professional, and you
sound like a real person on a video call — not a form. You adapt to what the
candidate just said and ask natural follow-ups instead of reading a fixed list.

You also quietly coach: after each candidate answer you give a SHORT, honest
piece of feedback (one thing that worked, one concrete improvement, and a
stronger way to frame it). Feedback is constructive and specific to THIS role.

You will be told the CURRENT ROUND and its focus. Stay in that round's lane for
your next question. React to the candidate's latest answer first (a sentence of
acknowledgement or a brief probe), THEN ask the next question.

Use the job posting, the candidate's profile, and especially their EXPERIENCE
BANK notes to make questions concrete (e.g. ask them to expand a real project).

Return STRICT JSON, no markdown, no code fence:
{
  "feedback": {
    "worked": "1 short sentence on what was effective",
    "improve": "1 short sentence — the single highest-leverage fix",
    "stronger": "1 short sentence modeling a stronger phrasing/angle"
  },
  "score": 0-100,            // quality of THIS answer for this role
  "next_message": "your spoken reply: a brief reaction + the next question"
}
"""

_OPENER_INSTRUCTIONS = """\
You are a seasoned hiring interviewer starting a realistic mock interview for one
specific job at one specific company. Warm, professional, human — like the first
minute of a real video call.

Give a one-line friendly intro (greet, name the role/company, set expectations),
then ask your FIRST question for the RECRUITER SCREEN round (logistics / "walk me
through your background"). Keep it to 2-3 sentences total.

Return STRICT JSON, no markdown, no code fence:
{ "next_message": "your greeting + first question" }
"""

_DEBRIEF_INSTRUCTIONS = """\
You are an interview coach writing the post-interview debrief for a candidate who
just finished a mock interview for a specific role. Be honest, specific, and
encouraging. Ground every point in what they actually said in the transcript.

Return STRICT JSON, no markdown, no code fence:
{
  "overall_score": 0-100,
  "summary": "2-3 sentence honest overall read",
  "round_scores": [
    {"round": "recruiter", "score": 0-100, "note": "1 sentence"},
    {"round": "behavioral", "score": 0-100, "note": "1 sentence"},
    {"round": "technical", "score": 0-100, "note": "1 sentence"},
    {"round": "company", "score": 0-100, "note": "1 sentence"}
  ],
  "strengths": ["2-4 concrete strengths"],
  "gaps": ["2-4 concrete gaps / risks"],
  "better_answers": [
    {"question": "a question they fumbled", "reframe": "a stronger answer they could give"}
  ]
}
"""


def _format_experience_bank(items: Optional[List[ExperienceItem]], job: Job) -> str:
    """Render active experience-bank notes for the prompt. '' when empty."""
    active = [it for it in (items or []) if getattr(it, "active", True)]
    if not active:
        return ""
    # Job-linked items first — they're the most relevant to this interview.
    linked = [it for it in active if it.job_id == job.id]
    others = [it for it in active if it.job_id != job.id]
    lines = ["EXPERIENCE BANK (candidate's own rough notes — use to make questions concrete):"]
    for it in linked + others:
        tag = " (linked to THIS job)" if it.job_id == job.id else ""
        label = f"[{it.label}] " if it.label else ""
        lines.append(f"- {label}{it.raw_text}{tag}")
    return "\n".join(lines)


def _job_block(job: Job) -> str:
    desc = (job.description or "")[:2500]
    return (
        f"JOB POSTING:\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Team: {job.team or '-'}\n"
        f"Location: {job.location or '-'} ({job.work_mode or 'mode unknown'})\n"
        f"Seniority: {job.seniority or '-'}\n"
        f"Description (excerpt):\n{desc}\n"
    )


def _transcript_block(turns: List[InterviewTurn], limit: int = 12) -> str:
    """Recent transcript as a readable dialogue. Trimmed to bound tokens."""
    recent = turns[-limit:]
    lines = []
    for t in recent:
        who = "INTERVIEWER" if t.role == "interviewer" else "CANDIDATE"
        lines.append(f"{who}: {t.content}")
    return "\n".join(lines) if lines else "(no prior turns)"


# ---------------------------------------------------------------------------
# Tolerant JSON parsing
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of an LLM JSON reply. Returns None on total failure."""
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


def _extract_feedback_score(
    data: Dict[str, Any], feedback: Dict[str, str], score: int,
) -> Tuple[Dict[str, str], int]:
    """Pull a normalized feedback dict + clamped score from a parsed reply,
    keeping the passed-in fallbacks for any missing field."""
    fb = data.get("feedback")
    if isinstance(fb, dict):
        feedback = {
            "worked": str(fb.get("worked", feedback["worked"]))[:400],
            "improve": str(fb.get("improve", feedback["improve"]))[:400],
            "stronger": str(fb.get("stronger", feedback["stronger"]))[:400],
        }
    try:
        score = max(0, min(100, int(float(data.get("score", score)))))
    except (TypeError, ValueError):
        pass
    return feedback, score


# ---------------------------------------------------------------------------
# Canned fallbacks (used only when the LLM is unavailable / unparseable)
# ---------------------------------------------------------------------------

_FALLBACK_OPENERS: Dict[str, str] = {
    "recruiter": "Thanks for joining! To start, walk me through your background and what's drawing you to this role.",
}
_FALLBACK_QUESTIONS: Dict[str, str] = {
    "recruiter": "Got it. What are your salary expectations and how soon could you start?",
    "behavioral": "Tell me about a time you faced a tough problem at work — what was the situation, what did you do, and how did it turn out?",
    "technical": "Let's go deeper on the craft. Walk me through how you'd approach a problem typical of this role, thinking out loud.",
    "company": "Why this role and this company specifically — and what questions do you have for us?",
}
_FALLBACK_FEEDBACK = {
    "worked": "You engaged with the question directly.",
    "improve": "Add a concrete example with a measurable result.",
    "stronger": "Frame it as Situation → Action → Result so the impact lands.",
}


# ---------------------------------------------------------------------------
# Engine API
# ---------------------------------------------------------------------------

def opening_message(profile_dict: Dict[str, Any], job: Job,
                    experience_items: Optional[List[ExperienceItem]] = None) -> str:
    """Generate the interviewer's opening greeting + first recruiter question."""
    bank = _format_experience_bank(experience_items, job)
    user_prompt = (
        f"{_job_block(job)}\n"
        f"{bank}\n\n"
        f"Start the interview now. Output JSON only."
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_OPENER_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=256,
            temperature=0.6,
        )
        data = _parse_json(raw) or {}
        msg = (data.get("next_message") or "").strip()
        if msg:
            return msg
    except Exception as e:  # noqa: BLE001 — must never 500
        print(f"[interview] opener LLM failed: {e}")
    return _FALLBACK_OPENERS["recruiter"]


def _questions_per_round(session: InterviewSession) -> int:
    try:
        return int((session.config or {}).get("questions_per_round", DEFAULT_QUESTIONS_PER_ROUND))
    except (TypeError, ValueError):
        return DEFAULT_QUESTIONS_PER_ROUND


def _candidate_count_in_round(turns: List[InterviewTurn], round_name: str) -> int:
    return sum(1 for t in turns if t.role == "candidate" and t.round == round_name)


def advance_round(current: str) -> Optional[str]:
    """Next round after `current`, or None if the interview is over."""
    try:
        idx = ROUND_ORDER.index(current)
    except ValueError:
        return None
    return ROUND_ORDER[idx + 1] if idx + 1 < len(ROUND_ORDER) else None


def process_answer(
    profile_dict: Dict[str, Any],
    job: Job,
    session: InterviewSession,
    prior_turns: List[InterviewTurn],
    candidate_answer: str,
    experience_items: Optional[List[ExperienceItem]] = None,
) -> Dict[str, Any]:
    """Produce inline feedback for `candidate_answer` and the next interviewer move.

    `prior_turns` is the transcript BEFORE this answer (interviewer/candidate
    alternating). Returns a dict:
      {
        "feedback": {...}, "score": int,
        "next_message": str | None,   # None when the interview is complete
        "next_round": str | None,     # the round the next question belongs to
        "complete": bool,
      }
    Round progression is deterministic; the LLM only supplies the words.
    Never raises.
    """
    current = session.current_round
    if current not in ROUND_ORDER:
        current = ROUND_ORDER[0]

    # Count this answer toward the current round's budget.
    answered_in_round = _candidate_count_in_round(prior_turns, current) + 1
    budget = _questions_per_round(session)
    round_done = answered_in_round >= budget

    next_round: Optional[str] = current
    complete = False
    if round_done:
        nxt = advance_round(current)
        if nxt is None:
            complete = True
            next_round = None
        else:
            next_round = nxt

    bank = _format_experience_bank(experience_items, job)
    transcript = _transcript_block(prior_turns)

    if complete:
        # Final answer of the final round: only need feedback on it.
        feedback, score = _feedback_only(
            profile_dict, job, current, transcript, candidate_answer, bank
        )
        return {
            "feedback": feedback, "score": score,
            "next_message": None, "next_round": None, "complete": True,
        }

    # Need feedback on the answer + the next question for `next_round`.
    user_prompt = (
        f"{_job_block(job)}\n"
        f"{bank}\n\n"
        f"TRANSCRIPT SO FAR:\n{transcript}\n\n"
        f"CANDIDATE'S LATEST ANSWER:\n{candidate_answer or '(no answer given)'}\n\n"
        f"The NEXT question must belong to the '{ROUND_LABELS.get(next_round, next_round)}' "
        f"round. Focus for that round: {ROUND_FOCUS.get(next_round, '')}\n\n"
        f"First give feedback on the latest answer, then ask the next question. "
        f"Output JSON only."
    )
    feedback = dict(_FALLBACK_FEEDBACK)
    score = 60
    next_message = _FALLBACK_QUESTIONS.get(next_round, _FALLBACK_QUESTIONS["behavioral"])
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_INTERVIEWER_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=512,
            temperature=0.6,
        )
        data = _parse_json(raw)
        if data:
            feedback, score = _extract_feedback_score(data, feedback, score)
            msg = (data.get("next_message") or "").strip()
            if msg:
                next_message = msg
    except Exception as e:  # noqa: BLE001
        print(f"[interview] process_answer LLM failed: {e}")

    return {
        "feedback": feedback, "score": score,
        "next_message": next_message, "next_round": next_round, "complete": False,
    }


def _feedback_only(
    profile_dict: Dict[str, Any], job: Job, round_name: str,
    transcript: str, candidate_answer: str, bank: str,
) -> Tuple[Dict[str, str], int]:
    """Feedback on the final answer (no next question)."""
    user_prompt = (
        f"{_job_block(job)}\n"
        f"{bank}\n\n"
        f"TRANSCRIPT SO FAR:\n{transcript}\n\n"
        f"CANDIDATE'S FINAL ANSWER (round: {round_name}):\n{candidate_answer or '(no answer given)'}\n\n"
        f"This is the last answer of the interview. Give feedback on it ONLY — "
        f"set next_message to an empty string. Output JSON only."
    )
    feedback = dict(_FALLBACK_FEEDBACK)
    score = 60
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_INTERVIEWER_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=384,
            temperature=0.5,
        )
        data = _parse_json(raw)
        if data:
            feedback, score = _extract_feedback_score(data, feedback, score)
    except Exception as e:  # noqa: BLE001
        print(f"[interview] feedback_only LLM failed: {e}")
    return feedback, score


def build_debrief(
    profile_dict: Dict[str, Any],
    job: Job,
    turns: List[InterviewTurn],
) -> Dict[str, Any]:
    """Full end-of-interview debrief: per-round + overall scores, strengths,
    gaps, and concrete better answers. Falls back to a score derived from the
    per-answer feedback when the LLM is unavailable. Never raises.
    """
    # Build a transcript that includes the per-answer scores we already have.
    lines = []
    for t in turns:
        who = "INTERVIEWER" if t.role == "interviewer" else "CANDIDATE"
        extra = ""
        if t.role == "candidate" and t.feedback and "score" in (t.feedback or {}):
            extra = f" [answer score: {t.feedback['score']}]"
        lines.append(f"[{t.round}] {who}: {t.content}{extra}")
    transcript = "\n".join(lines) if lines else "(empty interview)"

    user_prompt = (
        f"{_job_block(job)}\n\n"
        f"FULL TRANSCRIPT (with per-answer scores):\n{transcript}\n\n"
        f"Write the debrief. Output JSON only."
    )
    try:
        raw = llm.complete_with_cached_profile(
            profile=profile_dict,
            instructions=_DEBRIEF_INSTRUCTIONS,
            user_prompt=user_prompt,
            model=DEFAULT_MATCH_MODEL,
            max_tokens=900,
            temperature=0.4,
        )
        data = _parse_json(raw)
        if data and isinstance(data, dict):
            return _normalize_debrief(data, turns)
    except Exception as e:  # noqa: BLE001
        print(f"[interview] debrief LLM failed: {e}")
    return _fallback_debrief(turns)


def _normalize_debrief(data: Dict[str, Any], turns: List[InterviewTurn]) -> Dict[str, Any]:
    """Coerce a parsed debrief into the shape the template expects, filling gaps."""
    fallback = _fallback_debrief(turns)
    out: Dict[str, Any] = {}
    try:
        out["overall_score"] = max(0, min(100, int(float(data.get("overall_score", fallback["overall_score"])))))
    except (TypeError, ValueError):
        out["overall_score"] = fallback["overall_score"]
    out["summary"] = str(data.get("summary") or fallback["summary"])[:1000]

    rs = data.get("round_scores")
    norm_rs = []
    if isinstance(rs, list):
        for r in rs:
            if not isinstance(r, dict):
                continue
            try:
                sc = max(0, min(100, int(float(r.get("score", 0)))))
            except (TypeError, ValueError):
                sc = 0
            rnd = str(r.get("round", ""))
            norm_rs.append({"round": rnd, "label": ROUND_LABELS.get(rnd, rnd),
                            "score": sc, "note": str(r.get("note", ""))[:300]})
    out["round_scores"] = norm_rs or fallback["round_scores"]

    out["strengths"] = [str(x)[:300] for x in (data.get("strengths") or [])][:6] or fallback["strengths"]
    out["gaps"] = [str(x)[:300] for x in (data.get("gaps") or [])][:6] or fallback["gaps"]

    ba = []
    if isinstance(data.get("better_answers"), list):
        for item in data["better_answers"]:
            if isinstance(item, dict):
                ba.append({"question": str(item.get("question", ""))[:400],
                           "reframe": str(item.get("reframe", ""))[:800]})
    out["better_answers"] = ba[:6]
    return out


def _fallback_debrief(turns: List[InterviewTurn]) -> Dict[str, Any]:
    """Deterministic debrief from the per-answer scores already captured."""
    by_round: Dict[str, List[int]] = {}
    all_scores: List[int] = []
    for t in turns:
        if t.role == "candidate" and t.feedback and "score" in (t.feedback or {}):
            sc = int(t.feedback["score"])
            by_round.setdefault(t.round, []).append(sc)
            all_scores.append(sc)
    overall = round(sum(all_scores) / len(all_scores)) if all_scores else 60
    round_scores = []
    for rnd in ROUND_ORDER:
        scs = by_round.get(rnd)
        if scs:
            round_scores.append({
                "round": rnd, "label": ROUND_LABELS.get(rnd, rnd),
                "score": round(sum(scs) / len(scs)),
                "note": "Averaged from your per-answer scores.",
            })
    if not round_scores:
        round_scores = [{"round": "recruiter", "label": ROUND_LABELS["recruiter"],
                         "score": overall, "note": "Limited data."}]
    return {
        "overall_score": overall,
        "summary": ("Debrief generated from your per-answer coaching scores "
                    "(the AI summarizer was unavailable). Review the inline notes "
                    "above for specifics on each answer."),
        "round_scores": round_scores,
        "strengths": ["You completed the full interview across all rounds.",
                      "You engaged with each question directly."],
        "gaps": ["Add more concrete, measurable results to your examples.",
                 "Use the STAR structure to keep answers tight and high-impact."],
        "better_answers": [],
    }
