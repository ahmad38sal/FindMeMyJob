"""Configuration via environment variables.

Loads from .env if present (don't commit .env — see .gitignore).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Local data directory — on Railway, mount a volume and set DATA_DIR=/data
    data_dir: Path = Field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))

    # Postgres (Railway) or leave unset for SQLite fallback
    database_url: Optional[str] = Field(default=None, validation_alias="DATABASE_URL")

    # Apple internal careers
    apple_internal_careers_url: Optional[str] = None
    appleconnect_cookie_path: Optional[Path] = None

    # FastAPI dev server
    host: str = "127.0.0.1"
    port: int = 8000

    # Browser extension API (set FINDMEMYJOB_EXT_TOKEN in .env to enable /api/ext/*).
    # When unset, the extension router 503s every request — never wide-open.
    ext_token: Optional[str] = Field(default=None, validation_alias="FINDMEMYJOB_EXT_TOKEN")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "findmemyjob.db"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resumes_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
