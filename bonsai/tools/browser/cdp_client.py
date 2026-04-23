"""Minimal CDP (Chrome DevTools Protocol) client over WebSocket.

No heavy browser-automation deps (no selenium / playwright). Connects to an
existing Chrome launched with `--remote-debugging-port=9222`. One target
(tab) at a time; switching is a cheap operation.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import websockets

log = logging.getLogger(__name__)


@dataclass
class Target:
    id: str
    title: str
    url: str
    ws_url: str
    type: str


class CDPClient:
    """One logical connection to one tab (target)."""

    def __init__(self, debug_url: str = "http://127.0.0.1:9222") -> None:
        self.debug_url = debug_url.rstrip("/")
        self._ws: Any = None
        self._id_counter = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self.target: Target | None = None

    # ---- target discovery ----------------------------------------------
    async def list_targets(self) -> list[Target]:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.get(f"{self.debug_url}/json/list")
            r.raise_for_status()
            data = r.json()
        return [Target(id=t["id"], title=t.get("title", ""), url=t.get("url", ""),
                       ws_url=t.get("webSocketDebuggerUrl", ""), type=t.get("type", ""))
                for t in data if t.get("webSocketDebuggerUrl")]

    async def new_target(self, url: str = "about:blank") -> Target:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.put(f"{self.debug_url}/json/new?{url}")
            r.raise_for_status()
            t = r.json()
        return Target(id=t["id"], title=t.get("title", ""), url=t.get("url", ""),
                      ws_url=t.get("webSocketDebuggerUrl", ""), type=t.get("type", ""))

    async def attach(self, target: Target) -> None:
        await self.detach()
        self._ws = await websockets.connect(target.ws_url, max_size=50 * 1024 * 1024)
        self.target = target
        self._reader_task = asyncio.create_task(self._reader())
        await self.send("Page.enable")
        await self.send("DOM.enable")
        await self.send("Runtime.enable")
        await self.send("Accessibility.enable")

    async def detach(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ---- message pump --------------------------------------------------
    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if "error" in msg:
                        fut.set_exception(CDPError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result") or {})
                elif "method" in msg:
                    await self._events.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("CDP reader ended: %s", e)

    async def send(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        if self._ws is None:
            raise RuntimeError("CDPClient not attached")
        req_id = next(self._id_counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        payload = {"id": req_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=timeout)


class CDPError(RuntimeError):
    pass
