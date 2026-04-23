"""Zhipu GLM adapter. OpenAI-compatible at /api/paas/v4/chat/completions."""

from __future__ import annotations

from dataclasses import dataclass

from ._openai_compat import OpenAICompatAdapter


@dataclass
class GLMAdapter(OpenAICompatAdapter):
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    kind: str = "glm"

    def _clamp_temperature(self, t):
        # GLM rejects temperature==0 on some tool-calling paths; nudge to 0.01.
        if t is not None and t <= 0:
            return 0.01
        return t
