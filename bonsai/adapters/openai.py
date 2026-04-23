"""OpenAI API adapter (gpt-4.1, gpt-5 etc). Auto-caches prefixes ≥ 1024 tokens."""

from __future__ import annotations

from dataclasses import dataclass

from ._openai_compat import OpenAICompatAdapter


@dataclass
class OpenAIAdapter(OpenAICompatAdapter):
    base_url: str = "https://api.openai.com/v1"
    kind: str = "openai"

    # OpenAI does automatic prefix caching — no cache_control hints needed.
    # The adapter just has to keep the prefix byte-stable (done in types.FrozenPrefix).
