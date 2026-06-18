"""Provider-agnostic LLM client.

Supports OpenAI (default), Anthropic, and Google Gemini.

Configuration (env vars):
  LLM_PROVIDER        openai | anthropic | gemini  (default: openai)
  LLM_API_KEY         universal override (takes precedence over provider-specific keys)
  OPENAI_API_KEY      used when provider=openai and LLM_API_KEY unset
  ANTHROPIC_API_KEY   used when provider=anthropic and LLM_API_KEY unset
  GEMINI_API_KEY      used when provider=gemini and LLM_API_KEY unset
  LLM_MODEL           override the default model for the selected provider
  LLM_MATCH_MODEL     override model used for job-match scoring
  LLM_TAILOR_MODEL    override model used for resume tailoring / cover letters

Public interface (preserved for callers):
  llm.complete(system, messages, model, max_tokens, temperature) -> str
  llm.complete_with_cached_profile(profile, instructions, user_prompt, model, max_tokens, temperature) -> str
  llm.acomplete(system, messages, model, max_tokens, temperature) -> Coroutine[str]
  llm.acomplete_with_cached_profile(profile, instructions, user_prompt, model, max_tokens, temperature) -> Coroutine[str]
  DEFAULT_MATCH_MODEL  (module-level constant)
  DEFAULT_TAILOR_MODEL (module-level constant)
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Provider / model defaults
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULTS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "gemini": "gemini-1.5-flash",
}

_LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai").lower()
_LLM_MODEL: str = os.environ.get(
    "LLM_MODEL", _PROVIDER_DEFAULTS.get(_LLM_PROVIDER, "gpt-4o-mini")
)

DEFAULT_MATCH_MODEL: str = os.environ.get("LLM_MATCH_MODEL", _LLM_MODEL)
DEFAULT_TAILOR_MODEL: str = os.environ.get("LLM_TAILOR_MODEL", _LLM_MODEL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_api_key(provider: str) -> str:
    """Return the API key for *provider*, or raise a clear RuntimeError."""
    # Universal override first
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key:
        return key

    env_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    var = env_map.get(provider, "LLM_API_KEY")
    key = os.environ.get(var, "").strip()
    if not key:
        raise RuntimeError(
            f"No API key found for LLM provider '{provider}'. "
            f"Set the {var} environment variable (or LLM_API_KEY as a universal override)."
        )
    return key


def _system_blocks_to_string(system: List[Dict[str, Any]]) -> str:
    """Flatten Anthropic-style system blocks into a plain string for OpenAI."""
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "\n\n".join(parts)


def _strip_code_fence(s: str) -> str:
    """LLMs sometimes wrap JSON in ```json blocks despite instructions not to."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

def _openai_complete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        import openai as _openai
    except ImportError as exc:
        raise ImportError(
            "openai package is required. Install it: pip install openai>=1.0"
        ) from exc

    api_key = _get_api_key("openai")
    client = _openai.OpenAI(api_key=api_key)

    system_text = _system_blocks_to_string(system)
    oai_messages: List[Dict[str, str]] = []
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})
    oai_messages.extend(messages)

    response = client.chat.completions.create(
        model=model,
        messages=oai_messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


async def _openai_acomplete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        import openai as _openai
    except ImportError as exc:
        raise ImportError(
            "openai package is required. Install it: pip install openai>=1.0"
        ) from exc

    api_key = _get_api_key("openai")
    client = _openai.AsyncOpenAI(api_key=api_key)

    system_text = _system_blocks_to_string(system)
    oai_messages: List[Dict[str, str]] = []
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})
    oai_messages.extend(messages)

    response = await client.chat.completions.create(
        model=model,
        messages=oai_messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

def _anthropic_complete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        import anthropic as _anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package is required for provider=anthropic. "
            "Install it: pip install anthropic>=0.39"
        ) from exc

    api_key = _get_api_key("anthropic")
    client = _anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,  # type: ignore[arg-type]
        messages=messages,  # type: ignore[arg-type]
    )
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )


async def _anthropic_acomplete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        import anthropic as _anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package is required for provider=anthropic. "
            "Install it: pip install anthropic>=0.39"
        ) from exc

    api_key = _get_api_key("anthropic")
    client = _anthropic.AsyncAnthropic(api_key=api_key)

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,  # type: ignore[arg-type]
        messages=messages,  # type: ignore[arg-type]
    )
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

def _gemini_complete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        import google.genai as genai  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "google-genai package is required for provider=gemini. "
            "Install it: pip install google-genai"
        ) from exc

    api_key = _get_api_key("gemini")
    client = genai.Client(api_key=api_key)

    system_text = _system_blocks_to_string(system)
    prompt_parts = []
    if system_text:
        prompt_parts.append(system_text)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    prompt_parts.append(block["text"])
        else:
            prompt_parts.append(str(content))

    response = client.models.generate_content(
        model=model,
        contents="\n\n".join(prompt_parts),
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return response.text or ""


async def _gemini_acomplete(
    *,
    system: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Gemini async via asyncio.to_thread (google-genai SDK is sync-only)."""
    import asyncio
    return await asyncio.to_thread(
        lambda: _gemini_complete(
            system=system,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )


# ---------------------------------------------------------------------------
# LLMClient — public API
# ---------------------------------------------------------------------------

class LLMClient:
    """Provider-agnostic LLM wrapper with sync and async paths."""

    def complete(
        self,
        *,
        system: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.4,
    ) -> str:
        """Synchronous completion. Dispatches to the configured provider."""
        provider = _LLM_PROVIDER
        if provider == "openai":
            return _openai_complete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        if provider == "anthropic":
            return _anthropic_complete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        if provider == "gemini":
            return _gemini_complete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider!r}. Choose openai, anthropic, or gemini."
        )

    async def acomplete(
        self,
        *,
        system: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.4,
    ) -> str:
        """Async completion. Dispatches to the configured provider."""
        provider = _LLM_PROVIDER
        if provider == "openai":
            return await _openai_acomplete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        if provider == "anthropic":
            return await _anthropic_acomplete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        if provider == "gemini":
            return await _gemini_acomplete(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
            )
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider!r}. Choose openai, anthropic, or gemini."
        )

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
        """Completion with profile injected as a system block.

        For Anthropic, the profile block is marked ephemeral for prompt caching.
        For OpenAI, prompt caching is automatic (no special markup needed).
        """
        system_blocks = self._build_system_blocks(profile, instructions)
        return self.complete(
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def acomplete_with_cached_profile(
        self,
        *,
        profile: Dict[str, Any],
        instructions: str,
        user_prompt: str,
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.4,
    ) -> str:
        """Async version of complete_with_cached_profile."""
        system_blocks = self._build_system_blocks(profile, instructions)
        return await self.acomplete(
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    @staticmethod
    def _build_system_blocks(
        profile: Dict[str, Any], instructions: str
    ) -> List[Dict[str, Any]]:
        """Build the two-block system list used by complete_with_cached_profile."""
        return [
            {"type": "text", "text": instructions},
            {
                "type": "text",
                "text": (
                    f"USER PROFILE (the candidate):\n"
                    f"{json.dumps(profile, indent=2, default=str)}"
                ),
                # Anthropic uses cache_control for ephemeral caching.
                # OpenAI ignores this field silently (auto-caches long prompts).
                "cache_control": {"type": "ephemeral"},
            },
        ]


llm = LLMClient()
