"""Resume PDF rendering — Playwright + Chromium print-to-PDF.

Reuses the headless browser we already have for job scraping. The template
(`templates/resume.html`) is a single-column ATS-friendly layout — semantic
HTML, no tables for layout, no graphics, real text everywhere.

Public functions:
  render_resume_pdf(contact, summary, work_history, education, skills,
                    certifications) -> bytes
  save_resume_pdf(...args, out_path: Path) -> Path
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader

from findmemyjob.config import settings

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)


def _render_html(
    *,
    contact: Dict[str, Any],
    summary: str = "",
    work_history: Optional[List[Dict[str, Any]]] = None,
    education: Optional[List[Dict[str, Any]]] = None,
    skills: Optional[List[Dict[str, Any]]] = None,
    certifications: Optional[List[Dict[str, Any]]] = None,
) -> str:
    template = _jinja.get_template("resume.html")
    return template.render(
        contact=contact or {},
        summary=summary or "",
        work_history=work_history or [],
        education=education or [],
        skills=skills or [],
        certifications=certifications or [],
    )


def render_resume_pdf(
    *,
    contact: Dict[str, Any],
    summary: str = "",
    work_history: Optional[List[Dict[str, Any]]] = None,
    education: Optional[List[Dict[str, Any]]] = None,
    skills: Optional[List[Dict[str, Any]]] = None,
    certifications: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    from playwright.sync_api import sync_playwright

    html = _render_html(
        contact=contact, summary=summary, work_history=work_history,
        education=education, skills=skills, certifications=certifications,
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        pdf_bytes = page.pdf(
            format="Letter",
            print_background=True,
            margin={"top": "0in", "right": "0in", "bottom": "0in", "left": "0in"},
        )
        browser.close()
    return pdf_bytes


def save_resume_pdf(
    *,
    contact: Dict[str, Any],
    summary: str = "",
    work_history: Optional[List[Dict[str, Any]]] = None,
    education: Optional[List[Dict[str, Any]]] = None,
    skills: Optional[List[Dict[str, Any]]] = None,
    certifications: Optional[List[Dict[str, Any]]] = None,
    filename_hint: str = "resume",
) -> Path:
    pdf_bytes = render_resume_pdf(
        contact=contact, summary=summary, work_history=work_history,
        education=education, skills=skills, certifications=certifications,
    )
    settings.resumes_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in filename_hint)[:60]
    out = settings.resumes_dir / f"{safe}-{ts}.pdf"
    out.write_bytes(pdf_bytes)
    return out
