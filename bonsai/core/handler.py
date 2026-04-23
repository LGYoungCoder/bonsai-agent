"""Tool dispatcher. Core responsibility: run tool calls (possibly parallel),
collect results, flag conflicts, manage working memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.ask_user import ask_user
from ..tools.code_run import code_run
from ..tools.file_read import file_read
from ..tools.file_write import file_write
from ..tools.memory_search import memory_recall, memory_search
from ..tools.skill_lookup import skill_lookup
from .session import Session
from .types import ToolCall, ToolResult

log = logging.getLogger(__name__)


@dataclass
class StepOutcome:
    tool_result: ToolResult
    should_exit: bool = False


UserPromptFn = Callable[[str, list[str] | None], Awaitable[str]]


def _normalize_write_path(args: dict, cwd: Path) -> str:
    p = args.get("path", "")
    if not p:
        return ""
    path = Path(p)
    return str((cwd / path).resolve() if not path.is_absolute() else path)


def _conflicts(calls: list[ToolCall], cwd: Path) -> list[set[int]]:
    """Return groups of call indices that must be serialized against each other.

    Rules (conservative, easy to reason about):
      • Two file_writes to the same path → serial
      • file_write + file_read to the same path → serial (read after write or vice versa)
      • Two code_run in the same cwd → serial
      • ask_user calls are always serial last (they pause the loop)
    """
    n = len(calls)
    groups: list[set[int]] = []
    # Group same target paths
    path_buckets: dict[str, set[int]] = {}
    run_buckets: dict[str, set[int]] = {}
    user_idxs: set[int] = set()
    for i, tc in enumerate(calls):
        if tc.name == "file_write" or tc.name == "file_read":
            p = _normalize_write_path(tc.args, cwd)
            path_buckets.setdefault(p, set()).add(i)
        elif tc.name == "code_run":
            wd = tc.args.get("cwd", ".")
            run_buckets.setdefault(wd, set()).add(i)
        elif tc.name == "ask_user":
            user_idxs.add(i)

    for bucket in path_buckets.values():
        # Serialize only if any are writes; two reads are safe.
        has_write = any(calls[i].name == "file_write" for i in bucket)
        if has_write and len(bucket) > 1:
            groups.append(bucket)
    for bucket in run_buckets.values():
        if len(bucket) > 1:
            groups.append(bucket)
    if user_idxs:
        groups.append(user_idxs | set(range(n)))  # ask_user drains everything
    return groups


def _display_available() -> bool:
    """True if we likely have a GUI display for headful chromium."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@dataclass
class Handler:
    session: Session
    schema_path: Path | None = None
    prompt_fn: UserPromptFn | None = None
    # Sprint 2 add-ons — optional so Sprint 1 tests still pass.
    memory_store: Any = None      # type: MemoryStore | None
    skill_store: Any = None       # type: SkillStore | None
    browser: Any = None           # type: BrowserSession | None (Sprint 4)
    evidence: Any = None          # type: EvidenceRecorder | None
    # Zero-config browser: if None when the model first calls a web_* tool,
    # we transparently spawn a managed chromium. Fail-once semantics
    # so a missing chromium binary doesn't re-retry on every turn.
    # Default: headful if a display is available (interactive dev box),
    # headless on bare servers. IM bot / scheduler override with True so
    # autonomous runs don't pop windows.
    browser_headless: bool | None = None
    _browser_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False,
                                         repr=False, compare=False)
    _browser_init_failed: str | None = field(default=None, init=False,
                                              repr=False, compare=False)

    async def dispatch(self, call: ToolCall) -> StepOutcome:
        t0 = time.monotonic()
        outcome = await self._dispatch(call)
        if self.evidence is not None:
            try:
                self.evidence.record(
                    turn=self.session.turns,
                    tool=call.name,
                    args=call.args or {},
                    ok=not outcome.tool_result.is_error,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    err=outcome.tool_result.content if outcome.tool_result.is_error else None,
                )
            except Exception:
                log.exception("evidence record failed")
        return outcome

    async def _dispatch(self, call: ToolCall) -> StepOutcome:
        name = call.name
        args = call.args or {}
        log.debug("dispatch %s %s", name, args)
        try:
            if name == "file_read":
                out = file_read(
                    path=args["path"],
                    start=args.get("start", 1),
                    count=args.get("count", 200),
                    keyword=args.get("keyword"),
                    cwd=self.session.cwd,
                )
                return StepOutcome(ToolResult(call.id, out))

            if name == "file_write":
                out = file_write(
                    path=args["path"],
                    mode=args.get("mode", "patch"),
                    new_content=args["new_content"],
                    old_content=args.get("old_content"),
                    cwd=self.session.cwd,
                )
                return StepOutcome(ToolResult(call.id, out))

            if name == "code_run":
                out = await code_run(
                    code=args.get("code", ""),
                    type=args.get("type", "python"),
                    timeout=args.get("timeout", 60),
                    cwd=self.session.cwd / args.get("cwd", "."),
                    interest_hint=args.get("interest_hint"),
                    artifact_dir=self.session.artifact_dir(),
                )
                return StepOutcome(ToolResult(call.id, out))

            if name == "memory_search":
                out = memory_search(
                    query=args["query"],
                    wing=args.get("wing"),
                    room=args.get("room"),
                    n=args.get("n", 5),
                    store=self.memory_store,
                )
                return StepOutcome(ToolResult(call.id, out))

            if name == "memory_recall":
                out = memory_recall(
                    wing=args.get("wing"),
                    room=args.get("room"),
                    limit=args.get("limit", 5),
                    store=self.memory_store,
                )
                return StepOutcome(ToolResult(call.id, out))

            if name == "skill_lookup":
                if self.skill_store is None:
                    return StepOutcome(ToolResult(call.id,
                        "[skill_lookup] SkillStore not initialized"))
                out = skill_lookup(keyword=args["keyword"], store=self.skill_store)
                return StepOutcome(ToolResult(call.id, out))

            if name in ("web_scan", "web_execute_js", "web_click", "web_type",
                        "web_scroll", "web_navigate"):
                if self.browser is None:
                    await self._ensure_browser()
                if self.browser is None:
                    hint = self._browser_init_failed or "unknown error"
                    return StepOutcome(ToolResult(call.id,
                        f"[browser] 自动启动 chromium 失败: {hint}。"
                        "装 chromium/google-chrome 后再试,或 `bonsai chat "
                        "--browser attach` 接管已启动的 Chrome。",
                        is_error=True))
                out = await _dispatch_browser(self.browser, name, args)
                return StepOutcome(ToolResult(call.id, out))

            if name == "ask_user":
                out = await ask_user(
                    question=args["question"],
                    candidates=args.get("candidates"),
                    prompt_fn=self.prompt_fn,
                )
                # ask_user always yields a user reply — the loop treats this as
                # a signal to rebuild messages with that reply and continue.
                return StepOutcome(ToolResult(call.id, out), should_exit=False)

            return StepOutcome(ToolResult(call.id, f"[error] unknown tool: {name}", is_error=True))

        except KeyError as e:
            return StepOutcome(ToolResult(call.id, f"[error] missing argument: {e}", is_error=True))
        except Exception as e:
            log.exception("tool %s failed", name)
            return StepOutcome(ToolResult(call.id, f"[error] {type(e).__name__}: {e}", is_error=True))

    async def _ensure_browser(self) -> None:
        """Lazy-spawn a headless managed chromium on first web_* call.
        Fail-once: if the chromium binary is missing we remember and stop
        retrying within this session so every turn isn't a slow timeout.
        """
        if self.browser is not None or self._browser_init_failed:
            return
        async with self._browser_lock:
            if self.browser is not None or self._browser_init_failed:
                return
            try:
                from ..tools.browser import BrowserSession
                headless = (self.browser_headless if self.browser_headless is not None
                            else not _display_available())
                self.browser = await BrowserSession.managed(headless=headless)
                log.info("lazy browser: managed chromium at %s (headless=%s)",
                         getattr(self.browser, "debug_url", "?"), headless)
            except Exception as e:
                self._browser_init_failed = f"{type(e).__name__}: {e}"
                log.warning("lazy browser init failed: %s", self._browser_init_failed)

    async def dispatch_batch(self, calls: list[ToolCall]) -> list[StepOutcome]:
        """Run tool calls in parallel unless they conflict.

        Conflicting groups are serialized; independent calls run concurrently.
        """
        if not calls:
            return []
        n = len(calls)
        conflict_groups = _conflicts(calls, self.session.cwd)

        # Union-find over conflicting indices
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for group in conflict_groups:
            items = sorted(group)
            for a, b in zip(items, items[1:], strict=False):
                union(a, b)

        # Bucket by root
        buckets: dict[int, list[int]] = {}
        for i in range(n):
            buckets.setdefault(find(i), []).append(i)

        results: list[StepOutcome | None] = [None] * n

        async def run_serial(idxs: list[int]) -> None:
            for i in idxs:
                results[i] = await self.dispatch(calls[i])

        await asyncio.gather(*(run_serial(idxs) for idxs in buckets.values()))
        return [r for r in results if r is not None]


async def _dispatch_browser(browser: Any, name: str, args: dict) -> str:
    if name == "web_scan":
        return await browser.scan(
            scope=args.get("scope"),
            tabs_only=bool(args.get("tabs_only")),
            full=bool(args.get("full")),
            switch_tab_id=args.get("switch_tab_id"),
        )
    if name == "web_execute_js":
        script = args.get("script") or args.get("code") or ""
        return await browser.execute_js(
            script, save_to_file=args.get("save_to_file"),
            switch_tab_id=args.get("switch_tab_id"),
        )
    if name == "web_click":
        return await browser.click(args["id"])
    if name == "web_type":
        return await browser.type_text(args["id"], args["text"],
                                        submit=bool(args.get("submit")))
    if name == "web_scroll":
        return await browser.scroll(direction=args.get("direction", "down"),
                                     amount=int(args.get("amount", 400)))
    if name == "web_navigate":
        return await browser.navigate(args["url"],
                                       new_tab=bool(args.get("new_tab")))
    return f"[error] unknown browser op: {name}"
