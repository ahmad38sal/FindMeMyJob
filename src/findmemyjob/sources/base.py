"""Base protocol all job sources must implement."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from findmemyjob.models import Job


@runtime_checkable
class JobSource(Protocol):
    """A pluggable source of job postings.

    Implementations should return Job instances that are NOT yet persisted —
    the caller is responsible for upserting (dedup by source + source_id).
    """

    name: str  # e.g. "apple_internal", "greenhouse"

    def fetch(self, *, query: str = "", limit: int = 100) -> List[Job]:
        ...
