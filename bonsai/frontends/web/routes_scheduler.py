"""Scheduler tasks list / save / delete / run-now / reports."""

from __future__ import annotations

import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/scheduler/tasks")
    async def api_sched_list() -> JSONResponse:
        from ...scheduler import list_tasks, _last_run
        from dataclasses import asdict
        out = []
        for t in list_tasks(root):
            d = asdict(t)
            last = _last_run(root, t.name)
            d["last_run"] = last.isoformat(timespec="minutes") if last else None
            out.append(d)
        return JSONResponse({"tasks": out})

    @router.post("/api/scheduler/tasks")
    async def api_sched_save(request: Request) -> JSONResponse:
        from ...scheduler import Task, save_task
        body = orjson.loads(await request.body())
        try:
            task = Task(
                name=(body.get("name") or "").strip(),
                schedule=(body.get("schedule") or "").strip(),
                prompt=body.get("prompt") or "",
                repeat=body.get("repeat") or "daily",
                enabled=bool(body.get("enabled", True)),
                max_delay_hours=int(body.get("max_delay_hours", 6)),
            )
            path = save_task(root, task)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"ok": True, "path": str(path)})

    @router.delete("/api/scheduler/tasks/{name}")
    async def api_sched_delete(name: str) -> JSONResponse:
        from ...scheduler import delete_task
        if not delete_task(root, name):
            raise HTTPException(404, "不存在")
        return JSONResponse({"ok": True})

    @router.post("/api/scheduler/tasks/{name}/run")
    async def api_sched_run_now(name: str) -> JSONResponse:
        """Fire the task right now, out of schedule."""
        from ...scheduler import list_tasks, run_once
        from ...config import load_config
        task = next((t for t in list_tasks(root) if t.name == name), None)
        if task is None:
            raise HTTPException(404, "不存在")
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "config.toml 缺失")
        try:
            path = await run_once(root, task, cfg)
        except Exception as e:
            log.exception("manual run failed")
            raise HTTPException(500, f"运行失败: {e}")
        return JSONResponse({"ok": True, "report": path.name})

    @router.get("/api/scheduler/reports")
    async def api_sched_reports(task: str | None = None, limit: int = 50) -> JSONResponse:
        from ...scheduler import list_reports
        return JSONResponse({"reports": list_reports(root, task_name=task, limit=limit)})

    @router.get("/api/scheduler/reports/{fname}")
    async def api_sched_report_read(fname: str) -> JSONResponse:
        from ...scheduler import reports_dir
        p = (reports_dir(root) / fname).resolve()
        if not str(p).startswith(str(reports_dir(root).resolve())) or not p.exists():
            raise HTTPException(404, "not found")
        return JSONResponse({"file": fname, "content": p.read_text(encoding="utf-8")})

    return router
