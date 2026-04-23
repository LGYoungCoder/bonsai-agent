"""FrontendAdapter Protocol.

A frontend:
  1. accepts a user message (text)
  2. streams agent events back
  3. optionally handles ask_user prompts
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

PromptFn = Callable[[str, list[str] | None], Awaitable[str]]


class FrontendAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
