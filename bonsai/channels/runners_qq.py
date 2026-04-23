"""QQ runner — qq-botpy long-connection bot bridge to AgentLoop.

Requires the optional `qq-botpy` package. Install via:
    pip install bonsai-agent[qq]
or:
    pip install qq-botpy

Talks to Tencent's official QQ bot open platform. Supports C2C (单聊) and
group-at (群里被 @) messages. Passive replies use the inbound msg_id so the
bot doesn't need to be a pay-to-send active-message bot.

Config shape (config.toml):
    [channels.qq]
    enabled = true
    app_id = "102..."
    app_secret = "$ref:env:QQ_BOT_SECRET"
    allowed_users = "openid1,openid2"   # * or empty = open
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
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

_QQ_SPLIT_LIMIT = 1500  # QQ content limit per message; keep slack

_PROCESSED_IDS: deque = deque(maxlen=2000)
_MSG_SEQ_LOCK = threading.Lock()
_MSG_SEQ = 1


def _next_msg_seq() -> int:
    global _MSG_SEQ
    with _MSG_SEQ_LOCK:
        _MSG_SEQ += 1
        return _MSG_SEQ


def _try_import_botpy():
    try:
        import botpy  # type: ignore
        from botpy.message import C2CMessage, GroupMessage  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "qq-botpy not installed. Run `pip install qq-botpy` "
            "(or `pip install bonsai-agent[qq]`)."
        ) from e
    return botpy, C2CMessage, GroupMessage


def run_qq(root: Path, cfg: Config, *,
           allowed_users: set[str] | None = None) -> None:
    """Block and run the QQ bot. Reconnects on transient failure."""
    qq_cfg = (cfg.channels or {}).get("qq") or {}
    app_id = str(qq_cfg.get("app_id") or "").strip()
    app_secret = str(qq_cfg.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError(
            "channels.qq.app_id / app_secret missing. Run `bonsai setup` "
            "or edit config.toml."
        )
    if allowed_users is None:
        raw = str(qq_cfg.get("allowed_users") or "").strip()
        if raw and raw != "*":
            allowed_users = {u.strip() for u in raw.split(",") if u.strip()}

    botpy, C2CMessage, GroupMessage = _try_import_botpy()

    sys_prompt = (
        "你现在通过 QQ 跟用户对话。\n"
        "- 用户看到的是 QQ 文本消息,没有 Markdown 渲染;不要用复杂格式;\n"
        "- 每条单独消息上限 ~1500 字符,长回复会被自动切分;\n"
        "- 用户可发 /help /new /stop /status 等命令。"
    )
    print(f"[qq] building agent (app_id=***{app_id[-6:]})", flush=True)
    ctx = build_agent(root, cfg, system_prompt=sys_prompt)
    sessions = PerUserSessions(ctx, root)
    install_shutdown_flush(sessions)
    install_maintenance(root, cfg)

    app = _QQApp(sessions, cfg, allowed_users=allowed_users,
                 C2CMessage=C2CMessage, GroupMessage=GroupMessage)
    print("[qq] runner online, starting bot client", flush=True)
    asyncio.run(app.run_forever(botpy, app_id, app_secret))


class _QQApp:
    """Holds the botpy client and per-user session dispatch."""

    def __init__(self, sessions: PerUserSessions, cfg: Config, *,
                 allowed_users: set[str] | None,
                 C2CMessage, GroupMessage) -> None:
        self.sessions = sessions
        self.cfg = cfg
        self.allowed = allowed_users
        self._C2C = C2CMessage
        self._Group = GroupMessage
        self.client = None

    def _build_intents(self, botpy):
        # Try fully-loaded ctor first; fall back to attribute toggling for
        # older qq-botpy versions.
        try:
            return botpy.Intents(public_messages=True, direct_message=True)
        except Exception:
            intents = (botpy.Intents.none() if hasattr(botpy.Intents, "none")
                       else botpy.Intents())
            for attr in (
                "public_messages", "public_guild_messages",
                "direct_message", "direct_messages",
                "c2c_message", "c2c_messages",
                "group_at_message", "group_at_messages",
            ):
                if hasattr(intents, attr):
                    try:
                        setattr(intents, attr, True)
                    except Exception:
                        pass
            return intents

    def _make_client_class(self, botpy):
        outer = self

        class QQBot(botpy.Client):  # type: ignore[misc]
            def __init__(self):
                super().__init__(intents=outer._build_intents(botpy),
                                  ext_handlers=False)

            async def on_ready(self):  # noqa: D401
                name = getattr(getattr(self, "robot", None), "name", "QQBot")
                print(f"[qq] bot ready: {name}", flush=True)

            async def on_c2c_message_create(self, message):
                await outer._on_message(message, is_group=False)

            async def on_group_at_message_create(self, message):
                await outer._on_message(message, is_group=True)

            async def on_direct_message_create(self, message):
                await outer._on_message(message, is_group=False)

        return QQBot

    async def run_forever(self, botpy, app_id: str, app_secret: str) -> None:
        cls = self._make_client_class(botpy)
        self.client = cls()
        while True:
            try:
                print(f"[qq] bot starting... {time.strftime('%m-%d %H:%M')}",
                      flush=True)
                await self.client.start(appid=app_id, secret=app_secret)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[qq] bot error: {e!r}", flush=True)
            print("[qq] reconnect in 5s...", flush=True)
            await asyncio.sleep(5)

    async def _on_message(self, data, *, is_group: bool) -> None:
        try:
            msg_id = getattr(data, "id", None)
            if msg_id in _PROCESSED_IDS:
                return
            if msg_id:
                _PROCESSED_IDS.append(msg_id)
            content = (getattr(data, "content", "") or "").strip()
            if not content:
                return
            author = getattr(data, "author", None)
            uid = str(
                getattr(author, "member_openid" if is_group else "user_openid", "")
                or getattr(author, "id", "")
                or "unknown"
            )
            chat_id = (str(getattr(data, "group_openid", "") or uid)
                       if is_group else uid)

            if self.allowed and uid not in self.allowed:
                log.info("drop qq msg from %s (not in allow list)", uid)
                return

            _print_inbound(uid, chat_id, content, is_group=is_group)

            if content.startswith("/"):
                reply = dispatch_command(content, uid=uid,
                                         sessions=self.sessions, cfg=self.cfg)
                if reply:
                    await self._send(chat_id, reply.text, msg_id=msg_id,
                                      is_group=is_group)
                return

            # Fire-and-forget: long turns shouldn't block the botpy
            # event loop from receiving other messages.
            asyncio.create_task(
                self._run_agent_turn(uid, chat_id, content,
                                      msg_id=msg_id, is_group=is_group)
            )
        except Exception:
            log.exception("qq on_message failed")

    async def _run_agent_turn(self, uid: str, chat_id: str, text: str, *,
                              msg_id: str | None, is_group: bool) -> None:
        us = self.sessions.get_or_create(uid)
        if not us.preamble_used and us.profile:
            pre = us.profile.preamble()
            if pre:
                text = pre + text
            us.preamble_used = True
        try:
            reply_raw, aborted = await drive_turn(us, text)
        except Exception:
            log.exception("turn failed for qq uid=%s", uid)
            await self._send(chat_id, "抱歉，agent 遇到临时错误。换个说法再试，或发 /new 重开会话。",
                              msg_id=msg_id, is_group=is_group)
            return
        pretty = md_to_plain(reply_raw) or "(已完成)"
        if aborted:
            pretty += "\n\n(用户要求中止，回合提前结束)"
        await self._send(chat_id, pretty, msg_id=msg_id, is_group=is_group)

    async def _send(self, chat_id: str, content: str, *,
                    msg_id: str | None, is_group: bool) -> None:
        if self.client is None:
            return
        api = (self.client.api.post_group_message if is_group
               else self.client.api.post_c2c_message)
        key = "group_openid" if is_group else "openid"
        for part in split_for_im(content, limit=_QQ_SPLIT_LIMIT):
            try:
                await api(**{
                    key: chat_id, "msg_type": 0,
                    "content": part, "msg_id": msg_id,
                    "msg_seq": _next_msg_seq(),
                })
            except Exception as e:
                log.error("qq send failed (is_group=%s): %s", is_group, e)
                break


def _print_inbound(uid: str, chat_id: str, text: str, *, is_group: bool) -> None:
    kind = "group" if is_group else "c2c"
    head = f"[qq/{kind}] uid={uid[:10]} chat={chat_id[:10]} @ {time.strftime('%H:%M:%S')}"
    body = text.strip() or "(无文字)"
    lines = [head]
    for ln in body.splitlines() or [body]:
        lines.append(f"  │ {ln}")
    print("\n".join(lines), flush=True)
