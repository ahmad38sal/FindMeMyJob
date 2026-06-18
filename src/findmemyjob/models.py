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
