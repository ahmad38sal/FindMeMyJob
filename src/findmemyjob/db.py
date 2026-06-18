"""Database engine + session helpers.

Uses DATABASE_URL env when set (Railway Postgres). Normalises
``postgres://`` -> ``postgresql+psycopg://``.  Falls back to SQLite at
``settings.db_path`` when DATABASE_URL is unset.
"""
from __future__ import annotations

import logging
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine

from findmemyjob.config import settings

logger = logging.getLogger(__name__)


def _build_engine():
    db_url = settings.database_url

    if db_url:
        # Railway (and some older Postgres providers) emit postgres:// which
        # SQLAlchemy 2.x / psycopg3 no longer accepts.
        if db_url.startswith("postgres://"):
            db_url = "postgresql+psycopg" + db_url[len("postgres"):]
        elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
            db_url = "postgresql+psycopg" + db_url[len("postgresql"):]

        logger.info("Using Postgres database: %s", db_url.split("@")[-1])
        return create_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
        )

    # SQLite fallback
    sqlite_url = f"sqlite:///{settings.db_path}"
    logger.info("Using SQLite database: %s", settings.db_path)
    return create_engine(
        sqlite_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )


engine = _build_engine()


def init_db() -> None:
    """Import models then create tables. Idempotent."""
    try:
        from findmemyjob import models  # noqa: F401  — registers tables on metadata
        SQLModel.metadata.create_all(engine)
    except Exception as exc:
        logger.error("DB init failed: %s", exc)
        raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session
