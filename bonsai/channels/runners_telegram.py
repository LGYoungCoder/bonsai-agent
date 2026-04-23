"""Telegram runner — long-polling bot bridge to AgentLoop.

Talks directly to https://api.telegram.org/bot<TOKEN>/ — no third-party SDK.
Uses the shared chat_runtime helpers (sessions, command dispatch, markdown
sanitizing, message splitting) so nothing here reimplements agent logic.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Config
from ..runtime import build_agent
from .chat_runtime import (
    PerUserSessions,
    install_shutdown_flush,
    install_maintenance,
    dispatch_command,
    drive_turn,
    md_to_plain,
    split_for_im,
)

log = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_POLL_TIMEOUT = 25   # seconds; Telegram caps at 50
_REQUEST_TIMEOUT = 35  # client-side ceiling (polling + slack)
_TG_SPLIT_LIMIT = 3800  # Telegram hard limit is 4096 chars; leave slack for our prefix


def run_telegram(root: Path, cfg: Config, *,
                 allowed_users: set[str] | None = None) -> None:
    """Block and run the Telegram bot long-poll loop.

    Config shape (config.toml):
        [channels.telegram]
        enabled = true
        bot_token = "$ref:env:TG_BOT_TOKEN"
        allowed_users = "12345,67890"   # Telegram numeric user_id (not username)
    """
    tg_cfg = (cfg.channels or {}).get("telegram") or {}
    token = (tg_cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError(
            "channels.telegram.bot_token is empty. Run `bonsai setup` or "
            "edit config.toml.")

    # Prefer explicit allow list from the caller (CLI --allow); fall back
    # to config. Both empty = open to anyone who finds the bot (not safe).
    if allowed_users is None:
        raw = str(tg_cfg.get("allowed_users") or "").strip()
        if raw and raw != "*":
            allowed_users = {u.strip() for u in raw.split(",") if u.strip()}

    sys_prompt = (
        "你现在通过 Telegram 跟用户对话。\n"
        "- 用户看到的是 Telegram 消息文本,Markdown 渲染有限;不要用复杂 Markdown;\n"
        "- 回复尽量简洁,必要时分段;\n"
        "- 用户可发 /help /new /stop /status 等命令。"
    )
    print(f"[telegram] building agent (token=***{token[-6:]})", flush=True)
    ctx = build_agent(root, cfg, system_prompt=sys_prompt)
    sessions = PerUserSessions(ctx, root)
    install_shutdown_flush(sessions)
    install_maintenance(root, cfg)
    aio = asyncio.new_event_loop()

    # Verify credentials up front (fail fast, not on first message).
    me = _call(token, "getMe", {})
    bot_username = (me or {}).get("username", "?")
    print(f"[telegram] runner online, @{bot_username}, entering poll loop",
          flush=True)

    offset: int | None = None
    tick = 0
    try:
        while True:
            tick += 1
            t0 = time.time()
            params: dict[str, Any] = {"timeout": _POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = _call(token, "getUpdates", params,
                                timeout=_REQUEST_TIMEOUT) or []
            except Exception as e:
                print(f"[telegram] getUpdates failed: {e!r} — retry in 5s",
                      flush=True)
                time.sleep(5)
                continue
            dur = time.time() - t0
            print(f"[telegram] tick #{tick} got {len(updates)} update(s) in {dur:.1f}s",
                  flush=True)
            for upd in updates:
                offset = max(offset or 0, int(upd.get("update_id", 0)) + 1)
                try:
                    _handle_update(token, upd, sessions, cfg, aio,
                                    allowed_users=allowed_users)
                except Exception as e:
                    print(f"[telegram] handle_update crashed: {e!r}", flush=True)
                    import traceback
                    traceback.print_exc()
    finally:
        aio.close()


def _handle_update(token: str, upd: dict, sessions: PerUserSessions,
                   cfg: Config, aio: asyncio.AbstractEventLoop,
                   *, allowed_users: set[str] | None) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    uid = str((msg.get("from") or {}).get("id", ""))
    if not uid:
        return
    if allowed_users and uid not in allowed_users:
        log.info("drop msg from tg uid=%s (not in allow list)", uid)
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        _send_text(token, chat_id,
                    "（暂不支持此消息类型，请发文字或/help）")
        return

    _print_inbound(uid, chat_id, text)

    if text.startswith("/"):
        reply = dispatch_command(text, uid=uid, sessions=sessions, cfg=cfg)
        if reply:
            _send_text(token, chat_id, reply.text)
            return

    us = sessions.get_or_create(uid)
    if not us.preamble_used and us.profile:
        pre = us.profile.preamble()
        if pre:
            text = pre + text
        us.preamble_used = True

    # Typing indicator heartbeat — Telegram expires it after 5s.
    typing_stop = threading.Event()

    def _typing_hb():
        while not typing_stop.is_set():
            try:
                _call(token, "sendChatAction",
                      {"chat_id": chat_id, "action": "typing"})
            except Exception:
                pass
            if typing_stop.wait(4.0):
                break

    hb = threading.Thread(target=_typing_hb, daemon=True)
    hb.start()
    try:
        reply_raw, aborted = aio.run_until_complete(drive_turn(us, text))
    except Exception:
        log.exception("turn failed for tg uid=%s", uid)
        _send_text(token, chat_id,
                    "抱歉，agent 遇到了临时错误。换个说法再试一次，或发 /new 重开会话。")
        return
    finally:
        typing_stop.set()
        hb.join(timeout=1.0)

    pretty = md_to_plain(reply_raw) or "(已完成)"
    if aborted:
        pretty += "\n\n(用户要求中止，回合提前结束)"
    for chunk in split_for_im(pretty, limit=_TG_SPLIT_LIMIT):
        _send_text(token, chat_id, chunk)
        time.sleep(0.25)


def _call(token: str, method: str, params: dict[str, Any],
          *, timeout: float = 10.0) -> Any:
    url = f"{_API}/bot{token}/{method}"
    with httpx.Client(timeout=timeout) as cli:
        r = cli.post(url, json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram {method} failed: {data.get('description')} "
            f"(code {data.get('error_code')})")
    return data.get("result")


def _send_text(token: str, chat_id: Any, text: str) -> None:
    try:
        _call(token, "sendMessage",
              {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True})
    except Exception as e:
        log.error("tg sendMessage failed: %s", e)


def _print_inbound(uid: str, chat_id: Any, text: str) -> None:
    head = f"[telegram] uid={uid} chat={chat_id} @ {time.strftime('%H:%M:%S')}"
    body = text.strip() or "(无文字)"
    lines = [head]
    for ln in body.splitlines() or [body]:
        lines.append(f"  │ {ln}")
    print("\n".join(lines), flush=True)
