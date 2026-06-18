"""FastAPI app entry — wires routes, templates, and DB init.

Run with:  uv run uvicorn findmemyjob.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

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


# ---------------------------------------------------------------------------
# Friendly error handling: never show a raw "Internal Server Error" to a user
# browsing the site. API (extension) routes still get JSON.
# ---------------------------------------------------------------------------

def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in (
        request.headers.get("accept", "")
    )


def _error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    try:
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {"status_code": status_code, "message": message},
            status_code=status_code,
        )
    except Exception:  # noqa: BLE001 - template missing/broken; plain fallback
        return HTMLResponse(
            f"<h1>{status_code}</h1><p>{message}</p>"
            '<p><a href="/">Back to home</a></p>',
            status_code=status_code,
        )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=exc.status_code)
    return _error_page(request, exc.status_code, detail)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Log full detail server-side; show a friendly message to the user.
    print(f"[error] unhandled {type(exc).__name__} on {request.url.path}: {exc}")
    if _wants_json(request):
        return JSONResponse(
            {"detail": "Internal server error"}, status_code=500
        )
    return _error_page(
        request,
        500,
        "Something went wrong on our end. Please try again — if it keeps "
        "happening, the AI model may be busy.",
    )


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
