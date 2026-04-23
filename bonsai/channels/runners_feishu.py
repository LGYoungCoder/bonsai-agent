"""Feishu (Lark) runner — long-connection event subscription via lark-oapi.

Requires the optional `lark-oapi` package:
    pip install lark-oapi

No webhook / public IP needed — uses Feishu's official long-connection
WebSocket subscription. Text-only MVP; multimodal left for a follow-up.

Config (config.toml):
    [channels.feishu]
    enabled = true
    app_id = "cli_..."
    app_secret = "$ref:env:FEISHU_APP_SECRET"
    allowed_users = "ou_xxx,ou_yyy"   # open_id; * / empty = open
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

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

_FS_SPLIT_LIMIT = 3500


def _try_import_lark():
    try:
        import lark_oapi as lark  # type: ignore
        from lark_oapi.ws.client import Client as WSClient  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "lark-oapi not installed. Run `pip install lark-oapi` "
            "(or `pip install bonsai-agent[feishu]`)."
        ) from e
    return lark, WSClient


def run_feishu(root: Path, cfg: Config, *,
               allowed_users: set[str] | None = None) -> None:
    fs_cfg = (cfg.channels or {}).get("feishu") or {}
    app_id = str(fs_cfg.get("app_id") or "").strip()
    app_secret = str(fs_cfg.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError(
            "channels.feishu.app_id / app_secret missing. Run `bonsai setup` "
            "or edit config.toml.")
    if allowed_users is None:
        raw = str(fs_cfg.get("allowed_users") or "").strip()
        if raw and raw != "*":
            allowed_users = {u.strip() for u in raw.split(",") if u.strip()}

    lark, WSClient = _try_import_lark()

    sys_prompt = (
        "你现在通过飞书 (Lark) 跟用户对话。\n"
        "- 用户看到的是飞书消息,支持简单 Markdown 但不要滥用;\n"
        "- 回复尽量简洁;长回复会被自动切分;\n"
        "- 用户可发 /help /new /stop /status 等命令。"
    )
    print(f"[feishu] building agent (app_id=***{app_id[-6:]})", flush=True)
    ctx = build_agent(root, cfg, system_prompt=sys_prompt)
    sessions = PerUserSessions(ctx, root)
    install_shutdown_flush(sessions)
    install_maintenance(root, cfg)
    aio = asyncio.new_event_loop()

    # Keep a feishu client handle for sending replies (separate REST API).
    from lark_oapi.api.im.v1 import (  # type: ignore  # noqa: I001
        CreateMessageRequest,
        CreateMessageRequestBody,
    )
    rest = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def send_text(receive_id: str, text: str, *, reply_to_root: str | None = None,
                  receive_id_type: str = "open_id") -> None:
        for chunk in split_for_im(text, limit=_FS_SPLIT_LIMIT):
            body = CreateMessageRequestBody.builder() \
                .receive_id(receive_id) \
                .msg_type("text") \
                .content(json.dumps({"text": chunk}, ensure_ascii=False)) \
                .build()
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(body) \
                .build()
            try:
                rest.im.v1.message.create(req)
            except Exception as e:
                log.error("feishu send failed: %s", e)
                break

    def on_msg(data) -> None:
        """p2_im_message_receive_v1 handler."""
        try:
            event = data.event
            message = event.message
            sender = event.sender
            uid = sender.sender_id.open_id if sender and sender.sender_id else ""
            if not uid:
                return
            if allowed_users and uid not in allowed_users:
                log.info("drop feishu msg from %s (not in allow list)", uid)
                return
            msg_type = getattr(message, "message_type", "")
            if msg_type != "text":
                send_text(uid, "（暂不支持此消息类型，请发文字或 /help）")
                return
            content_raw = json.loads(message.content or "{}")
            text = (content_raw.get("text") or "").strip()
            # Strip "@bot" mentions that show up as markup.
            text = _strip_mentions(text)
            if not text:
                return
            chat_id = getattr(message, "chat_id", "") or uid
            _print_inbound(uid, chat_id, text)

            if text.startswith("/"):
                reply = dispatch_command(text, uid=uid, sessions=sessions, cfg=cfg)
                if reply:
                    send_text(uid, reply.text)
                return

            us = sessions.get_or_create(uid)
            if not us.preamble_used and us.profile:
                pre = us.profile.preamble()
                if pre:
                    text = pre + text
                us.preamble_used = True

            try:
                reply_raw, aborted = aio.run_until_complete(drive_turn(us, text))
            except Exception:
                log.exception("turn failed for feishu uid=%s", uid)
                send_text(uid, "抱歉，agent 遇到临时错误。换个说法再试，或发 /new 重开会话。")
                return
            pretty = md_to_plain(reply_raw) or "(已完成)"
            if aborted:
                pretty += "\n\n(用户要求中止，回合提前结束)"
            send_text(uid, pretty)
        except Exception:
            log.exception("feishu on_msg failed")

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_msg)
        .build()
    )
    ws = WSClient(app_id, app_secret, event_handler=event_handler,
                  log_level=lark.LogLevel.INFO)
    print("[feishu] runner online, starting WS subscription", flush=True)
    try:
        ws.start()
    finally:
        aio.close()


def _strip_mentions(text: str) -> str:
    """Drop @bot mentions ('@_user_1', bot markup) from inbound text."""
    import re
    out = re.sub(r"@_user_\d+", "", text)
    return out.strip()


def _print_inbound(uid: str, chat_id: str, text: str) -> None:
    head = f"[feishu] uid={uid[:10]} chat={chat_id[:10]} @ {time.strftime('%H:%M:%S')}"
    body = text.strip() or "(无文字)"
    lines = [head]
    for ln in body.splitlines() or [body]:
        lines.append(f"  │ {ln}")
    print("\n".join(lines), flush=True)
