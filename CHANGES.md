# FindMeMyJob — Rebuild Changes

## Summary

Complete rebuild per `REBUILD_SPEC.md`. All 7 spec sections implemented. Smoke tests pass.

---

## Files changed / added

### 1. `src/findmemyjob/llm.py` — **Rewritten completely**
- Removed all Apple Floodgate / `get-apple-token.sh` / `bundle.pem` / `httpx` code.
- New provider-agnostic client: OpenAI (default `gpt-4o-mini`), Anthropic, Gemini.
- Config via env: `LLM_PROVIDER`, `LLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`, `LLM_MODEL`, `LLM_MATCH_MODEL`, `LLM_TAILOR_MODEL`.
- Preserved public interface: `llm.complete()`, `llm.complete_with_cached_profile()`, `DEFAULT_MATCH_MODEL`, `DEFAULT_TAILOR_MODEL`.
- Added `async def acomplete()` and `async def acomplete_with_cached_profile()` for async hot paths.
- OpenAI: flattens Anthropic-style system blocks into a single system string; uses Chat Completions API; no global `response_format=json_object` (cover letters return prose).
- Anthropic: passes system blocks + `cache_control` natively.
- Gemini: sync SDK wrapped in `asyncio.to_thread`.
- Raises `RuntimeError` naming the missing env var if no API key is configured.
- `_strip_code_fence` moved here and re-exported (callers import it from `llm`).

### 2. `src/findmemyjob/config.py` — **Updated**
- `data_dir` now defaults from `DATA_DIR` env var (Railway volume mount path).
- Added `database_url: Optional[str]` field reading `DATABASE_URL` env var.

### 3. `src/findmemyjob/db.py` — **Rewritten**
- Builds engine from `DATABASE_URL` when set.
- Normalises `postgres://` → `postgresql+psycopg://` for psycopg3 compatibility.
- Adds `pool_pre_ping=True` for Postgres to recover dropped connections.
- Falls back to SQLite (`settings.db_path`) when `DATABASE_URL` is unset.
- Only passes `check_same_thread=False` for SQLite (not Postgres).
- `init_db()` logs and re-raises on failure for clear startup errors.

### 4. `src/findmemyjob/models.py` — **Updated**
- Added `__table_args__` composite index `ix_job_source_source_id` on `Job(source, source_id)` to speed dedup lookups. Added `Index` import from sqlalchemy.

### 5. `src/findmemyjob/matching.py` — **Updated**
- Imports `_strip_code_fence` from `findmemyjob.llm`.
- Added `score_jobs_bulk(profile_dict, jobs, concurrency=5)` — async bulk scorer using `asyncio.Semaphore(5)` and `llm.acomplete_with_cached_profile`.
- Extracted `_build_user_prompt()` helper (DRY between sync and async paths).

### 6. `src/findmemyjob/routes/jobs.py` — **Updated**
- Added `POST /score-all` route: scores all unscored jobs concurrently via `score_jobs_bulk`, creates/updates `Application` rows, redirects to `/jobs`.
- Fixed all `TemplateResponse` calls to use new Starlette 1.x signature `(request, name, context)`.

### 7. `src/findmemyjob/routes/profile.py` — **Updated**
- Added `POST /profile/apple-session/upload` route: accepts `UploadFile`, saves to `settings.data_dir / "apple_session.json"`.
- Fixed `TemplateResponse` call to new Starlette 1.x signature.

### 8. `src/findmemyjob/routes/home.py` — **Updated**
- Fixed `TemplateResponse` call to new Starlette 1.x signature.

### 9. `src/findmemyjob/routes/applications.py` — **Updated**
- Fixed `TemplateResponse` call to new Starlette 1.x signature.

### 10. `src/findmemyjob/templates/profile.html` — **Updated**
- Added "Apple internal — session upload" panel with `<form>` posting to `/profile/apple-session/upload`.

### 11. `src/findmemyjob/templates/jobs.html` — **Updated**
- Added "Score all" button posting to `/jobs/score-all`.

### 12. `pyproject.toml` — **Updated**
- Removed `[[tool.uv.index]]` Apple PyPI block (`https://pypi.apple.com/simple`).
- Added deps: `openai>=1.0`, `psycopg[binary]>=3.2`.
- Added optional extras: `[anthropic]`, `[gemini]`.
- Set `requires-python = ">=3.11"`.
- Updated description to reflect multi-source tool.
- Bumped version to `0.2.0`.

### 13. `.gitignore` — **Updated**
- Filled the empty `# Local data` section with `data/` to untrack the entire data directory.

### 14. `README.md` — **Rewritten** (was empty)
- Full documentation: setup, env vars table, local run, Railway deploy, architecture overview.

### 15. `Dockerfile` — **New**
- `python:3.11-slim` base; installs build deps, `uv`; runs `uv sync`; installs Playwright Chromium with deps; sets `PATH` to `.venv/bin`; `CMD uvicorn findmemyjob.main:app --host 0.0.0.0 --port $PORT`.

### 16. `railway.json` — **New**
- Builder: `DOCKERFILE`; start command; healthcheck path `/`; restart policy.

### 17. `Procfile` — **New**
- `web: uvicorn findmemyjob.main:app --host 0.0.0.0 --port $PORT`

### 18. `.env.example` — **Rewritten**
- Documents all env vars: `LLM_PROVIDER`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `LLM_MODEL`, `LLM_MATCH_MODEL`, `LLM_TAILOR_MODEL`, `DATABASE_URL`, `DATA_DIR`, `FINDMEMYJOB_EXT_TOKEN`, `APPLE_INTERNAL_CAREERS_URL`.

### 19. `smoke_test.py` — **New**
- Standalone smoke test script; sets env, calls `init_db()`, hits `/`, `/profile`, `/jobs`, `/applications` with `TestClient` without making any real LLM API calls.

### 20. Git tracking
- `git rm -r --cached data` — untracked the entire `data/` directory (DB, resumes, Apple debug HTML, session JSON).

---

## Smoke test result

```
Data dir: /tmp/fmj_smoke_<tmpdir>
Testing FindMeMyJob ...
  OK  GET / -> 200
  OK  GET /profile -> 200
  OK  GET /jobs -> 200
  OK  GET /applications -> 200

All smoke tests passed.
```

Uvicorn boots cleanly with SQLite + `OPENAI_API_KEY=dummy` (no real LLM API calls made).

---

## What was NOT changed

- Source files (`sources/apple_internal.py`, `greenhouse.py`, `lever.py`, etc.) — untouched. Apple internal scraping logic preserved; session upload route added.
- `tailoring.py`, `salary.py`, `importing.py`, `search_strategy.py`, `pdf.py` — these call `llm.complete_with_cached_profile` / `llm.complete` which still work unchanged.
- `routes/ext.py` — Chrome extension API untouched. Imports `llm` and `_strip_code_fence` from `findmemyjob.llm`; both still importable.
- Jinja2 templates other than `profile.html` and `jobs.html` — not modified (only `TemplateResponse` call-site signature updated in Python routes).
- `main.py`, `__init__.py` — no changes needed.
