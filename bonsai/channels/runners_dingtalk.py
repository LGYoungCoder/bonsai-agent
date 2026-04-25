"""DingTalk runner — Stream-mode subscription via dingtalk-stream SDK.

Requires the optional `dingtalk-stream` package:
    pip install dingtalk-stream

No webhook / public IP — uses DingTalk's long-connection Stream protocol.
Only @bot messages (IM callback) are handled; enterprise events ignored.

Config (config.toml):
    [channels.dingtalk]
    enabled = true
    client_id = "dingxxxxx"
    client_secret = "$ref:env:DINGTALK_CLIENT_SECRET"
    allowed_users = "staff1,staff2"   # staff_id / unionId; * = open
"""

from __future__ import annotations

import asyncio
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

_DT_SPLIT_LIMIT = 1800


def _try_import_dingtalk():
    try:
        import dingtalk_stream  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "dingtalk-stream not installed. Run `pip install dingtalk-stream` "
            "(or `pip install bonsai-agent[dingtalk]`)."
        ) from e
    return dingtalk_stream


def run_dingtalk(root: Path, cfg: Config, *,
                 allowed_users: set[str] | None = None) -> None:
    dt_cfg = (cfg.channels or {}).get("dingtalk") or {}
    client_id = str(dt_cfg.get("client_id") or "").strip()
    client_secret = str(dt_cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "channels.dingtalk.client_id / client_secret missing. "
            "Run `bonsai setup` or edit config.toml.")
    if allowed_users is None:
        raw = str(dt_cfg.get("allowed_users") or "").strip()
        if raw and raw != "*":
            allowed_users = {u.strip() for u in raw.split(",") if u.strip()}

    dingtalk_stream = _try_import_dingtalk()

    sys_prompt = (
        "你现在通过钉钉跟用户对话。\n"
        "- 用户看到的是钉钉消息文本;不要用复杂 Markdown;\n"
        "- 回复尽量简洁,必要时分段;\n"
        "- 用户可发 /help /new /stop /status 等命令。"
    )
    print(f"[dingtalk] building agent (client_id=***{client_id[-6:]})",
          flush=True)
    ctx = build_agent(root, cfg, system_prompt=sys_prompt)
    from ..runtime import register_hot_reloader, reload_agent_context
    register_hot_reloader(lambda new_cfg: reload_agent_context(ctx, new_cfg, root=root))
    sessions = PerUserSessions(ctx, root)
    install_shutdown_flush(sessions)
    install_maintenance(root, cfg)
    aio = asyncio.new_event_loop()

    ChatbotHandler = dingtalk_stream.ChatbotHandler

    class _Handler(ChatbotHandler):  # type: ignore[misc]
        def __init__(self):
            super().__init__()

        async def process(self, callback):
            try:
                msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                uid = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "")
                uid = str(uid or "")
                if not uid:
                    return dingtalk_stream.AckMessage.STATUS_OK, "empty uid"
                if allowed_users and uid not in allowed_users:
                    log.info("drop dingtalk msg from %s (not in allow list)", uid)
                    return dingtalk_stream.AckMessage.STATUS_OK, "unauthorized"
                text = (getattr(msg, "text", None)
                        and msg.text.content or "").strip()
                if not text:
                    self.reply_text("（暂不支持此消息类型，请发文字或 /help）", msg)
                    return dingtalk_stream.AckMessage.STATUS_OK, "no text"

                _print_inbound(uid, text)

                if text.startswith("/"):
                    reply = dispatch_command(
                        text, uid=uid, sessions=sessions, cfg=cfg)
                    if reply:
                        self._send_long(reply.text, msg)
                    return dingtalk_stream.AckMessage.STATUS_OK, "cmd"

                us = sessions.get_or_create(uid)
                if not us.preamble_used and us.profile:
                    pre = us.profile.preamble()
                    if pre:
                        text = pre + text
                    us.preamble_used = True

                try:
                    reply_raw, aborted = aio.run_until_complete(
                        drive_turn(us, text))
                except Exception:
                    log.exception("turn failed for dingtalk uid=%s", uid)
                    self.reply_text(
                        "抱歉，agent 遇到临时错误。换个说法再试，或发 /new 重开会话。",
                        msg)
                    return dingtalk_stream.AckMessage.STATUS_OK, "error"
                pretty = md_to_plain(reply_raw) or "(已完成)"
                if aborted:
                    pretty += "\n\n(用户要求中止，回合提前结束)"
                self._send_long(pretty, msg)
                return dingtalk_stream.AckMessage.STATUS_OK, "ok"
            except Exception:
                log.exception("dingtalk process failed")
                return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, "err"

        def _send_long(self, text: str, msg) -> None:
            for chunk in split_for_im(text, limit=_DT_SPLIT_LIMIT):
                try:
                    self.reply_text(chunk, msg)
                except Exception as e:
                    log.error("dingtalk reply_text failed: %s", e)
                    break

    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(client_id, client_secret)
    )
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, _Handler())
    print("[dingtalk] runner online, starting stream client", flush=True)
    try:
        client.start_forever()
    finally:
        aio.close()


def _print_inbound(uid: str, text: str) -> None:
    head = f"[dingtalk] uid={uid[:10]} @ {time.strftime('%H:%M:%S')}"
    body = text.strip() or "(无文字)"
    lines = [head]
    for ln in body.splitlines() or [body]:
        lines.append(f"  │ {ln}")
    print("\n".join(lines), flush=True)
