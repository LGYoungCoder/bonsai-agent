"""Backend Protocol + failover chain.

Adapters implement `chat()` (blocking) and optionally `stream()` (async generator).
The Protocol is intentionally minimal — provider-specific hacks (cache_control,
temperature clamp, SSE quirks) belong in adapters, not here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .cache_monitor import CacheMonitor
from .types import DynamicTail, FrozenPrefix, Response, StreamEvent

log = logging.getLogger(__name__)


@runtime_checkable
class Backend(Protocol):
    name: str  # provider instance name from config
    kind: str  # "claude" | "openai" | "glm" | "qwen" | "minimax" | ...
    model: str

    async def chat(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any) -> Response:
        ...

    async def stream(self, prefix: FrozenPrefix, tail: DynamicTail,
                     **opts: Any) -> AsyncIterator[StreamEvent]:
        ...


@dataclass
class FailoverChain:
    """Sequential backend chain with exponential backoff per provider.

    A backend is skipped for `cooldown` seconds after a failure. All backends
    share the same cache monitor so the UI can see per-provider stats.
    """

    backends: list[Backend]
    monitor: CacheMonitor = field(default_factory=CacheMonitor)
    cooldown: float = 30.0
    _blacklist: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.backends:
            raise ValueError("FailoverChain needs at least one backend")

    def _available(self, now: float) -> list[Backend]:
        return [b for b in self.backends if self._blacklist.get(b.name, 0) <= now]

    def _mark_down(self, name: str, now: float) -> None:
        self._blacklist[name] = now + self.cooldown

    async def chat(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any) -> Response:
        loop = asyncio.get_event_loop()
        last_err: Exception | None = None
        available = self._available(loop.time())
        if not available:
            # Reset blacklist if everything is down — give it another shot.
            self._blacklist.clear()
            available = list(self.backends)
        for b in available:
            try:
                resp = await b.chat(prefix, tail, **opts)
                self.monitor.record(
                    provider=b.name,
                    cache_read=resp.usage.cache_read_tokens,
                    cache_creation=resp.usage.cache_creation_tokens,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    model=resp.model,
                )
                if alert := self.monitor.alert(b.name):
                    log.warning(alert)
                return resp
            except Exception as e:
                log.warning("backend %s failed: %s — falling back", b.name, e)
                self._mark_down(b.name, loop.time())
                last_err = e
                continue
        assert last_err is not None
        raise last_err

    async def stream(self, prefix: FrozenPrefix, tail: DynamicTail,
                     **opts: Any) -> AsyncIterator[StreamEvent]:
        loop = asyncio.get_event_loop()
        available = self._available(loop.time())
        if not available:
            self._blacklist.clear()
            available = list(self.backends)
        last_err: Exception | None = None
        for b in available:
            try:
                async for ev in b.stream(prefix, tail, **opts):
                    if ev.kind == "usage":
                        u = ev.data
                        self.monitor.record(
                            provider=b.name,
                            cache_read=u.cache_read_tokens,
                            cache_creation=u.cache_creation_tokens,
                            input_tokens=u.input_tokens,
                            output_tokens=u.output_tokens,
                            model=b.model,
                        )
                    yield ev
                return
            except Exception as e:
                log.warning("backend %s stream failed: %s — falling back", b.name, e)
                self._mark_down(b.name, loop.time())
                last_err = e
                continue
        assert last_err is not None
        raise last_err
