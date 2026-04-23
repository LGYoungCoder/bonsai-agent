"""High-level BrowserSession — wraps CDPClient + element pool + AX rendering.

Exposed to the handler via `handler.browser`. Tool functions call through this.

Two launch modes:
  • attach  — connect to a Chrome started by the user with
              --remote-debugging-port=9222. Login state preserved.
  • managed — BrowserSession owns an isolated chromium subprocess
              (dedicated profile dir). No setup, but no shared login.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .ax_tree import ax_tree_to_text
from .cdp_client import CDPClient, Target
from .dom_prune import dom_prune_script
from .element_pool import ElementPool
from .managed import ManagedChromium

log = logging.getLogger(__name__)


@dataclass
class BrowserSession:
    debug_url: str = "http://127.0.0.1:9222"
    client: CDPClient = field(init=False)
    pool: ElementPool = field(default_factory=ElementPool)
    _connected: bool = False
    _managed: ManagedChromium | None = None

    def __post_init__(self) -> None:
        self.client = CDPClient(self.debug_url)

    @classmethod
    async def managed(cls, *, headless: bool = False,
                      profile_dir: str | None = None) -> "BrowserSession":
        """Spawn an isolated chromium and attach to it.

        Caller must await session.close() to shut the subprocess down.
        """
        from pathlib import Path as _P
        m = ManagedChromium(
            headless=headless,
            profile_dir=_P(profile_dir) if profile_dir else ManagedChromium().profile_dir,
        )
        url = await m.start()
        sess = cls(debug_url=url)
        sess._managed = m
        return sess

    async def ensure_connected(self, prefer_url_contains: str | None = None) -> Target:
        if self._connected and self.client.target is not None:
            return self.client.target
        targets = await self.client.list_targets()
        page_targets = [t for t in targets if t.type == "page"]
        if not page_targets:
            page_targets = [await self.client.new_target("about:blank")]
        if prefer_url_contains:
            matched = [t for t in page_targets if prefer_url_contains in t.url]
            if matched:
                page_targets = matched
        await self.client.attach(page_targets[0])
        self._connected = True
        return page_targets[0]

    async def close(self) -> None:
        await self.client.detach()
        self._connected = False
        if self._managed is not None:
            await self._managed.close()
            self._managed = None

    # ---- high-level ops ------------------------------------------------
    async def list_tabs(self) -> str:
        targets = await self.client.list_targets()
        pages = [t for t in targets if t.type == "page"]
        if not pages:
            return "(no open tabs)"
        lines = [f"[{len(pages)} tab(s)]"]
        for i, t in enumerate(pages, 1):
            marker = "*" if self.client.target and self.client.target.id == t.id else " "
            title = (t.title or "(untitled)")[:60]
            lines.append(f" {marker}{i:>2} {title}  {t.url[:80]}")
        return "\n".join(lines)

    async def switch_tab(self, target_ref: str) -> str:
        """target_ref: url substring or integer index from list_tabs."""
        targets = [t for t in await self.client.list_targets() if t.type == "page"]
        if target_ref.isdigit():
            idx = int(target_ref) - 1
            if 0 <= idx < len(targets):
                await self.client.attach(targets[idx])
                self._connected = True
                return f"[switched to] {targets[idx].title} ({targets[idx].url})"
        match = next((t for t in targets if target_ref in t.url or target_ref in t.title), None)
        if not match:
            return f"[error] no tab matches {target_ref!r}"
        await self.client.attach(match)
        self._connected = True
        return f"[switched to] {match.title} ({match.url})"

    async def scan(self, *, scope: str | None = None, tabs_only: bool = False,
                   full: bool = False) -> str:
        if tabs_only:
            return await self.list_tabs()
        await self.ensure_connected()
        if not full and scope is None:
            self.pool.reset()
        # Get AX tree
        result = await self.client.send("Accessibility.getFullAXTree")
        nodes = result.get("nodes") or []
        # If scope is given, it maps back to a short_id; resolve.
        scope_ax_id = None
        if scope:
            entry = self.pool.resolve(scope)
            if entry:
                scope_ax_id = entry["ax_node_id"]
            else:
                return f"[error] unknown scope id: {scope}"
        max_nodes = 800 if full else 250
        text = ax_tree_to_text(nodes, self.pool, scope_id=scope_ax_id,
                               max_nodes=max_nodes)
        # Also include URL + title
        info = await self.client.send("Page.getNavigationHistory")
        cur = info.get("entries", [])[info.get("currentIndex", 0)] if info else {}
        header = f"[URL] {cur.get('url', '')}\n[title] {cur.get('title', '')}"

        # AX Tree can be nearly empty on canvas-heavy / custom-widget pages
        # (figma, google docs, certain SPAs). If the rendering is obviously
        # thin, tack on a pruned DOM snapshot so the model still sees content.
        if _is_thin(text):
            dom_txt = await self._scan_dom_pruned()
            if dom_txt:
                return f"{header}\n\n{text}\n\n[dom-fallback]\n{dom_txt}"
        return f"{header}\n\n{text}"

    async def _scan_dom_pruned(self) -> str:
        try:
            result = await self.client.send("Runtime.evaluate", {
                "expression": dom_prune_script(),
                "returnByValue": True,
                "awaitPromise": False,
            })
            val = (result.get("result") or {}).get("value")
            return (val or "").strip() if isinstance(val, str) else ""
        except Exception as e:
            log.debug("dom prune fallback failed: %s", e)
            return ""

    async def execute_js(self, script: str, *, save_to_file: str | None = None) -> str:
        await self.ensure_connected()
        # Rewrite a1/a2 references if any: `selectById('a3')` →
        # `document.querySelector('[data-backend-node-id="XX"]')`-style.
        # Simplest path: inject a helper once and let model reference via our
        # global bonsai_el(id) function.
        helper = self._build_helper_script()
        full = f"{helper}\n{script}"
        result = await self.client.send("Runtime.evaluate", {
            "expression": full,
            "returnByValue": True,
            "awaitPromise": True,
        })
        val = result.get("result") or {}
        if val.get("type") == "undefined":
            return "[ok] (undefined)"
        raw = str(val.get("value", val.get("description", "")))
        if save_to_file:
            from pathlib import Path
            p = Path(save_to_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf-8")
            return f"[ok] result saved to {p} ({len(raw)} bytes; preview: {raw[:200]})"
        return raw[:8000] + ("\n...[truncated]" if len(raw) > 8000 else "")

    def _build_helper_script(self) -> str:
        mapping = {sid: e["backend_node_id"] for sid, e in self.pool.entries.items()
                   if e.get("backend_node_id")}
        import json as _j
        return (
            f"window.__bonsai_map__ = {_j.dumps(mapping)};\n"
            "window.bonsai_el = function(id) {\n"
            "  const bn = window.__bonsai_map__[id];\n"
            "  if (!bn) throw new Error('unknown id: '+id);\n"
            "  return document.querySelector(`[bonsai-bn=\"${bn}\"]`) || null;\n"
            "};\n"
        )

    # ---- simple action helpers ----------------------------------------
    async def click(self, short_id: str) -> str:
        entry = self.pool.resolve(short_id)
        if not entry or not entry.get("backend_node_id"):
            return f"[error] unknown or un-clickable id: {short_id}"
        bn = entry["backend_node_id"]
        try:
            result = await self.client.send("DOM.getBoxModel",
                                             {"backendNodeId": bn})
            box = result.get("model", {}).get("content")
            if not box or len(box) < 2:
                return "[error] element has no box"
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[5]) / 2
            for event in ("mousePressed", "mouseReleased"):
                await self.client.send("Input.dispatchMouseEvent", {
                    "type": event, "x": cx, "y": cy,
                    "button": "left", "clickCount": 1,
                })
            return f"[ok] clicked {short_id} ({entry['role']} {entry['name']!r})"
        except Exception as e:
            return f"[error] click failed: {e}"

    async def type_text(self, short_id: str, text: str, *, submit: bool = False) -> str:
        entry = self.pool.resolve(short_id)
        if not entry:
            return f"[error] unknown id: {short_id}"
        await self.client.send("DOM.focus", {"backendNodeId": entry["backend_node_id"]})
        for ch in text:
            await self.client.send("Input.insertText", {"text": ch})
        if submit:
            await self.client.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
            })
            await self.client.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
            })
        return f"[ok] typed {len(text)} chars into {short_id}"

    async def scroll(self, direction: str = "down", *, amount: int = 400) -> str:
        await self.ensure_connected()
        dy = amount if direction == "down" else -amount
        await self.client.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 300, "y": 300, "deltaX": 0, "deltaY": dy,
        })
        return f"[ok] scrolled {direction} by {amount}px"

    async def navigate(self, url: str) -> str:
        await self.ensure_connected()
        await self.client.send("Page.navigate", {"url": url})
        return f"[ok] navigating to {url}"


_THIN_LINE_THRESHOLD = 20  # AX rendering with fewer lines → try DOM fallback


def _is_thin(text: str) -> bool:
    if not text:
        return True
    non_blank = [ln for ln in text.splitlines() if ln.strip()]
    return len(non_blank) < _THIN_LINE_THRESHOLD
