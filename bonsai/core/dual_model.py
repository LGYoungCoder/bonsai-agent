"""Planner/Executor split.

Strategy:
  - first turn of a new user task (or after `/replan`): Planner responds
  - subsequent turns: Executor responds
  - if the Executor asks the same thing twice, or its output is "I don't know"-shaped
    within N turns, escalate to Planner
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import orjson

from .backend import Backend
from .types import DynamicTail, FrozenPrefix, Response, ToolCall

log = logging.getLogger(__name__)


_PLANNER_CUE = "请你作为 Planner。给出 2-5 步执行计划。"
_CONFUSED_PATTERN = re.compile(
    r"(不清楚|不知道|需要更多信息|无法确定|suggest(s)? you|I'm not sure|I don't know)",
    re.IGNORECASE,
)

_LOOP_DETECT_N = 3  # escalate when the executor emits the SAME tool_call signature
                    # this many times in a row — conservative to avoid spurious
                    # planner (expensive model) invocations.


def _tool_sig(tool_calls: list[ToolCall]) -> str:
    """Stable signature of a turn's tool_calls. Empty string if none."""
    if not tool_calls:
        return ""
    parts = []
    for tc in tool_calls:
        args_bytes = orjson.dumps(tc.args or {}, option=orjson.OPT_SORT_KEYS)
        parts.append(f"{tc.name}:{args_bytes.decode('utf-8')}")
    return "||".join(parts)


@dataclass
class DualModelBackend:
    """Backend wrapper that routes requests between planner/executor."""

    name: str = "dual"
    kind: str = "dual"
    model: str = "planner+executor"

    planner: Backend = None  # type: ignore[assignment]
    executor: Backend = None  # type: ignore[assignment]
    replan_every_n_turns: int = 6
    _turn: int = 0
    _last_turn_was_planner: bool = False
    _recent_sigs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.planner is None or self.executor is None:
            raise ValueError("dual_model requires both planner and executor")
        self.model = f"{self.planner.model}+{self.executor.model}"

    async def chat(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any) -> Response:
        is_new_task = self._turn == 0 or tail.messages and self._is_user_followup(tail)
        use_planner = (
            is_new_task
            or self._turn % self.replan_every_n_turns == 0
        )
        chosen = self.planner if use_planner else self.executor
        log.debug("dual_model -> %s (planner=%s)", chosen.name, use_planner)
        resp = await chosen.chat(prefix, tail, **opts)

        escalated = False
        if not use_planner:
            # Trigger 1: executor says "I don't know"-ish.
            if _CONFUSED_PATTERN.search(resp.content or ""):
                log.info("executor confused, escalating to planner")
                resp = await self.planner.chat(prefix, tail, **opts)
                escalated = True
            else:
                # Trigger 2: executor emits the same tool_call signature N times
                # in a row → it's stuck in a loop, kick up to planner.
                sig = _tool_sig(resp.tool_calls)
                if sig:
                    self._recent_sigs.append(sig)
                    self._recent_sigs = self._recent_sigs[-_LOOP_DETECT_N:]
                    if (len(self._recent_sigs) >= _LOOP_DETECT_N
                            and len(set(self._recent_sigs)) == 1):
                        log.info("executor stuck on %s (%d× in a row), "
                                 "escalating to planner", sig[:120], _LOOP_DETECT_N)
                        resp = await self.planner.chat(prefix, tail, **opts)
                        escalated = True
                        self._recent_sigs.clear()

        if escalated or use_planner:
            # Don't let planner turns or post-escalation fool the loop detector.
            self._recent_sigs.clear()

        self._turn += 1
        self._last_turn_was_planner = use_planner or escalated
        return resp

    async def stream(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any):
        # For simplicity, fall back to .chat() and yield as a single text event.
        from .types import StreamEvent
        resp = await self.chat(prefix, tail, **opts)
        if resp.content:
            yield StreamEvent(kind="text", data=resp.content)
        for tc in resp.tool_calls:
            yield StreamEvent(kind="tool_call", data=tc)
        yield StreamEvent(kind="usage", data=resp.usage)
        yield StreamEvent(kind="done")

    @staticmethod
    def _is_user_followup(tail: DynamicTail) -> bool:
        """The last message being a user turn without prior assistant means new task."""
        if not tail.messages:
            return True
        last = tail.messages[-1]
        return last.role == "user" and not any(
            m.role == "assistant" for m in tail.messages[:-1]
        )
