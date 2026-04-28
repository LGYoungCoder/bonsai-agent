"""Usage stats endpoints — /api/stats/{usage,today,hourly,weekly,monthly,
hit-rate-trend,anomalies,export.csv}. Thin wrappers around bonsai.stats."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    def _stats_log_path() -> Path:
        from ...config import load_config
        try:
            cfg = load_config(root / "config.toml")
            return root / cfg.logging.cache_stats.lstrip("./")
        except Exception:
            return root / "logs" / "cache_stats.jsonl"

    @router.get("/api/stats/usage")
    async def api_stats_usage(days: int = 14) -> JSONResponse:
        from ...stats import load_usage, report_to_dict
        r = load_usage(_stats_log_path(), window_days=max(1, min(days, 60)))
        return JSONResponse(report_to_dict(r))

    @router.get("/api/stats/today")
    async def api_stats_today() -> JSONResponse:
        from ...stats import load_today
        return JSONResponse(load_today(_stats_log_path()))

    @router.get("/api/stats/hourly")
    async def api_stats_hourly(date: str | None = None) -> JSONResponse:
        from ...stats import load_hourly
        return JSONResponse(load_hourly(_stats_log_path(), date=date))

    @router.get("/api/stats/weekly")
    async def api_stats_weekly(weeks: int = 8) -> JSONResponse:
        from ...stats import load_weekly
        return JSONResponse(load_weekly(_stats_log_path(),
                                          weeks=max(1, min(weeks, 52))))

    @router.get("/api/stats/monthly")
    async def api_stats_monthly() -> JSONResponse:
        from ...stats import load_monthly_compare
        return JSONResponse(load_monthly_compare(_stats_log_path()))

    @router.get("/api/stats/hit-rate-trend")
    async def api_stats_hit_rate(days: int = 14) -> JSONResponse:
        from ...stats import hit_rate_trend
        return JSONResponse({"trend":
            hit_rate_trend(_stats_log_path(), days=max(1, min(days, 60)))})

    @router.get("/api/stats/anomalies")
    async def api_stats_anomalies(days: int = 14) -> JSONResponse:
        from ...stats import detect_anomalies
        return JSONResponse({"anomalies":
            detect_anomalies(_stats_log_path(), days=max(3, min(days, 60)))})

    @router.get("/api/stats/export.csv")
    async def api_stats_export(days: int = 30):
        from fastapi.responses import Response
        from ...stats import export_csv
        body = export_csv(_stats_log_path(), days=max(1, min(days, 365)))
        return Response(
            content=body, media_type="text/csv; charset=utf-8",
            headers={
                "content-disposition":
                    f"attachment; filename=bonsai-usage-{days}d.csv",
            },
        )

    return router
