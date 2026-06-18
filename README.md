# FindMeMyJob

A personal job-search and auto-apply FastAPI app. Features:

- **Multi-source job discovery**: Apple internal careers (Playwright), Greenhouse, Lever, RemoteOK, HN Who's Hiring, any job URL
- **LLM-powered matching**: score each job against your structured profile with configurable stretch slider
- **Async bulk scoring**: score all unscored jobs concurrently in one click
- **Resume tailoring + cover letters**: ATS-keyword-optimised PDF resumes, per-job cover letters
- **Chrome extension**: surface match score and auto-fill application forms
- **Deployable to Railway**: Postgres + persistent volume + Playwright Chromium

---

## Local setup

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

```bash
# Clone and enter the repo
git clone <repo-url>
cd FindMeMyJob

# Install deps (uv)
uv sync

# — or plain pip —
pip install -e .

# Install Playwright browser (needed for PDF rendering and Apple internal source)
playwright install --with-deps chromium
```

### Environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | No | `openai` (default) \| `anthropic` \| `gemini` |
| `OPENAI_API_KEY` | Yes (if provider=openai) | OpenAI API key |
| `ANTHROPIC_API_KEY` | Yes (if provider=anthropic) | Anthropic API key |
| `GEMINI_API_KEY` | Yes (if provider=gemini) | Google Gemini API key |
| `LLM_MODEL` | No | Override default model (e.g. `gpt-4o`, `claude-3-5-sonnet-20241022`) |
| `LLM_MATCH_MODEL` | No | Model for job scoring (falls back to `LLM_MODEL`) |
| `LLM_TAILOR_MODEL` | No | Model for tailoring/cover letters (falls back to `LLM_MODEL`) |
| `DATABASE_URL` | No | Postgres URL (Railway sets this). Omit for local SQLite |
| `DATA_DIR` | No | Path for DB, resumes, session files (default: `./data`) |
| `FINDMEMYJOB_EXT_TOKEN` | No | Bearer token for Chrome extension API (`/api/ext/*`) |
| `APPLE_INTERNAL_CAREERS_URL` | No | Custom Apple careers search URL |

### Run locally

```bash
uvicorn findmemyjob.main:app --reload
# or
uv run python -m findmemyjob.main
```

Open http://127.0.0.1:8000

---

## Usage

1. **Profile** — visit `/profile`, upload your resume PDF to parse it, or edit the JSON directly.
2. **Jobs** — visit `/jobs`, hit **Refresh from sources** to pull listings, then **Score all** to batch-score against your profile.
3. **Job detail** — click any job to see the match score, gaps, and tailoring options.
4. **Apple internal** — upload your `apple_session.json` (Playwright storage state) at `/profile` to enable headless scraping.

---

## Railway deploy

### One-click

1. Fork this repo, connect it to Railway.
2. Add a Postgres plugin → Railway auto-sets `DATABASE_URL`.
3. Add a Volume mounted at `/data` → set `DATA_DIR=/data`.
4. Set the LLM env vars in Railway's Variables panel.
5. Railway builds via the `Dockerfile` and starts uvicorn on `$PORT`.

### Manual

```bash
railway up
```

The `Dockerfile` installs Python 3.11, all deps, and Playwright Chromium for PDF rendering and the Apple internal scraper.

---

## Architecture

```
src/findmemyjob/
  main.py            FastAPI app, route wiring, startup hook
  config.py          Pydantic Settings (reads env vars / .env)
  db.py              SQLAlchemy/SQLModel engine; Postgres or SQLite
  llm.py             Provider-agnostic LLM client (OpenAI / Anthropic / Gemini)
  models.py          SQLModel tables: Profile, Job, Application, Resume
  matching.py        Prefilter + LLM scoring; async bulk via score_jobs_bulk
  tailoring.py       Resume tailoring + cover letter generation
  salary.py          LLM salary estimation
  importing.py       PDF/DOCX resume parser
  search_strategy.py LLM-suggested search queries
  pdf.py             Playwright print-to-PDF for resume.html
  sources/           Job scrapers: apple_internal, greenhouse, lever, remoteok, hn_whoishiring, generic_url
  routes/            FastAPI routers: home, profile, jobs, applications, ext
  templates/         Jinja2 + HTMX HTML templates
  static/            CSS
```
