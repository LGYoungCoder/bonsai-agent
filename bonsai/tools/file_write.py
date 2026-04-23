"""file_write — patch / overwrite / append / prepend. Patch requires unique match."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def _backup(p: Path) -> Path | None:
    if not p.exists():
        return None
    bdir = p.parent / ".bonsai_backups"
    bdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bp = bdir / f"{p.name}.{stamp}.bak"
    shutil.copy2(p, bp)
    return bp


def file_write(path: str, mode: str, new_content: str,
               old_content: str | None = None,
               cwd: Path | None = None) -> str:
    base = cwd or Path.cwd()
    p = (base / path).resolve() if not Path(path).is_absolute() else Path(path)

    if mode == "patch":
        if not p.exists():
            return f"[error] patch target missing: {p}"
        if old_content is None:
            return "[error] patch mode requires old_content"
        raw = p.read_text(encoding="utf-8", errors="replace")
        n = raw.count(old_content)
        if n == 0:
            return "[error] old_content not found (check whitespace / newlines)"
        if n > 1:
            return f"[error] old_content matches {n} times; provide larger unique context"
        _backup(p)
        new_raw = raw.replace(old_content, new_content, 1)
        p.write_text(new_raw, encoding="utf-8")
        return f"[ok] patched {p} ({len(raw)} → {len(new_raw)} bytes)"

    if mode == "overwrite":
        p.parent.mkdir(parents=True, exist_ok=True)
        bp = _backup(p)
        p.write_text(new_content, encoding="utf-8")
        hint = f" (backup: {bp})" if bp else " (new file)"
        return f"[ok] wrote {p} · {len(new_content)} bytes{hint}"

    if mode == "append":
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(new_content)
        return f"[ok] appended {len(new_content)} bytes to {p}"

    if mode == "prepend":
        existing = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        _backup(p)
        p.write_text(new_content + existing, encoding="utf-8")
        return f"[ok] prepended {len(new_content)} bytes to {p}"

    return f"[error] unknown mode: {mode}"
