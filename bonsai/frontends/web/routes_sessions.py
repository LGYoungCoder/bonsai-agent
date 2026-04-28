"""Session log read / list / delete."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()
    sessions_dir = root / "logs" / "sessions"

    @router.get("/api/sessions")
    async def api_sessions_list(limit: int = 50) -> JSONResponse:
        import json as _json
        d = sessions_dir
        out = []
        if not d.exists():
            return JSONResponse({"sessions": []})
        for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            preview = ""
            user_turns = 0
            total_lines = 0
            try:
                with p.open("r", encoding="utf-8") as f:
                    for raw in f:
                        total_lines += 1
                        if not raw.strip():
                            continue
                        try:
                            e = _json.loads(raw)
                        except Exception:
                            continue
                        if e.get("role") == "user":
                            user_turns += 1
                            if not preview and e.get("content"):
                                preview = str(e["content"])[:80]
            except OSError:
                continue
            # Filename scheme: `{source}-{uid}-{chat}-{ts}.jsonl` for
            # channel logs; bare session_id for web/CLI logs.
            stem = p.stem
            source = "web"
            for sfx in ("wechat", "feishu", "wecom", "telegram", "dingtalk"):
                if stem.startswith(sfx + "-"):
                    source = sfx
                    break
            out.append({
                "id": stem,
                "preview": preview or "(空)",
                "mtime": p.stat().st_mtime,
                "turns": user_turns,
                "lines": total_lines,
                "size": p.stat().st_size,
                "source": source,
            })
            if len(out) >= limit:
                break
        return JSONResponse({"sessions": out})

    @router.get("/api/sessions/{sid}")
    async def api_session_read(sid: str, since: int = 0) -> JSONResponse:
        """`since` = byte offset into the jsonl. When >0 only return entries
        past it. `offset` in the response = byte position right after the
        last *complete* (\\n-terminated) line read; pass it as `since` next
        poll. Partial trailing lines are left for the next tick so we never
        emit a half-written entry."""
        import json as _json
        p = sessions_dir / f"{sid}.jsonl"
        try:
            p.resolve().relative_to(sessions_dir.resolve())
        except ValueError:
            raise HTTPException(400, "path escapes sessions dir")
        if not p.exists():
            raise HTTPException(404, "not found")
        entries = []
        offset = max(0, int(since or 0))
        with p.open("rb") as f:
            size = p.stat().st_size
            # `since` past EOF (file truncated/rotated) → reset to 0.
            if offset > size:
                offset = 0
            f.seek(offset)
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    entries.append(_json.loads(line))
                except Exception:
                    continue
        return JSONResponse({"id": sid, "entries": entries, "offset": offset})

    @router.delete("/api/sessions/{sid}")
    async def api_session_delete(sid: str) -> JSONResponse:
        p = sessions_dir / f"{sid}.jsonl"
        try:
            p.resolve().relative_to(sessions_dir.resolve())
        except ValueError:
            raise HTTPException(400, "path escapes sessions dir")
        if not p.exists():
            raise HTTPException(404, "not found")
        p.unlink()
        return JSONResponse({"ok": True})

    return router
