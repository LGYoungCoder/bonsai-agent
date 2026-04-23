"""Long-session compaction: produce AAAK-style summary + drop into MemoryStore as L4.

AAAK = Anchor / Action / Artifact / Knowledge
  Anchor   — the user's ask
  Action   — what the agent did (tool calls summary)
  Artifact — files created / changed / URLs
  Knowledge — things the agent *learned* (new SOPs, surprising facts)
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import orjson

from ..stores.memory_store import MemoryStore

log = logging.getLogger(__name__)


def compact_session(session_file: Path, memory_store: MemoryStore,
                    *, closet: str = "life", wing: str = "summaries",
                    trigger_turns: int = 40) -> str | None:
    """Summarize a long session. Returns the AAAK text, or None if too short."""
    if not session_file.exists():
        return None
    turns: list[dict] = []
    with session_file.open("rb") as f:
        for line in f:
            try:
                turns.append(orjson.loads(line))
            except Exception:
                continue
    if len(turns) < trigger_turns:
        return None

    aaak = _aaak(turns)
    if not aaak.strip():
        return None
    room = f"session-{session_file.stem}"
    memory_store.ingest(
        closet=closet, wing=wing, room=room,
        kind="session_summary",
        content=aaak,
        meta={"turns": len(turns), "source": session_file.name,
              "summarized_at": time.time()},
    )
    return aaak


def _aaak(turns: list[dict]) -> str:
    """Deterministic AAAK construction without LLM. Cheap but useful."""
    anchors = [t["content"] for t in turns[:3] if t.get("role") == "user" and t.get("content")]
    tool_counts: dict[str, int] = {}
    files_touched: set[str] = set()
    urls: set[str] = set()
    errors = 0
    for t in turns:
        for tc in t.get("tool_calls") or []:
            nm = tc.get("name", "")
            tool_counts[nm] = tool_counts.get(nm, 0) + 1
            args = tc.get("args") or {}
            if nm in ("file_write", "file_read"):
                if p := args.get("path"):
                    files_touched.add(p)
            if nm in ("web_scan", "web_navigate", "web_execute_js"):
                for v in args.values():
                    if isinstance(v, str) and re.match(r"https?://", v):
                        urls.add(v)
        for tr in t.get("tool_results") or []:
            if tr.get("is_error"):
                errors += 1

    lines = ["# Session AAAK Summary"]
    if anchors:
        lines.append("\n## Anchor (user asks)")
        for a in anchors[:3]:
            lines.append(f"- {a[:200]}")
    if tool_counts:
        lines.append("\n## Action (tool calls)")
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {name} × {count}")
        if errors:
            lines.append(f"- tool errors: {errors}")
    artifacts_lines: list[str] = []
    if files_touched:
        artifacts_lines.append("### Files")
        artifacts_lines.extend(f"- {f}" for f in sorted(files_touched)[:30])
    if urls:
        artifacts_lines.append("### URLs")
        artifacts_lines.extend(f"- {u}" for u in sorted(urls)[:15])
    if artifacts_lines:
        lines.append("\n## Artifacts")
        lines.extend(artifacts_lines)

    # Knowledge: detect "I learned ..." / "记住" patterns in last few assistant turns.
    knowledge: list[str] = []
    for t in turns[-20:]:
        c = t.get("content") or ""
        if not c:
            continue
        for m in re.finditer(r"(?:记住|learned|注意|重要)[::]\s*([^\n]{5,200})", c):
            knowledge.append(m.group(1).strip())
    if knowledge:
        lines.append("\n## Knowledge")
        lines.extend(f"- {k}" for k in list(dict.fromkeys(knowledge))[:8])

    return "\n".join(lines)
