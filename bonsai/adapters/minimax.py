"""MiniMax M2 series adapter. OpenAI-compatible with minor temperature quirks."""

from __future__ import annotations

from dataclasses import dataclass

from ._openai_compat import OpenAICompatAdapter


@dataclass
class MiniMaxAdapter(OpenAICompatAdapter):
    base_url: str = "https://api.minimax.chat/v1"
    kind: str = "minimax"

    def _clamp_temperature(self, t):
        # MiniMax M2.7 behaves badly at temperature>1.2 on tool use paths.
        # Clamp defensively; user can still override via opts to get raw behaviour.
        if t is None:
            return None
        if t < 0.01:
            return 0.01
        if t > 1.2:
            return 1.2
        return t
