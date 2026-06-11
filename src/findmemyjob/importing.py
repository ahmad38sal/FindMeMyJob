"""Resume importer — turn an existing PDF/docx resume into a structured Profile.

Strategy: extract raw text with pypdf / python-docx, then ask Claude to
structure it. The user reviews + edits in the UI before the profile is saved.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from pypdf import PdfReader
from docx import Document

from findmemyjob.llm import DEFAULT_TAILOR_MODEL, llm
from findmemyjob.matching import _strip_code_fence


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix in {".docx", ".doc"}:
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix in {".txt", ".md"}:
        return path.read_text()
    raise ValueError(f"Unsupported resume format: {suffix}")


_IMPORT_INSTRUCTIONS = """\
You convert raw resume text into a structured JSON profile. Be conservative —
if a field isn't clearly present, leave it null/empty rather than guessing.

Output STRICT JSON (no commentary, no markdown):
{
  "contact": {"name": "...", "email": "...", "phone": "...", "location": "...",
              "linkedin": "...", "github": "...", "portfolio": "..."},
  "summary": "the 'about me' / objective paragraph if present",
  "work_history": [
    {"company": "...", "title": "...", "location": "...",
     "start": "YYYY-MM-DD or YYYY-MM or null",
     "end":   "YYYY-MM-DD or YYYY-MM or null (null = current)",
     "bullets": ["...", ...], "skills": ["...", ...]}
  ],
  "education": [{"school": "...", "degree": "...", "field": "...",
                 "start": "...", "end": "...", "gpa": null,
                 "highlights": [...]}],
  "skills": [{"name": "Python", "category": "language", "years": null}],
  "certifications": [{"name": "...", "issuer": "...", "date_earned": "..."}]
}
"""


def parse_resume_text(text: str) -> Dict[str, Any]:
    """Single LLM call to structure the resume. Returns dict shaped like Profile fields."""
    raw = llm.complete(
        system=[{"type": "text", "text": _IMPORT_INSTRUCTIONS}],
        messages=[{"role": "user", "content": f"RESUME TEXT:\n{text}\n\nReturn the JSON now."}],
        model=DEFAULT_TAILOR_MODEL,
        max_tokens=4096,
        temperature=0.1,
    )
    return json.loads(_strip_code_fence(raw))


def import_resume(path: Path) -> Dict[str, Any]:
    """End-to-end: file path → structured profile dict (NOT yet persisted)."""
    text = extract_text(path)
    return parse_resume_text(text)
