"""SkillStore listing + L3 SOP CRUD."""

from __future__ import annotations

import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    def _resolve_skill_dir() -> Path:
        try:
            from ...config import load_config
            cfg = load_config(root / "config.toml")
            sd = Path(cfg.memory.skill_dir)
            return sd if sd.is_absolute() else (root / sd)
        except Exception:
            return root / "skills"

    def _safe_skill_target(path: str) -> Path:
        skill_dir = _resolve_skill_dir()
        target = (skill_dir / path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            raise HTTPException(400, "path escapes skill_dir")
        return target

    @router.get("/api/skills")
    async def api_skills() -> JSONResponse:
        from ...stores.skill_store import SkillStore
        try:
            from ...config import load_config
            cfg = load_config(root / "config.toml")
            skill_dir = Path(cfg.memory.skill_dir)
            if not skill_dir.is_absolute():
                skill_dir = root / skill_dir
        except Exception:
            skill_dir = root / "skills"
        if not skill_dir.exists():
            return JSONResponse({"skill_dir": str(skill_dir), "exists": False,
                                 "l1": "", "l2": "", "sops": []})
        store = SkillStore(skill_dir)
        sops = []
        for entry in store.list_sops():
            sops.append({
                "name": entry.name,
                "path": str(entry.path.relative_to(skill_dir)) if entry.path.is_relative_to(skill_dir) else str(entry.path),
                "keywords": entry.keywords,
                "created": entry.created,
                "verified_on": entry.verified_on,
            })
        return JSONResponse({
            "skill_dir": str(skill_dir),
            "exists": True,
            "l1": store.l1_text(max_chars=4000),
            "l2": store.l2_text(max_chars=1000),
            "sops": sops,
        })

    @router.get("/api/skills/read")
    async def api_skill_read(path: str) -> JSONResponse:
        target = _safe_skill_target(path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "not found")
        return JSONResponse({"path": path, "content": target.read_text(encoding="utf-8")})

    @router.post("/api/skills/write")
    async def api_skill_write(request: Request) -> JSONResponse:
        """Create or overwrite an L3 SOP. Body: {name, keywords (list|str), content}
        - `name` lower-snake,<40 chars
        - content 可以自带 frontmatter;没带就用 name+keywords 自动生成
        - 落到 <skill_dir>/L3/<name>.md,然后 rebuild L1 索引
        """
        import re as _re
        import time as _time
        body = orjson.loads(await request.body())
        name = (body.get("name") or "").strip()
        if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}", name):
            raise HTTPException(400, "name 必须是字母/数字/下划线/连字符,1-40 字符")
        kws = body.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(",") if k.strip()]
        content = body.get("content") or ""
        if not content.strip():
            raise HTTPException(400, "content 不能为空")
        skill_dir = _resolve_skill_dir()
        l3_dir = skill_dir / "L3"
        l3_dir.mkdir(parents=True, exist_ok=True)
        target = l3_dir / f"{name}.md"
        if not content.lstrip().startswith("---"):
            today = _time.strftime("%Y-%m-%d")
            frontmatter = (
                f"---\nname: {name}\nkeywords: {kws}\n"
                f"created: {today}\nverified_on: {today}\nsource: manual\n---\n\n"
            )
            content = frontmatter + content.lstrip()
        target.write_text(content, encoding="utf-8")
        # Rebuild L1 index so lookup() sees the new entry.
        try:
            from ...stores.skill_store import SkillStore
            SkillStore(skill_dir)._rebuild_l1()
        except Exception as e:
            log.warning("rebuild L1 failed: %s", e)
        return JSONResponse({"ok": True,
                              "path": str(target.relative_to(skill_dir)),
                              "overwritten": target.stat().st_size > 0})

    @router.delete("/api/skills/delete")
    async def api_skill_delete(path: str) -> JSONResponse:
        """Delete one L3 SOP. `path` is relative to skill_dir (e.g. 'L3/foo.md')."""
        target = _safe_skill_target(path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "not found")
        # Refuse to delete anything outside L3/ — evidence, L0, L1/L2 indexes are off-limits.
        skill_dir = _resolve_skill_dir()
        if target.parent != (skill_dir / "L3").resolve():
            raise HTTPException(400, "只允许删除 L3/ 下的 SOP")
        target.unlink()
        try:
            from ...stores.skill_store import SkillStore
            SkillStore(skill_dir)._rebuild_l1()
        except Exception as e:
            log.warning("rebuild L1 failed: %s", e)
        return JSONResponse({"ok": True})

    return router
