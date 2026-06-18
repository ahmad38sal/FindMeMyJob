#!/usr/bin/env python3
"""Seed a target database (e.g. Railway Postgres) from an old SQLite snapshot.

Usage:
    DATABASE_URL="postgresql+psycopg://..."  \
    SQLITE_PATH="/path/to/findmemyjob.db"     \
    python scripts/seed_from_sqlite.py

- Reads all rows from the old SQLite DB using raw sqlite3 (no schema assumptions
  beyond column names, which are mapped to the current SQLModel models).
- Writes them into the target DB via the app's SQLModel models, so the target
  schema is created/validated automatically.
- Preserves primary keys so foreign keys (application.job_id, etc.) stay intact.
- Idempotent-ish: refuses to run if the target already has data, unless
  ALLOW_NONEMPTY=1 is set (then it upserts by primary key).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

# Make the app package importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402
from findmemyjob.models import Application, Job, Profile, Resume  # noqa: E402

SQLITE_PATH = os.environ.get("SQLITE_PATH", "/home/user/workspace/seed/findmemyjob.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOW_NONEMPTY = os.environ.get("ALLOW_NONEMPTY") == "1"

if not DATABASE_URL:
    sys.exit("ERROR: set DATABASE_URL (the Railway Postgres connection string).")

# Normalize Railway's postgres:// to the psycopg driver form
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

JSON_FIELDS = {
    "profile": {"contact", "work_history", "education", "skills",
                "certifications", "preferences"},
    "job": {"raw"},
    "application": {"gaps", "keywords_targeted"},
    "resume": {"diff_from_master", "keywords_targeted"},
}
DT_FIELDS = {"updated_at", "posted_at", "expires_at", "fetched_at",
             "submitted_at", "last_status_change", "created_at"}


def coerce(table: str, row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if v is not None and k in JSON_FIELDS.get(table, set()) and isinstance(v, str):
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
        if v is not None and k in DT_FIELDS and isinstance(v, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    v = datetime.strptime(v, fmt)
                    break
                except ValueError:
                    continue
        out[k] = v
    return out


def load_sqlite(table: str) -> list[dict]:
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    rows = [coerce(table, dict(r)) for r in con.execute(f'SELECT * FROM "{table}"')]
    con.close()
    return rows


def main() -> None:
    print(f"Source SQLite : {SQLITE_PATH}")
    print(f"Target DB     : {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SQLModel.metadata.create_all(engine)

    # Order matters for FKs: profile, job, resume, application
    plan = [("profile", Profile), ("job", Job),
            ("resume", Resume), ("application", Application)]

    with Session(engine) as s:
        existing = s.exec(select(Job)).first()
        if existing and not ALLOW_NONEMPTY:
            sys.exit("Target already has data. Re-run with ALLOW_NONEMPTY=1 to upsert.")

        for table, Model in plan:
            rows = load_sqlite(table)
            n = 0
            for row in rows:
                obj = s.get(Model, row.get("id")) if row.get("id") is not None else None
                if obj:
                    for k, v in row.items():
                        setattr(obj, k, v)
                else:
                    s.add(Model(**row))
                n += 1
            s.commit()
            print(f"  {table}: {n} rows seeded")

    print("Done. Verify in the app at /jobs and /profile.")


if __name__ == "__main__":
    main()
