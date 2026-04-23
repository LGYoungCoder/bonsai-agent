"""Token budgeting — estimate, soft/hard limits, history compression.

Design rule: *rules and regex*, never "smart" LLM summarization. Each
compression step must be byte-stable for the frozen prefix; only the
dynamic tail may shrink.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

try:
    import tiktoken  # optional
    _ENC = tiktoken.get_encoding("cl100k_base")

    def _count(text: str) -> int:
        return len(_ENC.encode(text, disallowed_special=()))
except Exception:
    _ENC = None

    def _count(text: str) -> int:
        # 4 chars/token for ASCII, ~1.5 for CJK. Cheap heuristic.
        ascii_chars = sum(1 for c in text if ord(c) < 128)
        cjk_chars = len(text) - ascii_chars
        return ascii_chars // 4 + int(cjk_chars / 1.5) + 1


def estimate(x: Any) -> int:
    """Approximate token count for str / dict / list of messages."""
    if x is None:
        return 0
    if isinstance(x, str):
        return _count(x)
    if isinstance(x, (int, float, bool)):
        return _count(str(x))
    if isinstance(x, dict):
        return sum(estimate(k) + estimate(v) for k, v in x.items()) + 2
    if isinstance(x, (list, tuple)):
        return sum(estimate(v) for v in x) + 2
    return _count(str(x))


@dataclass
class BudgetPolicy:
    soft: int = 40_000
    hard: int = 60_000
    prefix_max: int = 3_000  # sys + tools + L1 index


class BudgetExceeded(RuntimeError):
    pass


def check(messages: Iterable[dict], tools: Any, *, policy: BudgetPolicy) -> int:
    total = estimate(list(messages)) + estimate(tools)
    if total > policy.hard:
        raise BudgetExceeded(f"total={total} exceeds hard={policy.hard}")
    return total


_TRUNC_MARKER = "\n... [truncated {n} chars] ..."


def truncate_tool_result(content: str, max_chars: int = 20_000,
                         interest_hint: str | None = None) -> str:
    """Head+tail truncation. If interest_hint is given, also keep top-BM25 lines."""
    if len(content) <= max_chars:
        return content
    if not interest_hint:
        head = content[: max_chars // 2]
        tail = content[-max_chars // 2:]
        dropped = len(content) - len(head) - len(tail)
        return head + _TRUNC_MARKER.format(n=dropped) + tail

    # Cheap relevance: score lines by shared tokens with hint.
    hint_tokens = set(re.findall(r"\w+", interest_hint.lower()))
    lines = content.splitlines(keepends=True)
    scored = [
        (i, sum(1 for w in re.findall(r"\w+", line.lower()) if w in hint_tokens), line)
        for i, line in enumerate(lines)
    ]
    keep = sorted((s for s in scored if s[1] > 0), key=lambda s: -s[1])
    budget = max_chars // 3
    kept_indices: set[int] = set()
    used = 0
    for idx, _score, line in keep:
        if used + len(line) > budget:
            break
        kept_indices.add(idx)
        used += len(line)
    # head + tail + matched lines
    head_budget = (max_chars - used) // 2
    tail_budget = max_chars - used - head_budget
    head: list[str] = []
    tail: list[str] = []
    used_head = used_tail = 0
    for i, line in enumerate(lines):
        if i in kept_indices:
            continue
        if used_head + len(line) < head_budget:
            head.append(line)
            used_head += len(line)
        elif used_tail + len(line) < tail_budget:
            tail.append(line)
            used_tail += len(line)
    # rebuild in original order
    kept_set = kept_indices
    result: list[str] = []
    for i, line in enumerate(lines):
        if i in kept_set or line in head or line in tail:
            result.append(line)
    if len("".join(result)) < len(content):
        dropped = len(content) - len("".join(result))
        result.append(_TRUNC_MARKER.format(n=dropped))
    return "".join(result)


def compress_history(messages: list[dict], policy: BudgetPolicy,
                     *, keep_last: int = 4) -> list[dict]:
    """Shrink the dynamic tail. Never touches the first two messages (system + first user)."""
    if len(messages) <= keep_last + 2:
        return messages
    pinned = messages[:2]
    tail = messages[2:]
    to_fold = tail[:-keep_last]
    survivors = tail[-keep_last:]

    # Fold repeated identical tool_results.
    folded: list[dict] = []
    prev_key: tuple | None = None
    run = 0
    for m in to_fold:
        key = (m.get("role"), m.get("name"), _stable_hash(m.get("content")))
        if key == prev_key:
            run += 1
            continue
        if run > 0 and folded:
            folded[-1] = _annotate_repeat(folded[-1], run + 1)
            run = 0
        folded.append(m)
        prev_key = key
    if run > 0 and folded:
        folded[-1] = _annotate_repeat(folded[-1], run + 1)

    # Hard truncate tool_result fields > 4K chars.
    for m in folded:
        c = m.get("content")
        if isinstance(c, str) and len(c) > 4000:
            m["content"] = truncate_tool_result(c, max_chars=4000)

    return pinned + folded + survivors


_THINKING_RE = re.compile(r"(<thinking>)(.*?)(</thinking>)", re.DOTALL)


def compress_thinking(text: str, *, max_block_chars: int = 800) -> str:
    """Truncate <thinking>…</thinking> blocks longer than max_block_chars.

    Head+tail preservation with a fixed marker. Idempotent: re-running on
    already-compressed text yields the same bytes, so cache stays stable.
    """
    if "<thinking>" not in text:
        return text

    _MARKER_RESERVE = 64  # bytes kept for the truncation marker text

    def _shrink(match: re.Match) -> str:
        inner = match.group(2)
        if len(inner) <= max_block_chars:
            return match.group(0)
        keep = max(32, (max_block_chars - _MARKER_RESERVE) // 2)
        dropped = len(inner) - 2 * keep
        return (
            match.group(1)
            + inner[:keep]
            + f"\n...[truncated {dropped}]...\n"
            + inner[-keep:]
            + match.group(3)
        )

    return _THINKING_RE.sub(_shrink, text)


def _stable_hash(v: Any) -> int:
    if isinstance(v, str):
        return hash(v)
    if isinstance(v, (dict, list)):
        import orjson
        return hash(orjson.dumps(v, option=orjson.OPT_SORT_KEYS))
    return hash(repr(v))


def _annotate_repeat(msg: dict, n: int) -> dict:
    c = msg.get("content")
    if isinstance(c, str):
        msg = dict(msg)
        msg["content"] = c + f"\n[repeated {n} times]"
    return msg
