"""Memory store status / reseed / embedder connectivity test."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/memory/status")
    async def api_memory_status() -> JSONResponse:
        from ...config import load_config
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            return JSONResponse({"initialized": False, "reason": "no config.toml"})
        skill_dir = (root / cfg.memory.skill_dir.lstrip("./")).resolve()
        db_path = (root / cfg.memory.memory_db.lstrip("./")).resolve()
        out = {
            "initialized": False,
            "skill_dir": str(skill_dir),
            "skill_dir_exists": skill_dir.exists(),
            "l0_exists": (skill_dir / "L0.md").exists(),
            "l1_exists": (skill_dir / "L1_index.txt").exists(),
            "l2_exists": (skill_dir / "L2_facts.txt").exists(),
            "l3_count": len(list((skill_dir / "L3").glob("*.md"))) if (skill_dir / "L3").exists() else 0,
            "memory_db": str(db_path),
            "memory_db_exists": db_path.exists(),
            "memory_db_size": db_path.stat().st_size if db_path.exists() else 0,
            "embed_provider": cfg.memory.embed_provider,
            "embed_model": cfg.memory.embed_model,
        }
        if db_path.exists():
            try:
                from ...stores.memory_store import MemoryStore
                ms = MemoryStore(db_path, embedder=None)
                st = ms.stats()
                ms.close()
                out.update(st)
            except Exception as e:
                out["db_error"] = str(e)
        out["initialized"] = (
            out["skill_dir_exists"] and out["l0_exists"]
            and out["l1_exists"] and out["memory_db_exists"]
        )
        return JSONResponse(out)

    @router.post("/api/memory/reseed")
    async def api_memory_reseed() -> JSONResponse:
        """Force-reseed sample SOPs + recreate L0/L1/L2 if missing."""
        from ...config import load_config
        from ...cli.setup_wizard import init_stores
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "先保存 config.toml")
        mem = {
            "skill_dir": cfg.memory.skill_dir,
            "memory_db": cfg.memory.memory_db,
        }
        return JSONResponse({"ok": True, "stores": init_stores(root, mem)})

    @router.post("/api/memory/embed_test")
    async def api_memory_embed_test() -> JSONResponse:
        """实例化当前 config 里配置的 embedder, embed 一段示例文本,
        返回是否连得通 + 维度 + 耗时。本地 sentence-transformers 第一次会下模型,
        所以默认时限 60s 由各 embedder 自带。"""
        import time
        from ...config import load_config
        from ...stores.embed import build_embedder
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "先保存 config.toml")
        mem_cfg = {
            "embed_provider": cfg.memory.embed_provider,
            "embed_model": cfg.memory.embed_model,
            "embed_base_url": cfg.memory.embed_base_url,
            "embed_api_key": cfg.memory.embed_api_key,
        }
        try:
            emb = build_embedder(mem_cfg)
        except Exception as e:
            return JSONResponse({"ok": False, "stage": "build",
                                 "provider": mem_cfg["embed_provider"],
                                 "error": f"{type(e).__name__}: {e}"})
        t0 = time.monotonic()
        try:
            # embed 是同步的(httpx / sentence-transformers 都是阻塞);
            # 扔到线程池别堵住 event loop。
            vecs = await asyncio.to_thread(emb.embed, ["bonsai 连接测试"])
        except Exception as e:
            return JSONResponse({"ok": False, "stage": "embed",
                                 "provider": mem_cfg["embed_provider"],
                                 "model": mem_cfg["embed_model"],
                                 "error": f"{type(e).__name__}: {e}",
                                 "latency_ms": int((time.monotonic() - t0) * 1000)})
        return JSONResponse({
            "ok": True,
            "provider": getattr(emb, "name", mem_cfg["embed_provider"]),
            "model": mem_cfg["embed_model"],
            "dim": len(vecs[0]) if vecs else 0,
            "latency_ms": int((time.monotonic() - t0) * 1000),
        })

    return router
