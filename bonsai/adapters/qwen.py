"""Alibaba Qwen (DashScope) OpenAI-compatible endpoint."""

from __future__ import annotations

from dataclasses import dataclass

from ._openai_compat import OpenAICompatAdapter


@dataclass
class QwenAdapter(OpenAICompatAdapter):
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    kind: str = "qwen"

    def _clamp_temperature(self, t):
        # DashScope rejects 0.0 on qwen-max* with tool use; ref: Aliyun docs.
        if t is None:
            return None
        if t < 0.01:
            return 0.01
        if t > 2.0:
            return 2.0
        return t
