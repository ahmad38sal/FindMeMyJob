"""One-time repair: re-sync Postgres auto-increment sequences after a bulk seed.

When rows are bulk-inserted with explicit primary-key values (as our SQLite->PG
migration did), Postgres does NOT advance the SERIAL/identity sequence. The next
natural insert then collides with an existing id (UniqueViolation on *_pkey).

This script sets each table's id sequence to MAX(id) so the next insert uses
MAX(id)+1. Safe to run multiple times. No-op for tables that are empty.

Usage:
    DATABASE_URL=postgresql://...  python scripts/fix_pg_sequences.py
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import text

# Reuse the app's engine so URL normalization (postgres:// -> psycopg) matches.
from findmemyjob.db import engine

TABLES = ["job", "application", "profile", "resume"]


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL not set; nothing to do (SQLite needs no sequence fix).")
        return 0

    with engine.begin() as conn:
        for table in TABLES:
            # pg_get_serial_sequence resolves the sequence name for table.id.
            seq_row = conn.execute(
                text("SELECT pg_get_serial_sequence(:t, 'id')"),
                {"t": table},
            ).first()
            seq = seq_row[0] if seq_row else None
            if not seq:
                print(f"[skip] {table}: no id sequence found")
                continue

            max_id = conn.execute(
                text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            ).scalar()

            if max_id and max_id > 0:
                # is_called=true -> nextval() returns max_id + 1
                conn.execute(
                    text("SELECT setval(:seq, :val, true)"),
                    {"seq": seq, "val": int(max_id)},
                )
                print(f"[ok]  {table}: sequence set to {max_id} (next id = {max_id + 1})")
            else:
                # Empty table: reset so first insert is id=1
                conn.execute(
                    text("SELECT setval(:seq, 1, false)"),
                    {"seq": seq},
                )
                print(f"[ok]  {table}: empty, sequence reset (next id = 1)")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
