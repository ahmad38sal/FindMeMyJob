"""One-time repair: normalize Resume.content stored as a JSON string.

A few legacy rows have ``content`` written as ``json.dumps(dict)`` into the
JSON column, so it round-trips back as a ``str`` instead of a ``dict``. Any
code doing ``content.get(...)`` / ``dict(content)`` then blows up — most
visibly the manual-edit save path, which silently no-ops on those rows.

This scans every Resume row, and for any whose ``content`` is a string, parses
it (unwrapping one layer of double-encoding) and rewrites it as a real dict.
Rows already holding a dict are left untouched. Safe to run repeatedly: a
second run finds 0 string rows.

Usage:
    DATABASE_URL=postgresql://...  python scripts/normalize_resume_content.py
    # (no DATABASE_URL -> operates on the local SQLite DB)
"""
from __future__ import annotations

import json
import sys

from sqlmodel import Session, select

from findmemyjob.db import engine
from findmemyjob.models import Resume


def _parse_to_dict(raw: str):
    """Parse a JSON string to a dict, unwrapping one extra encoding layer."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _count_string_rows(session: Session) -> int:
    return sum(
        1 for r in session.exec(select(Resume)).all() if isinstance(r.content, str)
    )


def main() -> int:
    with Session(engine) as session:
        before = _count_string_rows(session)
        print(f"Resume rows with string content (before): {before}")

        fixed, unparseable = 0, []
        for r in session.exec(select(Resume)).all():
            if not isinstance(r.content, str):
                continue
            parsed = _parse_to_dict(r.content)
            if parsed is None:
                unparseable.append(r.id)
                print(f"[warn] resume id={r.id}: content string not parseable to dict; left as-is")
                continue
            r.content = parsed  # assign a real dict; JSON column serializes it
            session.add(r)
            fixed += 1
            print(f"[ok]  resume id={r.id}: content normalized to dict")

        session.commit()

        after = _count_string_rows(session)
        print(f"Fixed {fixed} row(s). Unparseable: {unparseable or 'none'}")
        print(f"Resume rows with string content (after): {after}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
