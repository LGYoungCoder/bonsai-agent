"""Canonical message/tool types shared across backends.

Each adapter is responsible for translating to/from its provider's wire format.
Keep this module free of provider-specific concepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    name: str | None = None  # for tool-role messages (GLM/OpenAI-compat shape)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class FrozenPrefix:
    """Everything that must be byte-stable across requests for cache hits."""

    system_prompt: str
    tools: list[ToolSpec] = field(default_factory=list)
    l1_index: str = ""  # appended to system prompt by adapter

    def render_system(self) -> str:
        if self.l1_index:
            return f"{self.system_prompt}\n\n# Skill Index (read-only)\n{self.l1_index}"
        return self.system_prompt


@dataclass
class DynamicTail:
    """The mutable conversation tail."""

    messages: list[Message] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class Response:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""


# Streaming event shape — yielded from backend.chat_stream() and loop.
@dataclass
class StreamEvent:
    kind: Literal["text", "tool_call", "usage", "done", "error", "warn"]
    data: Any = None
