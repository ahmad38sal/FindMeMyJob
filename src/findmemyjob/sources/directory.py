"""Curated directory of well-known tech companies on Greenhouse / Lever.

Starter list — biased toward AI, devtools, fintech, infrastructure, since
that's where most engineering hiring happens. Extend by adding to your own
External Companies list in `/profile`.

Each slug below has been verified at least once, but companies do migrate
between ATSes — if a fetch 404s, the source skips it gracefully.
"""
from __future__ import annotations

from typing import List

# Greenhouse slugs (boards.greenhouse.io/<slug>)
GREENHOUSE: List[str] = [
    # AI / ML
    "anthropic", "openai", "scaleai", "huggingface", "perplexityai", "character",
    "elevenlabs", "runwayml", "midjourney",
    # Infrastructure / DevTools
    "stripe", "cloudflare", "hashicorp", "datadog", "snowflake", "confluent",
    "mongodb", "elastic", "gitlab", "circleci", "vercel", "netlify",
    "supabase", "render", "fly", "linear",
    # Productivity / SaaS
    "notion", "figma", "airtable", "canva", "asana", "monday", "miro",
    "intercom", "loom", "1password", "grammarly",
    # Consumer / Marketplaces
    "airbnb", "pinterest", "doordash", "instacart", "lyft", "reddit", "discord",
    "roblox", "roku", "patreon", "substack", "zapier",
    # Fintech
    "robinhood", "plaid", "brex", "ramp", "mercury", "wealthfront", "affirm",
    "chime", "coinbase", "kraken", "alchemy", "alpaca",
    # Healthcare / Bio
    "oscarhealth", "rohealth", "verily", "23andme",
    # Auto / Robotics
    "waymo", "joby", "wisk", "zipline",
    # Misc heavy hitters
    "databricks", "samsara", "thumbtack", "faire", "shopify",
]

# Lever slugs (jobs.lever.co/<slug>)
LEVER: List[str] = [
    "mistral", "together", "cresta", "modal", "replicate",
    "weightsbiases", "cohere",
    "vanta", "sentry", "retool",
    "ridgeline", "jasper",
    "betterup", "quill",
]
