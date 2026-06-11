"""Profile-driven search strategy.

The careers site has ~5,700 jobs. We don't want to fetch and score all of
them. Instead, we use the user's profile to generate a handful of targeted
search queries, run the fetcher once per query, and dedup the union.

Width controls how aggressively we widen:
  - "narrow"  -> 3 queries (closest to actual skills)
  - "medium"  -> 5 queries (closest + some adjacent areas)
  - "wide"    -> 10 queries (closest + adjacent + reach areas)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from findmemyjob.llm import DEFAULT_MATCH_MODEL, llm
from findmemyjob.matching import _strip_code_fence


WIDTH_TO_COUNT = {"narrow": 3, "medium": 5, "wide": 10}


_SEARCH_INSTRUCTIONS = """\
You generate short search queries for the careers.apple.com search box, given a
candidate's profile. Each query is 1-4 words, the kind of thing the candidate
would actually type into a job search.

Goals:
  - Cover the candidate's strongest, most recent skills.
  - Include role titles they realistically qualify for or could stretch into,
    given their `stretch_slider` (0 = strict match, 100 = include big reaches).
  - Avoid generic terms ("engineer", "developer") that match thousands of
    irrelevant jobs.
  - Don't include filters not entered as text (location, team) — those are
    handled separately.

Return STRICT JSON (no commentary, no markdown):
{
  "queries": [
    {"query": "iOS engineer", "rationale": "primary recent role, ICT4 mobile"},
    {"query": "ML platform Python", "rationale": "stretch — adjacent to current backend skills"}
  ]
}
"""


def suggest_search_queries(profile_dict: Dict[str, Any], width: str = "medium") -> List[Dict[str, str]]:
    """Return a list of {query, rationale} dicts based on the profile."""
    count = WIDTH_TO_COUNT.get(width, 5)
    user_prompt = (
        f"Generate exactly {count} search queries for this candidate. "
        f"Width: {width} ({count} queries; "
        f"{'closest matches only' if width == 'narrow' else 'include some adjacent areas' if width == 'medium' else 'include reaches'}).\n\n"
        "Output JSON only."
    )
    raw = llm.complete_with_cached_profile(
        profile=profile_dict,
        instructions=_SEARCH_INSTRUCTIONS,
        user_prompt=user_prompt,
        model=DEFAULT_MATCH_MODEL,
        max_tokens=1024,
        temperature=0.4,
    )
    data = json.loads(_strip_code_fence(raw))
    return data.get("queries", [])
