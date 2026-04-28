"""Autonomous workspace endpoints — todo, init, reports."""

from __future__ import annotations

from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/autonomous/state")
    async def api_auto_state() -> JSONResponse:
        from ...autonomous import AutonomousWorkspace
        w = AutonomousWorkspace(root)
        return JSONResponse({
            "initialized": w.initialized,
            "dir": str(w.dir),
            "todo": w.get_todo(),
            "history": w.get_history(30),
            "reports": w.list_reports(),
        })

    @router.post("/api/autonomous/init")
    async def api_auto_init(request: Request) -> JSONResponse:
        from ...autonomous import AutonomousWorkspace
        try:
            body = orjson.loads(await request.body() or b"{}")
        except Exception:
            body = {}
        w = AutonomousWorkspace(root)
        w.init(overwrite=bool(body.get("overwrite")))
        return JSONResponse({"ok": True, "dir": str(w.dir)})

    @router.post("/api/autonomous/todo")
    async def api_auto_todo_save(request: Request) -> JSONResponse:
        from ...autonomous import AutonomousWorkspace
        body = orjson.loads(await request.body())
        w = AutonomousWorkspace(root)
        w.set_todo(body.get("text") or "")
        return JSONResponse({"ok": True})

    @router.get("/api/autonomous/reports/{fname}")
    async def api_auto_report(fname: str) -> JSONResponse:
        from ...autonomous import AutonomousWorkspace
        w = AutonomousWorkspace(root)
        try:
            content = w.read_report(fname)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, str(e))
        return JSONResponse({"file": fname, "content": content})

    return router
