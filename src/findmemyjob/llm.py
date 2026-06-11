"""Claude via Floodgate — Apple's internal Anthropic-API-compatible proxy.

Mirrors the pattern from `~/Automated_Tableau_Reporter/TLF-Execution-Dashboard-main/app.py`:
  - Auth: bearer token from `~/.claude/apple/get-apple-token.sh`, cached 50 min.
  - Endpoint: https://floodgate.g.apple.com/api/anthropic/v1/messages
  - TLS: pinned to ~/.claude/apple/certs/bundle.pem.

Floodgate accepts standard Anthropic Messages-API request bodies, including
`system` blocks with `cache_control` markers — so prompt caching of the master
profile works the same way it would against the public Anthropic API.

DEPLOYMENT NOTE: this only works on a machine where `get-apple-token.sh` can
mint a token (i.e. an Apple corp-managed device). When we deploy the app to a
personal cloud host, the Apple-internal scraper has to run on the work Mac
and push results to the deployed app, OR we swap this client for the public
Anthropic API at deploy time.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from findmemyjob.config import settings

_APPLE_TOKEN_SCRIPT = str(Path.home() / ".claude/apple/get-apple-token.sh")
_CLAUDE_CA_BUNDLE = str(Path.home() / ".claude/apple/certs/bundle.pem")
_CLAUDE_ENDPOINT = "https://floodgate.g.apple.com/api/anthropic/v1/messages"

# Floodgate uses the `anthropic.<model>` naming convention.
DEFAULT_MATCH_MODEL = "anthropic.claude-sonnet-4-6"
DEFAULT_TAILOR_MODEL = "anthropic.claude-sonnet-4-6"


_token_state: Dict[str, Any] = {"token": None, "expires": 0.0}
_token_lock = threading.Lock()


def _get_token() -> str:
    """Bearer token for Floodgate. Cached for 50 min, then refreshed via the script."""
    with _token_lock:
        now = time.time()
        if _token_state["token"] and _token_state["expires"] > now + 60:
            return _token_state["token"]

        if not Path(_APPLE_TOKEN_SCRIPT).exists():
            raise RuntimeError(
                f"Floodgate token script not found at {_APPLE_TOKEN_SCRIPT}. "
                "This client only works on an Apple corp-managed device."
            )

        result = subprocess.run(
            ["bash", _APPLE_TOKEN_SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise RuntimeError(
                f"get-apple-token.sh failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        _token_state["token"] = token
        _token_state["expires"] = now + 50 * 60
        return token


class LLMClient:
    """Thin wrapper around Floodgate's Anthropic-compatible Messages endpoint."""

    def complete(
        self,
        *,
        system: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.4,
    ) -> str:
        token = _get_token()
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        resp = httpx.post(
            _CLAUDE_ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "User-Agent": "FindMeMyJob/0.1",
            },
            json=body,
            timeout=60,
            verify=_CLAUDE_CA_BUNDLE,
        )
        resp.raise_for_status()
        data = resp.json()
        # Same shape as the public Anthropic API.
        return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")

    def complete_with_cached_profile(
        self,
        *,
        profile: Dict[str, Any],
        instructions: str,
        user_prompt: str,
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.4,
    ) -> str:
        """Two-block system: instructions + cached profile.

        On every call after the first within ~5 min, the profile block is a
        cache hit. Keep `instructions` stable across calls in a given module
        so it caches too.
        """
        system_blocks: List[Dict[str, Any]] = [
            {"type": "text", "text": instructions},
            {
                "type": "text",
                "text": f"USER PROFILE (the candidate):\n{json.dumps(profile, indent=2, default=str)}",
                "cache_control": {"type": "ephemeral"},
            },
        ]
        return self.complete(
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )


llm = LLMClient()
