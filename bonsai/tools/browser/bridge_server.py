"""WebSocket bridge server for the Chrome extension under assets/chrome_bridge/.

The extension connects to ws://127.0.0.1:18765 and sends:
  - {"type": "ext_ready", "tabs": [...]}     on connect
  - {"type": "tabs_update", "tabs": [...]}   when tabs change
  - {"type": "ack", "id": "..."}             ack request
  - {"type": "result", "id": "...", "result": ..., "newTabs": [...]}
  - {"type": "error",  "id": "...", "error": ..., "newTabs": [...]}
  - {"type": "ping"}                         keepalive

The server sends:
  - {"id": "<uuid>", "code": <str|dict>, "tabId": <int>}

If `code` is a string the extension runs it as JS. If it's an object with
`cmd`, the extension routes to its custom command handler (tabs / cdp /
cookies / batch / management). See assets/chrome_bridge/background.js.

Single-extension assumption — bonsai is a single-user agent. We accept any
extension that connects and treat the most-recent `ext_ready` as the active
client. Earlier connections silently get displaced.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import orjson

log = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    """Raised when the extension reports an error or transport breaks."""


class BridgeServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 18765) -> None:
        self.host = host
        self.port = port
        self._server: Any = None        # websockets.Server
        self._client: Any = None        # active WS connection (most recent)
        self._tabs: list[dict] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._connected_evt = asyncio.Event()

    async def start(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "websockets not installed. Run `pip install websockets` "
                "(or `pip install bonsai-agent[bridge]`)."
            ) from e
        self._server = await websockets.serve(
            self._on_client, self.host, self.port,
            max_size=8 * 1024 * 1024,  # 8MB caps DOM dumps; same as managed mode
        )
        log.info("bridge listening on ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("server stopped"))
        self._pending.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait_for_extension(self, timeout: float = 20.0) -> None:
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise BridgeError(
                f"no extension connected after {timeout:.0f}s. "
                "Install assets/chrome_bridge/ in Chrome and reload."
            ) from e

    @property
    def tabs(self) -> list[dict]:
        return list(self._tabs)

    def pick_tab(self, url_match: str | None = None) -> dict | None:
        """First scriptable tab, optionally filtered by URL substring."""
        for t in self._tabs:
            if url_match and url_match not in t.get("url", ""):
                continue
            return t
        return None

    async def send_code(self, code: str, tab_id: int,
                        *, timeout: float = 15.0) -> Any:
        return await self._send({"code": code, "tabId": tab_id}, timeout=timeout)

    async def send_cmd(self, cmd: dict, tab_id: int | None = None,
                       *, timeout: float = 15.0) -> Any:
        msg = {"code": cmd}
        if tab_id is not None:
            msg["tabId"] = tab_id
        return await self._send(msg, timeout=timeout)

    async def _send(self, payload: dict, *, timeout: float) -> Any:
        if self._client is None:
            raise BridgeError("no extension connected")
        req_id = uuid.uuid4().hex[:12]
        payload = {"id": req_id, **payload}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._client.send(orjson.dumps(payload).decode())
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _on_client(self, ws) -> None:
        """One WS connection from the extension."""
        # Most recent extension wins; old one's pending requests get an error
        # so callers don't hang.
        if self._client is not None and self._client is not ws:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BridgeError("extension reconnected"))
            self._pending.clear()
        self._client = ws
        try:
            async for raw in ws:
                try:
                    data = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    log.warning("bridge: invalid json from extension: %r", raw[:200])
                    continue
                self._handle_inbound(data)
        except Exception as e:
            log.debug("bridge client closed: %s", e)
        finally:
            if self._client is ws:
                self._client = None
                self._connected_evt.clear()

    def _handle_inbound(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "ext_ready":
            self._tabs = list(data.get("tabs") or [])
            self._connected_evt.set()
            log.info("bridge: extension ready, %d tab(s)", len(self._tabs))
            return
        if kind == "tabs_update":
            self._tabs = list(data.get("tabs") or [])
            return
        if kind == "ping":
            return
        if kind == "ack":
            return  # acks are informational; we wait for result/error
        if kind in ("result", "error"):
            req_id = data.get("id", "")
            fut = self._pending.get(req_id)
            if fut is None or fut.done():
                return
            new_tabs = data.get("newTabs")
            if new_tabs:
                # Newly opened tabs from the executed code — splice in
                # so the next pick_tab can find them.
                seen = {t.get("id") for t in self._tabs}
                for nt in new_tabs:
                    if nt.get("id") not in seen:
                        self._tabs.append(nt)
            if kind == "result":
                fut.set_result(data.get("result"))
            else:
                err = data.get("error") or "(no message)"
                if isinstance(err, dict):
                    err = err.get("message") or str(err)
                fut.set_exception(BridgeError(str(err)))
            return
        log.debug("bridge: ignoring unknown inbound type=%r", kind)
