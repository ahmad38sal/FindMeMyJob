"""SQLite engine + session helpers."""
from __future__ import annotations

from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine

from findmemyjob.config import settings

# `check_same_thread=False` is fine for a single-process FastAPI app with
# SQLModel sessions; SQLAlchemy still serializes writes via its connection pool.
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Import models then create tables. Idempotent."""
    from findmemyjob import models  # noqa: F401  — register tables on the metadata
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session
