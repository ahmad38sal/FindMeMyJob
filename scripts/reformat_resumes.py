"""Deterministic, LLM-FREE cleanup of existing tailored resumes.

Re-runs the resume best-practice formatting pass (findmemyjob.resume_format)
over every tailored ``Resume.content`` row: caps bullets per role by
recency/relevance, tightens over-long bullets, and turns skills into short,
deduped, categorized tags. It then regenerates the PDF for any row whose
content actually changed (or whose PDF is missing).

This calls NO language model — it is pure heuristic normalization, so it is
cheap and safe to run repeatedly. The formatting pass is idempotent, so a
second run finds nothing to change.

Whenever a row's content changes, its ``content_hash`` is updated and — if no
fresh PDF is rendered in this run (``--no-pdf`` or a render failure) — the stale
``pdf_path`` is cleared, so the next download regenerates rather than serving an
out-of-date PDF.

Content is always written back as a real dict (never a JSON string), reusing
``_as_content_dict`` to tolerate legacy string rows.

Usage:
    # Local SQLite (default):
    python scripts/reformat_resumes.py [--dry-run] [--no-pdf]

    # Against live Postgres (run only after review):
    DATABASE_URL=postgresql://...  python scripts/reformat_resumes.py --dry-run
"""
from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from findmemyjob.db import engine
from findmemyjob.models import Job, Resume, ResumeKind
from findmemyjob.resume_format import format_resume_content, resume_content_hash
from findmemyjob.routes.jobs import _as_content_dict


def _job_text(session: Session, job_id) -> str:
    if not job_id:
        return ""
    job = session.get(Job, job_id)
    if not job:
        return ""
    return f"{job.title or ''} {job.description or ''}"


def _regen_pdf(session: Session, resume: Resume, content: dict) -> bool:
    """Render a fresh PDF from the cleaned content. Returns True on success."""
    # Imported lazily so --no-pdf / import-only environments don't need Playwright.
    from findmemyjob.pdf import save_resume_pdf
    from findmemyjob.routes.jobs import _profile_dict

    profile = _profile_dict(session)
    job = session.get(Job, resume.job_id) if resume.job_id else None
    company = (job.company if job else None) or "resume"
    try:
        pdf_path = save_resume_pdf(
            contact=profile.get("contact") or {},
            summary=content.get("summary") or "",
            work_history=content.get("work_history") or [],
            education=content.get("education") or profile.get("education") or [],
            skills=content.get("skills") or [],
            certifications=profile.get("certifications") or [],
            filename_hint=f"job-{resume.job_id}-{company}-reformatted",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] resume id={resume.id}: PDF regen failed: {type(e).__name__}: {e}")
        return False
    resume.pdf_path = str(pdf_path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--no-pdf", action="store_true", help="rewrite content but skip PDF regen")
    args = ap.parse_args()

    changed = regenerated = skipped = 0
    with Session(engine) as session:
        resumes = session.exec(
            select(Resume).where(Resume.kind == ResumeKind.tailored)
        ).all()
        print(f"Scanning {len(resumes)} tailored resume(s)...")

        for r in resumes:
            content = _as_content_dict(r.content)
            if not content:
                skipped += 1
                continue
            formatted = format_resume_content(
                content, job_text=_job_text(session, r.job_id),
                page_length=r.page_length or "auto",
            )
            content_changed = formatted != content
            if not content_changed:
                skipped += 1
                continue

            changed += 1
            print(f"[change] resume id={r.id} (job {r.job_id}): content reformatted")
            if args.dry_run:
                continue

            r.content = formatted  # real dict; JSON column serializes it
            r.content_hash = resume_content_hash(formatted)
            if not args.no_pdf and _regen_pdf(session, r, formatted):
                regenerated += 1
            else:
                # No fresh PDF was rendered now (either --no-pdf or a render
                # failure). Drop the stale cached path so the next download
                # regenerates from the new content instead of serving the old file.
                r.pdf_path = None
            session.add(r)

        if args.dry_run:
            print(f"\nDRY RUN — would reformat {changed} row(s); {skipped} already clean.")
        else:
            session.commit()
            print(f"\nReformatted {changed} row(s); regenerated {regenerated} PDF(s); "
                  f"{skipped} already clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
