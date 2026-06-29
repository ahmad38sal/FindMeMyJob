"""Data models for FindMeMyJob.

Profile/Resume content is stored as JSON in SQLite — the nested shape (work
history, skills, preferences) is easier to evolve without migrations, and a
personal tool doesn't benefit from normalized relational tables.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import JSON, Column, Index
from sqlmodel import SQLModel, Field as SQLField


# ---------------------------------------------------------------------------
# Nested structures (JSON-serialized inside Profile / Resume)
# ---------------------------------------------------------------------------

class WorkMode(str, Enum):
    remote = "remote"
    hybrid = "hybrid"
    onsite = "onsite"


class WorkExperience(BaseModel):
    company: str
    title: str
    location: Optional[str] = None
    start: date
    end: Optional[date] = None  # None = current
    bullets: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str
    degree: str
    field: Optional[str] = None
    start: Optional[date] = None
    end: Optional[date] = None
    gpa: Optional[float] = None
    highlights: List[str] = Field(default_factory=list)


class Skill(BaseModel):
    name: str
    category: Optional[str] = None  # e.g. "language", "framework", "tool"
    years: Optional[float] = None
    # Free-text claim from the user — "where/how I used this skill". Surfaced
    # to the matching LLM so user-asserted skills aren't re-flagged as gaps.
    evidence: Optional[str] = None


class Certification(BaseModel):
    name: str
    issuer: str
    date_earned: Optional[date] = None
    expires: Optional[date] = None


class Preferences(BaseModel):
    salary_min: Optional[int] = None
    salary_target: Optional[int] = None
    currency: str = "USD"
    locations: List[str] = Field(default_factory=list)
    work_modes: List[WorkMode] = Field(default_factory=list)
    seniority_levels: List[str] = Field(default_factory=list)
    industries: List[str] = Field(default_factory=list)
    exclude_companies: List[str] = Field(default_factory=list)
    # 0 = only solid matches; 100 = include big stretches I'd need to grow into
    stretch_slider: int = 30


class ContactInfo(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None


# ---------------------------------------------------------------------------
# DB tables
# ---------------------------------------------------------------------------

class Profile(SQLModel, table=True):
    """Singleton — there's only ever one row (id=1) for the user."""
    id: Optional[int] = SQLField(default=1, primary_key=True)
    contact: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))
    summary: str = ""
    work_history: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))
    education: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))
    skills: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))
    certifications: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))
    preferences: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class Job(SQLModel, table=True):
    """Normalized job posting from any source."""
    __table_args__ = (
        Index("ix_job_source_source_id", "source", "source_id"),
    )

    id: Optional[int] = SQLField(default=None, primary_key=True)
    source: str = SQLField(index=True)              # apple_internal, greenhouse, lever, ...
    source_id: str = SQLField(index=True)           # the source's own id, for dedup
    title: str
    company: str = SQLField(index=True)
    team: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    currency: str = "USD"
    seniority: Optional[str] = None
    description: str = ""
    url: str = ""
    posted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    fetched_at: datetime = SQLField(default_factory=datetime.utcnow)
    raw: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))

    # --- Discovery engine (additive, nullable) ---
    # Set by the discovery pipeline; existing rows simply stay None.
    discovered_at: Optional[datetime] = None        # first time discovery surfaced it
    fit_score: Optional[float] = None               # blended fit (0-100)
    fit_reasoning: Optional[str] = None
    fit_gaps: Optional[List[str]] = SQLField(default=None, sa_column=Column(JSON))
    undated: Optional[bool] = None                  # True when no posted_at available


class ExperienceItem(SQLModel, table=True):
    """A personal 'experience bank' note — the user's own rough words about a
    skill or experience, including things not on their resume.

    Stored RAW exactly as entered; never pre-polished. Tailoring reframes
    relevant items into resume language at tailor time. An item may optionally be
    linked to the Job it was added from (`job_id`), in which case tailoring for
    that job prioritizes it; all active items are general context otherwise.
    """
    id: Optional[int] = SQLField(default=None, primary_key=True)
    raw_text: str                                   # the user's rough note (required)
    label: Optional[str] = None                     # optional short title
    category: Optional[str] = None                  # optional free-text skill area
    job_id: Optional[int] = SQLField(default=None, foreign_key="job.id", index=True)
    active: bool = SQLField(default=True)            # include in tailoring when True
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class InterviewSession(SQLModel, table=True):
    """One mock-interview run for a specific job.

    Additive table (new in this release). Flows through round types in order:
    recruiter -> behavioral -> technical -> company -> (debrief). `config`
    holds the round plan + per-round question budget; `debrief` is the final
    summary written when the interview completes.
    """
    id: Optional[int] = SQLField(default=None, primary_key=True)
    job_id: int = SQLField(foreign_key="job.id", index=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    status: str = SQLField(default="active")          # active | completed
    current_round: str = SQLField(default="recruiter")  # recruiter|behavioral|technical|company
    config: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))
    debrief: Optional[Dict[str, Any]] = SQLField(default=None, sa_column=Column(JSON))


class InterviewTurn(SQLModel, table=True):
    """A single message in an interview transcript.

    `role` is "interviewer" or "candidate". For candidate turns, `feedback`
    holds the inline coaching note (what worked / improve / stronger reframe /
    score) produced right after the answer.
    """
    id: Optional[int] = SQLField(default=None, primary_key=True)
    session_id: int = SQLField(foreign_key="interviewsession.id", index=True)
    role: str                                          # interviewer | candidate
    round: str                                         # recruiter|behavioral|technical|company
    content: str = ""
    feedback: Optional[Dict[str, Any]] = SQLField(default=None, sa_column=Column(JSON))
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class SearchProfile(SQLModel, table=True):
    """Singleton (id=1) — the LLM-derived 'ideal role' search profile.

    Derived from the user's Profile, regenerable on demand. Stored so the
    discovery pipeline doesn't re-derive on every run.
    """
    id: Optional[int] = SQLField(default=1, primary_key=True)
    titles: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    keywords: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    seniority: Optional[str] = None
    remote_pref: Optional[str] = None               # remote | hybrid | onsite | any
    locations: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    salary_min: Optional[int] = None
    salary_target: Optional[int] = None
    currency: str = "USD"
    summary: str = ""                               # human-readable one-liner
    raw: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))
    generated_at: datetime = SQLField(default_factory=datetime.utcnow)


class DiscoveryRun(SQLModel, table=True):
    """One row per discovery-pipeline run — an audit trail + cron report source."""
    id: Optional[int] = SQLField(default=None, primary_key=True)
    started_at: datetime = SQLField(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    sources_used: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    fetched_count: int = 0
    new_count: int = 0          # brand-new jobs inserted this run
    scored_count: int = 0
    fresh_count: int = 0        # passed the freshness filter
    error: Optional[str] = None
    # The NEW top matches surfaced this run — what cron reports.
    # Each: {job_id, title, company, url, score, reasoning, posted_at, undated}
    top_matches: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))


class ApplicationStatus(str, Enum):
    pending = "pending"           # newly matched, not yet reviewed
    ready = "ready"               # tailored, awaiting confirm-or-submit
    submitted = "submitted"
    responded = "responded"       # any response from employer
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


class ApplyMode(str, Enum):
    auto = "auto"
    confirm = "confirm"
    manual = "manual"


class Application(SQLModel, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)
    job_id: int = SQLField(foreign_key="job.id", index=True)
    status: ApplicationStatus = SQLField(default=ApplicationStatus.pending)
    mode: ApplyMode = SQLField(default=ApplyMode.confirm)
    match_score: Optional[float] = None
    match_reasoning: str = ""
    gaps: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    stretch_required: bool = False
    tailored_resume_id: Optional[int] = SQLField(default=None, foreign_key="resume.id")
    cover_letter: str = ""
    submitted_at: Optional[datetime] = None
    last_status_change: datetime = SQLField(default_factory=datetime.utcnow)
    notes: str = ""


class ResumeKind(str, Enum):
    master = "master"
    tailored = "tailored"


class Resume(SQLModel, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)
    kind: ResumeKind = SQLField(default=ResumeKind.tailored, index=True)
    job_id: Optional[int] = SQLField(default=None, foreign_key="job.id", index=True)
    # Same shape as Profile sections, but post-tailoring (subset / reordered / rephrased).
    content: Dict[str, Any] = SQLField(default_factory=dict, sa_column=Column(JSON))
    pdf_path: Optional[str] = None
    # Bullet-by-bullet diff from master so the user can verify nothing was fabricated.
    diff_from_master: List[Dict[str, Any]] = SQLField(default_factory=list, sa_column=Column(JSON))
    keywords_targeted: List[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
