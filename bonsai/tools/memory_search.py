"""memory_search / memory_recall — query MemoryStore."""

from __future__ import annotations

from ..stores.memory_store import MemoryStore

_MAX_N = 5
_TOTAL_PREVIEW_CAP = 1500  # chars across all drawer previews per call


def memory_search(query: str, *, wing: str | None = None, room: str | None = None,
                  n: int = 5, store: MemoryStore | None = None) -> str:
    if store is None:
        return ("[memory_search] MemoryStore not attached. "
                "Run `bonsai init` first.")
    n = min(n, _MAX_N)
    drawers = store.search(query, wing=wing, room=room, n=n)
    if not drawers:
        return f"[memory_search] no hits for query={query!r}" \
               + (f" wing={wing}" if wing else "") \
               + (f" room={room}" if room else "")
    lines = [f"[memory_search] {len(drawers)} drawer(s) for {query!r}:"]
    used = 0
    for i, d in enumerate(drawers):
        remaining = _TOTAL_PREVIEW_CAP - used
        if remaining <= 0:
            lines.append(f"\n[+{len(drawers) - i} more truncated — narrow query or use wing/room]")
            break
        preview_budget = min(300, remaining)
        scope = f"{d.wing}/{d.room}" if d.wing else "(unscoped)"
        preview = d.content[:preview_budget].replace("\n", " ⏎ ")
        suffix = "" if len(d.content) <= preview_budget else f"... [+{len(d.content) - preview_budget} chars]"
        lines.append(f"\n— {scope}  score={d.score:.2f}  kind={d.kind}")
        lines.append(f"  {preview}{suffix}")
        used += len(preview)
    return "\n".join(lines)


def memory_recall(*, wing: str | None = None, room: str | None = None,
                  limit: int = 5, store: MemoryStore | None = None) -> str:
    if store is None:
        return "[memory_recall] MemoryStore not attached."
    drawers = store.recall(wing=wing, room=room, limit=limit)
    if not drawers:
        return "[memory_recall] empty scope"
    lines = [f"[memory_recall] {len(drawers)} recent drawer(s):"]
    for d in drawers:
        scope = f"{d.wing}/{d.room}" if d.wing else "(unscoped)"
        preview = d.content[:300].replace("\n", " ⏎ ")
        lines.append(f"\n— {scope} kind={d.kind}\n  {preview}")
    return "\n".join(lines)
