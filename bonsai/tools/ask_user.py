"""ask_user — delegated to the frontend. Backend just marks should_exit=True."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

UserPromptFn = Callable[[str, list[str] | None], Awaitable[str]]


async def ask_user(question: str, candidates: list[str] | None = None,
                   *, prompt_fn: UserPromptFn | None = None) -> str:
    if prompt_fn is None:
        # Offline default — return placeholder so the loop can exit cleanly.
        return f"[no frontend attached] user would be asked: {question}"
    return await prompt_fn(question, candidates)
