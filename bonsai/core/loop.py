"""Stateless agent loop.

Per turn:
  1. budget check
  2. backend.stream()  (SSE) with chat() as fallback if the backend
     doesn't support streaming. The loop relays text chunks so the UI
     can do a typewriter effect, and accumulates them to build the final
     assistant message for history.
  3. dispatch tool calls (parallel where safe)
  4. if no tool calls OR all should_exit → done
  5. append tool results as a 'tool' role message, loop

The loop never writes to memory stores; the background writer does.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .backend import Backend
from .budget import BudgetPolicy, compress_thinking, estimate, truncate_tool_result
from .handler import Handler
from .session_log import SessionLog
from .types import DynamicTail, FrozenPrefix, Message, StreamEvent, ToolCall, Usage

log = logging.getLogger(__name__)


class AgentLoop:
    """Small wrapper that drives a single conversation turn-by-turn."""

    def __init__(
        self,
        backend: Backend,
        prefix: FrozenPrefix,
        handler: Handler,
        *,
        policy: BudgetPolicy | None = None,
        max_turns: int = 40,
        session_log: SessionLog | None = None,
    ) -> None:
        self.backend = backend
        self.prefix = prefix
        self.handler = handler
        self.policy = policy or BudgetPolicy()
        self.max_turns = max_turns
        self.tail = DynamicTail(messages=[])
        self.session_log = session_log

    def add_user(self, text: str) -> None:
        self.tail.messages.append(Message(role="user", content=text))
        if self.session_log:
            self.session_log.record_user(text)

    async def run(self) -> AsyncIterator[StreamEvent]:
        """Drive one user turn to completion, yielding events.

        Uses backend.stream() so the UI sees text tokens as they arrive
        (typewriter effect). Chunks are accumulated into a single string
        for the history record — identical to the old non-streaming
        path from the budget/cache perspective.
        """
        soft_warned = False
        for turn in range(self.max_turns):
            self.handler.session.next_turn()
            total = _estimate_total(self.tail.messages, self.prefix)
            if total > self.policy.soft:
                self.tail.messages, new_total = _compress_tail(
                    self.tail.messages, self.prefix, self.policy, start_total=total,
                )
                log.info("compressed history: %d → %d tokens (soft=%d hard=%d)",
                         total, new_total, self.policy.soft, self.policy.hard)

            # Soft-landing warning (C2): give the UI/user a heads-up two turns
            # before we hit max_turns. Non-interactive channels can ignore it
            # safely — we don't block on ask_user here (headless would stall).
            if (not soft_warned
                    and self.max_turns >= 4
                    and turn == self.max_turns - 2):
                soft_warned = True
                yield StreamEvent(kind="warn", data={
                    "code": "approaching_max_turns",
                    "turns_so_far": turn + 1,
                    "max_turns": self.max_turns,
                    "hint": "接近 max_turns。建议模型总结现状、输出中间结论。",
                })

            acc_text = ""
            tool_calls: list[ToolCall] = []
            usage: Usage | None = None
            provider = ""
            model = ""
            try:
                async for ev in self.backend.stream(self.prefix, self.tail):
                    if ev.kind == "text":
                        chunk = ev.data or ""
                        acc_text += chunk
                        if chunk:
                            yield ev
                    elif ev.kind == "tool_call":
                        tool_calls.append(ev.data)
                    elif ev.kind == "usage":
                        usage = ev.data
                        yield ev
                    elif ev.kind == "done":
                        break
                    elif ev.kind == "error":
                        yield ev
            except Exception as e:
                # Streaming failed mid-turn — fall back to a blocking
                # chat() so one provider wobble doesn't wipe the user's
                # turn. Resulting text still lands in the history.
                log.warning("stream failed (%s) — falling back to chat()", e)
                resp = await self.backend.chat(self.prefix, self.tail)
                acc_text = resp.content or ""
                tool_calls = list(resp.tool_calls)
                usage = resp.usage
                provider = resp.provider
                model = resp.model
                if acc_text:
                    yield StreamEvent(kind="text", data=acc_text)
                yield StreamEvent(kind="usage", data=usage)

            # Surface tool calls after text — matches non-streaming shape.
            for tc in tool_calls:
                yield StreamEvent(kind="tool_call", data=tc)

            # Record assistant message with its tool calls.
            asst_msg = Message(
                role="assistant",
                content=acc_text or None,
                tool_calls=list(tool_calls),
            )
            self.tail.messages.append(asst_msg)
            if self.session_log:
                # Backend/model may not be surfaced via stream events —
                # use the backend's declared identity.
                prov = provider or getattr(self.backend, "name", "") or ""
                mdl = model or getattr(self.backend, "model", "") or ""
                self.session_log.record_assistant(
                    asst_msg,
                    provider=prov, model=mdl, turn=turn + 1,
                )

            if not tool_calls:
                yield StreamEvent(kind="done", data={"reason": "no_tool_calls", "turns": turn + 1})
                return

            outcomes = await self.handler.dispatch_batch(list(tool_calls))

            # Append tool results as a single 'tool' message.
            tool_msg = Message(
                role="tool",
                tool_results=[o.tool_result for o in outcomes],
            )
            self.tail.messages.append(tool_msg)
            if self.session_log:
                self.session_log.record_tool_results(tool_msg, turn=turn + 1)

            if any(o.should_exit for o in outcomes):
                yield StreamEvent(kind="done", data={"reason": "should_exit", "turns": turn + 1})
                return

        yield StreamEvent(kind="done", data={
            "reason": "max_turns",
            "turns": self.max_turns,
            "hint": "hit max_turns — raise policy, split the task, or continue "
                    "with a fresh user message.",
        })


def _msg_to_raw(m: Message) -> dict:
    # Rough dict shape for budget estimation.
    d = {"role": m.role}
    if m.content:
        d["content"] = m.content
    if m.tool_calls:
        d["tool_calls"] = [{"name": tc.name, "args": tc.args} for tc in m.tool_calls]
    if m.tool_results:
        d["tool_results"] = [{"id": tr.tool_call_id, "content": tr.content}
                             for tr in m.tool_results]
    return d


def _estimate_total(messages: list[Message], prefix: FrozenPrefix) -> int:
    return estimate([_msg_to_raw(m) for m in messages]) + estimate(prefix.tools)


_THINKING_COMPRESS_AGGRESSIVE_THRESHOLD = 50_000  # B2: fixed to keep tail bytes
                                                  # deterministic → cache stable.
_FILE_READ_SUPERSEDED_MARKER = "[superseded by later file_read with same args]"
_DROP_NOTE_PREFIX = "\n\n[system-note: "
_DROP_NOTE_SUFFIX = " earlier turn(s) dropped for token budget — thread continues.]"


def _compress_tail(
    messages: list[Message],
    prefix: FrozenPrefix,
    policy: BudgetPolicy,
    *,
    start_total: int,
) -> tuple[list[Message], int]:
    """Gradient compression. Each pass runs only if we're still above target.

    Target is soft × 0.6 — leaves headroom for the next turn's new tokens.
    Pipeline is idempotent so repeat calls don't break cache stability.
    Hitting hard does NOT raise; the final pass pops oldest non-pinned turns.
    """
    target = int(policy.soft * 0.6)

    def _total() -> int:
        return _estimate_total(messages, prefix)

    # Pass 0 (B3): supersede stale duplicate file_read results. Safe to run
    # repeatedly — keyed on tool_call args, not on tool_result content.
    _supersede_duplicate_file_reads(messages)
    if _total() <= target:
        return messages, _total()

    # Pass 1: shrink oversize tool_result bodies (cheapest, biggest win).
    for m in messages:
        if m.role != "tool":
            continue
        for tr in m.tool_results:
            if len(tr.content) > 4000:
                tr.content = truncate_tool_result(tr.content, max_chars=4000)
    if _total() <= target:
        return messages, _total()

    # Pass 2: compress <thinking> blocks in older assistant messages.
    # B2: under high pressure, extend compression to near-current turns too.
    # Threshold is a FIXED constant so idempotency holds for cache stability.
    keep_recent = 2 if _total() > _THINKING_COMPRESS_AGGRESSIVE_THRESHOLD else 6
    if len(messages) > keep_recent:
        for m in messages[:-keep_recent]:
            if m.role == "assistant" and m.content:
                m.content = compress_thinking(m.content, max_block_chars=800)
    if _total() <= target:
        return messages, _total()

    # Pass 3: pop oldest non-pinned messages (pin first user turn).
    # Drops pairs to keep tool_call/tool_result paired. Floor is 4 messages
    # so the model still sees at least one prior turn of context.
    pinned = messages[:1] if messages and messages[0].role == "user" else []
    body = messages[len(pinned):]

    # Pull any prior drop-note off the anchor so the count stays cumulative
    # across repeated compressions. Idempotent: parse → strip → re-add below.
    anchor = pinned[0] if pinned else None
    prior_drops = _strip_drop_note(anchor)

    pre_drop_len = len(body)
    min_body = 4
    while len(body) > min_body:
        messages = pinned + body
        if _total() <= target:
            break
        body.pop(0)
        if body and body[0].role == "tool":  # keep assistant/tool pairs balanced
            body.pop(0)
    messages = pinned + body

    # Leave the model a breadcrumb so it doesn't think the thread starts here.
    total_dropped = prior_drops + (pre_drop_len - len(body))
    if total_dropped > 0 and anchor is not None and isinstance(anchor.content, str):
        anchor.content += f"{_DROP_NOTE_PREFIX}{total_dropped}{_DROP_NOTE_SUFFIX}"

    # Pass 4: last resort — hard-crush any surviving tool_result bodies.
    if _total() > target:
        for m in messages:
            if m.role == "tool":
                for tr in m.tool_results:
                    if len(tr.content) > 400:
                        tr.content = truncate_tool_result(tr.content, max_chars=400)

    return messages, _total()


def _strip_drop_note(anchor: Message | None) -> int:
    """If the anchor user turn already carries a drop-note, extract its count
    and strip it. Returns the prior count (0 if no note). Pair this with a
    re-append to keep the count cumulative across compression rounds.
    """
    if anchor is None or not isinstance(anchor.content, str):
        return 0
    idx = anchor.content.rfind(_DROP_NOTE_PREFIX)
    if idx < 0:
        return 0
    tail = anchor.content[idx + len(_DROP_NOTE_PREFIX):]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return 0
    anchor.content = anchor.content[:idx]
    return int(digits)


def _supersede_duplicate_file_reads(messages: list[Message]) -> None:
    """If the same `file_read(path, start, count)` appears in multiple turns,
    replace the tool_result content of the earlier ones with a short marker.

    Only touches exact-arg duplicates — `ls -la` via code_run, or file_reads
    with different slice ranges, are left alone (they may legitimately return
    different content). Idempotent: repeated calls yield the same bytes.
    """
    # Collect file_read tool_call_ids with their arg-key and message index.
    by_id: dict[str, tuple[tuple, int]] = {}
    for idx, m in enumerate(messages):
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            if tc.name != "file_read":
                continue
            args = tc.args or {}
            key = (args.get("path"), args.get("start", 1), args.get("count", 200))
            if key[0] is None:
                continue  # malformed, skip
            by_id[tc.id] = (key, idx)

    # For each arg-key, keep only the latest id alive.
    latest_by_key: dict[tuple, tuple[str, int]] = {}
    for tc_id, (key, idx) in by_id.items():
        cur = latest_by_key.get(key)
        if cur is None or cur[1] < idx:
            latest_by_key[key] = (tc_id, idx)
    latest_ids = {v[0] for v in latest_by_key.values()}

    # Walk tool_results, replace content for superseded ids. Only act on ids
    # that appear more than once (i.e. the arg-key has a later, newer call).
    superseded = {tc_id for tc_id in by_id if tc_id not in latest_ids}
    if not superseded:
        return
    for m in messages:
        if m.role != "tool":
            continue
        for tr in m.tool_results:
            if tr.tool_call_id in superseded and tr.content != _FILE_READ_SUPERSEDED_MARKER:
                tr.content = _FILE_READ_SUPERSEDED_MARKER
