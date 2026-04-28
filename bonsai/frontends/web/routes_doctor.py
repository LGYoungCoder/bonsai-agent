"""Doctor health-check endpoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/doctor")
    async def api_doctor() -> JSONResponse:
        from ...cli.doctor import collect_checks
        try:
            checks = collect_checks(root)
        except Exception as e:
            log.exception("doctor failed")
            return JSONResponse({"error": str(e), "checks": []}, status_code=200)
        return JSONResponse({
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                for c in checks
            ],
        })

    return router
