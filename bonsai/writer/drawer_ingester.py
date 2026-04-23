"""Ingest conversation turns as MemoryStore drawers.

Runs in background. Never touches the agent loop's state. Idempotent by
content_hash — re-running on the same session file is safe.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import orjson

from ..stores.embed import build_embedder
from ..stores.memory_store import MemoryStore

log = logging.getLogger(__name__)


def ingest_session(session_file: Path, db_path: Path, embed_cfg: dict,
                   *, closet: str = "life", wing: str | None = None,
                   room: str | None = None) -> int:
    """Read a session JSONL and ingest each turn as a drawer. Returns count ingested."""
    if not session_file.exists():
        log.warning("session file missing: %s", session_file)
        return 0

    embedder = build_embedder(embed_cfg)
    store = MemoryStore(db_path, embedder=embedder)

    wing = wing or "general"
    room = room or time.strftime("%Y-%m-%d", time.localtime(session_file.stat().st_mtime))

    count = 0
    with session_file.open("rb") as f:
        for line in f:
            try:
                entry = orjson.loads(line)
            except Exception:
                continue
            content = _render_entry(entry)
            if not content.strip():
                continue
            inserted = store.ingest(
                closet=closet, wing=wing, room=room,
                kind=entry.get("role", "turn"),
                content=content,
                meta={k: v for k, v in entry.items()
                      if k in {"turn", "session_id", "model", "provider"}},
                ts=entry.get("t") or time.time(),
            )
            if inserted is not None:
                count += 1
    store.close()
    return count


def _render_entry(entry: dict) -> str:
    """Render a session log entry into verbatim drawer content."""
    role = entry.get("role", "?")
    if entry.get("content"):
        return f"[{role}]\n{entry['content']}"
    tcs = entry.get("tool_calls") or []
    if tcs:
        parts = [f"[{role} · tool_calls]"]
        for tc in tcs:
            parts.append(f"  {tc.get('name', '?')}({orjson.dumps(tc.get('args', {})).decode()})")
        return "\n".join(parts)
    trs = entry.get("tool_results") or []
    if trs:
        parts = [f"[{role} · tool_results]"]
        for tr in trs:
            parts.append(f"  {tr.get('id', '')}: {str(tr.get('content', ''))[:500]}")
        return "\n".join(parts)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_file", type=Path)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--wing", type=str)
    ap.add_argument("--room", type=str)
    ap.add_argument("--closet", type=str, default="life")
    ap.add_argument("--embed-provider", type=str, default="hash")
    ap.add_argument("--embed-api-key", type=str, default="")
    ap.add_argument("--embed-base-url", type=str, default="")
    ap.add_argument("--embed-model", type=str, default="")
    args = ap.parse_args()

    logging.basicConfig(level="INFO")
    cfg = {
        "embed_provider": args.embed_provider,
        "embed_api_key": args.embed_api_key,
        "embed_base_url": args.embed_base_url,
        "embed_model": args.embed_model or "text-embedding-3-small",
    }
    n = ingest_session(args.session_file, args.db, cfg,
                       closet=args.closet, wing=args.wing, room=args.room)
    print(f"ingested {n} drawer(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
