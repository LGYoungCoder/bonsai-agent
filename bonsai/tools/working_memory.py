"""working_memory — agent-driven scratch pad persisted in the session dir.

Why: bonsai's prompt cache treats turn history as opaque; once you've
read 10 files to understand a module, that context will eventually get
compressed away by BudgetPolicy. working_memory is the LLM's way of
saying "save this distilled fact to disk before it's forgotten".

Operations:
  set: replace the entire note with new content
  append: tack content onto the end
  get: read the current note
  clear: wipe it

Note file: <session_artifact_dir>/working_memory.md
"""

from __future__ import annotations

import time
from pathlib import Path


def _note_path(artifact_dir: Path | None) -> Path:
    base = artifact_dir or (Path.cwd() / ".bonsai_session")
    base.mkdir(parents=True, exist_ok=True)
    return base / "working_memory.md"


def working_memory(action: str, content: str | None = None,
                   *, artifact_dir: Path | None = None) -> str:
    path = _note_path(artifact_dir)
    action = (action or "").lower()

    if action == "get":
        if not path.exists():
            return "[empty] working memory not set"
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return "[empty] working memory exists but is empty"
        return f"[working_memory · {len(text)} chars]\n{text}"

    if action == "clear":
        if path.exists():
            path.unlink()
        return "[ok] working memory cleared"

    if action in ("set", "replace"):
        if content is None:
            return "[error] set/replace requires content"
        path.write_text(content, encoding="utf-8")
        return f"[ok] working memory set · {len(content)} chars"

    if action == "append":
        if content is None:
            return "[error] append requires content"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        stamp = time.strftime("%H:%M:%S")
        addition = f"\n\n--- {stamp} ---\n{content}"
        path.write_text(existing + addition, encoding="utf-8")
        return f"[ok] appended {len(content)} chars (total {len(existing) + len(addition)})"

    return f"[error] unknown action: {action!r} (use get/set/append/clear)"
