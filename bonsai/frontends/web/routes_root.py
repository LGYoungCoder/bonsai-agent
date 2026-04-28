"""Root-level routes: index, health, bootstrap, websocket chat."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from ._common import _FALLBACK_HTML, _event_to_wire

log = logging.getLogger(__name__)


def make_router(root: Path, chat_factory) -> APIRouter:
    router = APIRouter()
    app_html = root / "assets" / "app.html"

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if app_html.exists():
            return HTMLResponse(app_html.read_text(encoding="utf-8"))
        return HTMLResponse(_FALLBACK_HTML)

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @router.get("/api/bootstrap")
    async def api_bootstrap() -> JSONResponse:
        """Tell the frontend whether config exists so it can default to the
        config tab on first run."""
        from ...cli.setup_wizard import detect_state
        state = detect_state(root)
        return JSONResponse({
            "has_config": state.has_config,
            "has_skills": state.skill_dir_exists and state.skill_l0_exists,
            "has_memory": state.memory_db_exists,
            "is_partial": state.is_partial,
            "version": "0.1",
        })

    @router.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        session_ctx = None
        pending_prompt: asyncio.Future | None = None
        run_task: asyncio.Task | None = None

        async def prompt_user(question: str, candidates: list[str] | None) -> str:
            nonlocal pending_prompt
            pending_prompt = asyncio.get_event_loop().create_future()
            await sock.send_bytes(orjson.dumps({
                "kind": "ask_user",
                "question": question,
                "candidates": candidates or [],
            }))
            return await pending_prompt

        async def drive(loop_obj) -> None:
            try:
                async for ev in loop_obj.run():
                    data = _event_to_wire(ev)
                    await sock.send_bytes(orjson.dumps(data))
            except asyncio.CancelledError:
                # 用户按了停止 / WS 断了。给前端一个收尾事件再退出。
                try:
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "text", "data": "\n[已被用户中止]"}))
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "done", "data": {"reason": "stopped"}}))
                except Exception:
                    pass
                raise
            except Exception as e:
                log.exception("agent loop crashed: %s", e)
                try:
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "error", "data": str(e)}))
                except Exception:
                    pass

        try:
            session_ctx = chat_factory(prompt_user)
            # 一个 ws 连接 = 一次对话。AgentLoop 复用 → self.tail.messages 跨
            # turn 累积上下文。之前每次 user turn 新建 loop → 每次 tail 都是空 →
            # agent 失忆。
            loop = session_ctx.new_loop()
            while True:
                raw = await sock.receive_text()
                msg = orjson.loads(raw)
                kind = msg.get("kind")
                if kind == "user":
                    if run_task and not run_task.done():
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "error", "data": "上一轮还在跑,请先停止"}))
                        continue
                    loop.add_user(msg.get("text", ""))
                    run_task = asyncio.create_task(drive(loop))
                elif kind == "stop":
                    if run_task and not run_task.done():
                        run_task.cancel()
                elif kind == "resume":
                    # 切到某条历史会话继续聊。把旧 .jsonl 的消息塞进新 loop 的
                    # tail,SessionLog 改成 append 到同一个文件。
                    sid = msg.get("sid", "")
                    try:
                        pre = session_ctx.resume(sid)
                        loop = session_ctx.new_loop(pre_messages=pre)
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "resumed", "sid": sid, "turns": len(pre)}))
                    except Exception as e:
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "error", "data": f"resume failed: {e}"}))
                elif kind == "new":
                    # 开新会话 — 换 session_id + 换新 log 文件 + 清空 tail。
                    session_ctx.reset()
                    loop = session_ctx.new_loop()
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "new_session", "sid": session_ctx.session_id}))
                elif kind == "ask_user_reply" and pending_prompt is not None:
                    pending_prompt.set_result(msg.get("reply", ""))
                    pending_prompt = None
        except WebSocketDisconnect:
            log.info("ws disconnected")
        except Exception as e:
            log.exception("ws error: %s", e)
            try:
                await sock.send_bytes(orjson.dumps({"kind": "error", "data": str(e)}))
            except Exception:
                pass
        finally:
            if run_task and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except BaseException:
                    pass
            if session_ctx:
                session_ctx.cleanup()

    return router
