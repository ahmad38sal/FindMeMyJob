"""Database engine + session helpers.

Uses DATABASE_URL env when set (Railway Postgres). Normalises
``postgres://`` -> ``postgresql+psycopg://``.  Falls back to SQLite at
``settings.db_path`` when DATABASE_URL is unset.
"""
from __future__ import annotations

import logging
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from findmemyjob.config import settings

logger = logging.getLogger(__name__)

# Columns added after the initial schema. create_all() never ALTERs an existing
# table, so these additive, nullable columns are applied by hand on startup —
# safe on both live Postgres and local SQLite (idempotent: skipped if present).
_JOB_ADDITIVE_COLUMNS = {
    "discovered_at": "TIMESTAMP",
    "fit_score": "DOUBLE PRECISION",
    "fit_reasoning": "TEXT",
    "fit_gaps": "JSON",
    "undated": "BOOLEAN",
}

# New per-resume tailor options. Additive + nullable; defaults match prior behavior
# so old rows read as summary-on / automatic length.
_RESUME_ADDITIVE_COLUMNS = {
    "include_summary": "BOOLEAN",
    "page_length": "TEXT",
    "manually_edited": "BOOLEAN",
}

_ADDITIVE_COLUMNS = {
    "job": _JOB_ADDITIVE_COLUMNS,
    "resume": _RESUME_ADDITIVE_COLUMNS,
}


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


def _apply_additive_columns() -> None:
    """Add new nullable columns to pre-existing tables (no destructive DDL).

    SQLite and Postgres both accept ``ALTER TABLE ... ADD COLUMN`` for nullable
    columns. We only add what's missing, so this is safe to run on every boot.
    """
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    is_sqlite = engine.dialect.name == "sqlite"
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in table_names:
            continue  # create_all just made it with all columns — nothing to backfill
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, sql_type in columns.items():
                if name in existing:
                    continue
                col_type = "JSON" if (sql_type == "JSON" and is_sqlite) else sql_type
                try:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {col_type}'))
                    logger.info("Added %s.%s column", table, name)
                except Exception as exc:  # noqa: BLE001 - race/older engine; non-fatal
                    logger.warning("Could not add %s.%s (%s): %s", table, name, col_type, exc)


def init_db() -> None:
    """Import models then create tables. Idempotent."""
    try:
        from findmemyjob import models  # noqa: F401  — registers tables on metadata
        SQLModel.metadata.create_all(engine)
        _apply_additive_columns()
    except Exception as exc:
        logger.error("DB init failed: %s", exc)
        raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session
