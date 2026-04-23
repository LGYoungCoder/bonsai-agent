"""BrowserSession-shaped wrapper around BridgeServer.

Exposes the same public surface the handler dispatches to (scan / execute_js /
click / type_text / scroll / navigate / list_tabs / switch_tab / close), so
`Handler(browser=BridgeSession(...))` Just Works.

Differences from CDP-direct BrowserSession:
  - scan returns a pruned-DOM rendering only (no AX tree). The model still
    sees buttons/inputs/links inlined.
  - click/type_text treat the `id` arg as a CSS selector (best-effort). The
    AX-tree short-id pool isn't available through this path.
  - All ops run via JS in the page world, with the extension's CSP→CDP
    fallback handling refused-eval pages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .bridge_server import BridgeServer
from .dom_prune import dom_prune_script

log = logging.getLogger(__name__)


@dataclass
class BridgeSession:
    server: BridgeServer
    _active_tab_id: int | None = field(default=None)

    async def ensure_connected(self, prefer_url_contains: str | None = None) -> int:
        if self._active_tab_id is not None and prefer_url_contains is None:
            return self._active_tab_id
        await self.server.wait_for_extension(timeout=20.0)
        tab = self.server.pick_tab(url_match=prefer_url_contains)
        if tab is None:
            tab = self.server.pick_tab()
        if tab is None:
            raise RuntimeError("bridge: extension reports no scriptable tabs.")
        self._active_tab_id = int(tab["id"])
        return self._active_tab_id

    async def close(self) -> None:
        await self.server.stop()
        self._active_tab_id = None

    async def list_tabs(self) -> str:
        await self.server.wait_for_extension(timeout=5.0)
        tabs = self.server.tabs
        if not tabs:
            return "(no open tabs)"
        lines = [f"[{len(tabs)} tab(s)]"]
        for i, t in enumerate(tabs, 1):
            marker = "*" if t.get("id") == self._active_tab_id else " "
            title = (t.get("title") or "(untitled)")[:60]
            url = (t.get("url") or "")[:80]
            lines.append(f" {marker}{i:>2} {title}  {url}")
        return "\n".join(lines)

    async def switch_tab(self, target_ref: str) -> str:
        await self.server.wait_for_extension(timeout=5.0)
        tabs = self.server.tabs
        if target_ref.isdigit():
            idx = int(target_ref) - 1
            if 0 <= idx < len(tabs):
                t = tabs[idx]
                self._active_tab_id = int(t["id"])
                await self._chrome_focus_tab(self._active_tab_id)
                return f"[switched to] {t.get('title','')} ({t.get('url','')})"
        match = next(
            (t for t in tabs
             if target_ref in (t.get("url") or "")
             or target_ref in (t.get("title") or "")),
            None,
        )
        if not match:
            return f"[error] no tab matches {target_ref!r}"
        self._active_tab_id = int(match["id"])
        await self._chrome_focus_tab(self._active_tab_id)
        return f"[switched to] {match.get('title','')} ({match.get('url','')})"

    async def _chrome_focus_tab(self, tab_id: int) -> None:
        try:
            await self.server.send_cmd(
                {"cmd": "tabs", "method": "switch", "tabId": tab_id},
            )
        except Exception as e:
            log.debug("bridge: tab focus failed: %s", e)

    async def scan(self, *, scope: str | None = None, tabs_only: bool = False,
                   full: bool = False) -> str:
        if tabs_only:
            return await self.list_tabs()
        tab_id = await self.ensure_connected()
        # AX tree not available via plain JS; we use the same DOM-prune
        # pass that BrowserSession falls back to.
        try:
            dom_txt = await self.server.send_code(dom_prune_script(), tab_id)
        except Exception as e:
            return f"[error] bridge scan failed: {e}"
        # Pull URL/title via a tiny inline JS so the model has navigation context.
        try:
            meta = await self.server.send_code(
                "({url: location.href, title: document.title})", tab_id,
            )
        except Exception:
            meta = {}
        header = f"[URL] {meta.get('url','')}\n[title] {meta.get('title','')}"
        body = (dom_txt or "").strip() if isinstance(dom_txt, str) else str(dom_txt or "")
        if not full and len(body) > 12000:
            body = body[:12000] + "\n...[truncated; pass full=true for more]"
        return f"{header}\n\n{body}"

    async def execute_js(self, script: str,
                         *, save_to_file: str | None = None) -> str:
        tab_id = await self.ensure_connected()
        try:
            raw = await self.server.send_code(script, tab_id, timeout=30.0)
        except Exception as e:
            return f"[error] bridge execute_js failed: {e}"
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        if save_to_file:
            from pathlib import Path
            p = Path(save_to_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return f"[ok] result saved to {p} ({len(text)} bytes; preview: {text[:200]})"
        if len(text) > 8000:
            return text[:8000] + "\n...[truncated]"
        return text or "[ok] (no return value)"

    async def click(self, short_id: str) -> str:
        # Treat short_id as a CSS selector. With the bridge we have no AX
        # element pool — the model is expected to read the pruned DOM and
        # pass a selector like "button.submit" or "#login-form button".
        tab_id = await self.ensure_connected()
        sel_js = json.dumps(short_id)
        code = (
            f"(function(){{const el=document.querySelector({sel_js});"
            "if(!el) return '[error] no element matches selector';"
            "el.scrollIntoView({block:'center'}); el.click();"
            f"return '[ok] clicked '+{sel_js};}})()"
        )
        try:
            return str(await self.server.send_code(code, tab_id))
        except Exception as e:
            return f"[error] bridge click failed: {e}"

    async def type_text(self, short_id: str, text: str,
                        *, submit: bool = False) -> str:
        tab_id = await self.ensure_connected()
        sel_js = json.dumps(short_id)
        text_js = json.dumps(text)
        # Set value, then dispatch input + change (covers React/Vue listeners).
        code = (
            f"(function(){{const el=document.querySelector({sel_js});"
            "if(!el) return '[error] no element matches selector';"
            "el.focus();"
            "if('value' in el){"
            f"  el.value={text_js};"
            "  el.dispatchEvent(new Event('input',{bubbles:true}));"
            "  el.dispatchEvent(new Event('change',{bubbles:true}));"
            "} else { el.textContent=" + text_js + "; }"
        )
        if submit:
            code += (
                "el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));"
                "if(el.form){try{el.form.requestSubmit?el.form.requestSubmit():el.form.submit();}catch(e){}}"
            )
        code += f"return '[ok] typed '+({text_js}.length)+' chars into '+{sel_js};}})()"
        try:
            return str(await self.server.send_code(code, tab_id))
        except Exception as e:
            return f"[error] bridge type failed: {e}"

    async def scroll(self, direction: str = "down", *, amount: int = 400) -> str:
        tab_id = await self.ensure_connected()
        dy = amount if direction == "down" else -amount
        code = f"window.scrollBy(0,{dy});'[ok] scrolled by {dy}px'"
        try:
            return str(await self.server.send_code(code, tab_id))
        except Exception as e:
            return f"[error] bridge scroll failed: {e}"

    async def navigate(self, url: str) -> str:
        tab_id = await self.ensure_connected()
        url_js = json.dumps(url)
        code = f"location.href={url_js};'[ok] navigating to '+{url_js}"
        try:
            return str(await self.server.send_code(code, tab_id, timeout=8.0))
        except Exception as e:
            return f"[error] bridge navigate failed: {e}"
