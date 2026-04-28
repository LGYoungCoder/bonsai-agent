"""FastAPI + WebSocket frontend. Serves the unified app.html.

Usage:
  bonsai serve --host 0.0.0.0 --port 7878
then open http://localhost:7878/ in a browser.

Routes are split across `routes_*.py` modules under this package; each
exports a `make_router(root[, chat_factory]) -> APIRouter`. They get
included by `make_app` in the order below — `routes_channels` keeps its
internal wechat-specific routes BEFORE the generic `{kind}/runner/*` block
so FastAPI doesn't shadow them with the path-param route.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import (
    routes_autonomous,
    routes_channels,
    routes_config,
    routes_doctor,
    routes_memory,
    routes_root,
    routes_scheduler,
    routes_sessions,
    routes_skills,
    routes_stats,
)
from ._common import _autostart_channels

log = logging.getLogger(__name__)


def make_app(root: Path, chat_factory) -> FastAPI:
    """chat_factory is a callable returning (AgentLoop, Session, Handler, prompt_resolver)
    per new websocket session. prompt_resolver is an async fn to resolve ask_user."""

    @asynccontextmanager
    async def lifespan(app):
        # Restart already-running runners whose bytecode pre-dates the package
        # source. Without this, `git pull` + serve restart leaves runners on
        # stale bytecode → confusing cross-thread / missing-attr crashes that
        # the actual code already fixed.
        try:
            from ...channels.supervisor import restart_stale_runners
            restart_stale_runners(root)
        except Exception as e:
            log.warning("stale-runner sweep failed: %s", e)
        _autostart_channels(root)
        # Background scheduler task — runs alongside serve, stops with it.
        from ...scheduler import scheduler_loop
        stop_evt = asyncio.Event()
        sched_task = asyncio.create_task(scheduler_loop(root, stop_evt=stop_evt))
        # Periodic gc daemon (in-process, configurable via [maintenance]).
        try:
            from ...config import load_config as _lc
            from ...maintenance import start_maintenance
            start_maintenance(root, _lc(None))
        except Exception as e:
            log.warning("could not start maintenance: %s", e)
        try:
            yield
        finally:
            stop_evt.set()
            sched_task.cancel()
            try:
                await sched_task
            except (asyncio.CancelledError, Exception):
                pass

    app = FastAPI(title="Bonsai Web", lifespan=lifespan)

    # Order matters only inside routes_channels (wechat/* before {kind}/*).
    # Across routers FastAPI builds one combined route table, so the
    # registration order here decides match order.
    app.include_router(routes_root.make_router(root, chat_factory))
    app.include_router(routes_config.make_router(root))
    app.include_router(routes_sessions.make_router(root))
    app.include_router(routes_memory.make_router(root))
    app.include_router(routes_channels.make_router(root))
    app.include_router(routes_skills.make_router(root))
    app.include_router(routes_doctor.make_router(root))
    app.include_router(routes_stats.make_router(root))
    app.include_router(routes_autonomous.make_router(root))
    app.include_router(routes_scheduler.make_router(root))

    return app


__all__ = ["make_app"]
