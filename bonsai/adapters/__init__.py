"""Backend adapters. Each provider gets a thin subclass."""

from .claude import ClaudeAdapter
from .glm import GLMAdapter
from .minimax import MiniMaxAdapter
from .openai import OpenAIAdapter
from .qwen import QwenAdapter

KIND_TO_ADAPTER = {
    "claude": ClaudeAdapter,
    "openai": OpenAIAdapter,
    # DeepSeek is OpenAI-wire-compatible; ships as a kind alias so users can
    # write `kind = "deepseek"` and stats/logs show DeepSeek distinctly.
    # V4-flash / V3 / chat / reasoner all route the same way; only base_url changes.
    "deepseek": OpenAIAdapter,
    "glm": GLMAdapter,
    "qwen": QwenAdapter,
    "minimax": MiniMaxAdapter,
}


def build_adapter(cfg: dict):
    kind = cfg["kind"]
    cls = KIND_TO_ADAPTER.get(kind)
    if cls is None:
        raise ValueError(f"unknown provider kind: {kind}")
    fields = {
        "name": cfg["name"],
        "model": cfg["model"],
        "api_key": cfg["api_key"],
        "kind": kind,  # propagate so logs/stats distinguish aliased providers (e.g. deepseek → openai)
    }
    if "base_url" in cfg:
        fields["base_url"] = cfg["base_url"]
    if "temperature" in cfg:
        fields["temperature"] = cfg["temperature"]
    if "max_tokens" in cfg:
        fields["max_tokens"] = cfg["max_tokens"]
    if "timeout" in cfg:
        fields["timeout"] = cfg["timeout"]
    return cls(**fields)


__all__ = [
    "ClaudeAdapter",
    "GLMAdapter",
    "MiniMaxAdapter",
    "OpenAIAdapter",
    "QwenAdapter",
    "KIND_TO_ADAPTER",
    "build_adapter",
]
