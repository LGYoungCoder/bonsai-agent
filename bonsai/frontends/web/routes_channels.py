"""Channel runner lifecycle + per-channel auth flows (wechat QR, etc).

NOTE on route order: the wechat-specific endpoints (`/api/channels/wechat/*`)
must be registered BEFORE the generic runner endpoints
(`/api/channels/{kind}/runner/*`). FastAPI matches routes in registration
order; if `{kind}` came first it would happily eat `wechat` and the QR /
status / logout endpoints would never get hit. Keep the @router decorators
in this file in the same order they appear here."""

from __future__ import annotations

import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ._common import _preserve_secrets_single, _render_qr_svg

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/channels/kinds")
    async def api_channel_kinds() -> JSONResponse:
        from ...channels.registry import KINDS
        return JSONResponse({"kinds": [
            {"kind": s.kind, "label": s.label, "fields": s.fields,
             "required": s.required, "login_mode": s.login_mode, "docs": s.docs}
            for s in KINDS.values()
        ]})

    # ─────────── WeChat-specific (must come before {kind}/runner/* below) ───────────

    @router.post("/api/channels/wechat/login_start")
    async def api_wechat_login_start() -> JSONResponse:
        from ...channels.wechat_client import WxBotClient
        bot = WxBotClient(root / "data" / "wechat_token.json")
        try:
            qr = bot.login_qr_start()
        except Exception as e:
            raise HTTPException(500, f"iLink 无法获取二维码: {e}")
        return JSONResponse({
            "qr_id": qr.qr_id,
            "qr_image_url": qr.qr_image_url,
            "qr_svg": _render_qr_svg(qr.qr_image_url),
        })

    @router.get("/api/channels/wechat/login_status")
    async def api_wechat_login_status(qr_id: str) -> JSONResponse:
        from ...channels.wechat_client import WxBotClient
        bot = WxBotClient(root / "data" / "wechat_token.json")
        try:
            cur = bot.login_qr_poll(qr_id)
        except Exception as e:
            raise HTTPException(500, f"轮询失败: {e}")
        # 扫码确认成功 → 必须停掉还在跑的旧 runner。它装着旧 bot_token 在内存里,
        # 新 token 在文件里,runner 不知道要切;结果是新号消息发过来 iLink 路由到
        # 新 bot,旧 runner 永远空转 → 用户看到「登录成功但没反应」。
        if cur.status == "confirmed":
            from ...channels.supervisor import stop as sv_stop, status as sv_status
            st = sv_status(root, "wechat")
            if st.get("running"):
                try:
                    sv_stop(root, "wechat")
                    log.info("stopped old wechat runner after new QR login (pid %s)",
                             st.get("pid"))
                except Exception as e:
                    log.warning("failed to stop old wechat runner: %s", e)
        return JSONResponse({
            "status": cur.status,
            "bot_id": cur.ilink_bot_id,
            "logged_in": bot.logged_in,
        })

    @router.get("/api/channels/wechat/status")
    async def api_wechat_status() -> JSONResponse:
        from ...channels.wechat_client import WxBotClient, WxToken
        tf = root / "data" / "wechat_token.json"
        t = WxToken.load(tf)
        return JSONResponse({
            "logged_in": bool(t.bot_token),
            "bot_id": t.ilink_bot_id,
            "login_time": t.login_time,
            "token_file": str(tf),
        })

    @router.post("/api/channels/wechat/logout")
    async def api_wechat_logout() -> JSONResponse:
        tf = root / "data" / "wechat_token.json"
        if tf.exists():
            tf.unlink()
        return JSONResponse({"ok": True})

    # ─────────── Generic runner lifecycle (supervisor) ───────────

    @router.get("/api/channels/{kind}/runner/status")
    async def api_runner_status(kind: str) -> JSONResponse:
        from ...channels.supervisor import status
        return JSONResponse(status(root, kind))

    @router.post("/api/channels/{kind}/runner/start")
    async def api_runner_start(kind: str, request: Request) -> JSONResponse:
        from ...channels.supervisor import start
        try:
            body = orjson.loads(await request.body() or b"{}")
        except Exception:
            body = {}
        allow = body.get("allow", "") or ""
        try:
            st = start(root, kind, allow=allow)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.exception("runner start failed")
            raise HTTPException(500, f"启动失败: {e}")
        return JSONResponse(st)

    @router.post("/api/channels/{kind}/runner/stop")
    async def api_runner_stop(kind: str) -> JSONResponse:
        from ...channels.supervisor import stop
        try:
            return JSONResponse(stop(root, kind))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/api/channels/{kind}/runner/log")
    async def api_runner_log(kind: str, lines: int = 200) -> JSONResponse:
        from ...channels.supervisor import log_tail
        return JSONResponse({"text": log_tail(root, kind, lines=lines)})

    @router.post("/api/channels/test")
    async def api_channel_test(request: Request) -> JSONResponse:
        body = orjson.loads(await request.body())
        kind = body.get("kind") or ""
        cfg = body.get("cfg") or {}
        # if any secret is the redaction marker, pull real value from disk
        _preserve_secrets_single(root, kind, cfg)
        from ...channels.registry import get_adapter
        try:
            adapter = get_adapter(kind)
        except KeyError as e:
            raise HTTPException(400, str(e))
        res = adapter.test(cfg)
        return JSONResponse({"ok": res.ok, "message": res.message})

    return router
