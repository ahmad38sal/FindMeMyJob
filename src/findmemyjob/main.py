"""FastAPI app entry — wires routes, templates, and DB init.

Run with:  uv run uvicorn findmemyjob.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from findmemyjob.config import settings
from findmemyjob.db import init_db
from findmemyjob.routes import applications, ext, home, jobs, profile

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

app = FastAPI(title="FindMeMyJob", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

# The MV3 extension calls /api/ext/* from chrome-extension://<id> origins.
# Chrome sends "Origin: chrome-extension://..." on these requests, so the
# regex matches any extension id without us hard-coding one.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# Share the templates instance with route modules.
app.state.templates = TEMPLATES

app.include_router(home.router)
app.include_router(profile.router, prefix="/profile", tags=["profile"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(applications.router, prefix="/applications", tags=["applications"])
app.include_router(ext.router, prefix="/api/ext", tags=["ext"])


def main() -> None:
    """`uv run python -m findmemyjob.main` to launch in development."""
    import uvicorn
    uvicorn.run(
        "findmemyjob.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    main()
