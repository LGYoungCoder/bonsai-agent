"""Channel runners — long-running processes that bridge a chat channel's
messages to bonsai's AgentLoop.

Only WeChat (iLink) is implemented for now. Feishu / WeCom / Telegram /
DingTalk reuse the shared chat_runtime helpers once they get their own
`get_updates → send_text` adapters.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from pathlib import Path

from ..config import Config
from ..runtime import build_agent
from .chat_runtime import (
    PerUidSerializer,
    PerUserSessions,
    dispatch_command,
    drive_turn,
    install_maintenance,
    install_shutdown_flush,
    md_to_plain,
    split_for_im,
)
from .wechat_client import WxBotClient, WxSessionExpired

_FILE_TAG_RE = re.compile(r"\[FILE:([^\]]+)\]")
_BAD_PATH_PLACEHOLDERS = {"filepath", "<filepath>", "path", "<path>",
                           "file_path", "<file_path>", "...", ""}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

log = logging.getLogger(__name__)


def _extract_files(reply: str, sent_dir: Path, inbound_paths: set[Path]) -> list[Path]:
    """Pull [FILE:path] markers; absolute used as-is, relative under sent_dir.
    Drop placeholders / files user just sent us (agent might echo)."""
    out: list[Path] = []
    for m in _FILE_TAG_RE.findall(reply):
        raw = m.strip()
        if raw.lower() in _BAD_PATH_PLACEHOLDERS:
            continue
        p = Path(raw) if os.path.isabs(raw) else (sent_dir / raw)
        if p in inbound_paths or not p.exists():
            continue
        out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p in seen: continue
        seen.add(p); uniq.append(p)
    return uniq


def _send_file_routed(bot: WxBotClient, uid: str, path: Path, context_token: str) -> None:
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        bot.send_image(uid, path, context_token=context_token)
    elif ext in _VIDEO_EXTS:
        bot.send_video(uid, path, context_token=context_token)
    else:
        bot.send_file(uid, path, context_token=context_token)


def run_wechat(root: Path, cfg: Config, *,
               allowed_users: set[str] | None = None,
               poll_timeout: float = 30.0) -> None:
    """Block and run the WeChat bot loop. Caller handles interrupts."""
    token_file = root / "data" / "wechat_token.json"
    bot = WxBotClient(token_file)
    if not bot.logged_in:
        raise RuntimeError(
            f"未登录。先在 web UI 扫码,或删掉 {token_file} 重新登录。")

    # User-identity-aware system prompt — agent knows it's on WeChat.
    sys_prompt = (
        "你现在通过微信(iLink 协议)跟用户对话。\n"
        "- 用户看到的是纯文本微信消息,不要用 markdown 语法(没有粗体/标题渲染);\n"
        "- 回复尽量简洁,必要时分段;\n"
        "- 要让用户收到文件/图片,在回复里加 [FILE:绝对路径],runner 会自动发送并从文本里剥掉标记;\n"
        "- 用户发的图片/文件路径会以 [用户发来文件: <path>] 附在消息后,你可以用 file_read/code_run 处理。\n"
        "- 用户可发 /help /new /stop /status 等命令。"
    )
    print(f"[wechat] building agent (token_file={token_file}, buf_len={len(bot.t.updates_buf)})",
          flush=True)
    ctx = build_agent(root, cfg, system_prompt=sys_prompt)
    sessions = PerUserSessions(ctx, root)
    install_shutdown_flush(sessions)
    install_maintenance(root, cfg)
    media_dir = root / "data" / "wechat_media"
    print(f"[wechat] runner online, bot_id={bot.t.ilink_bot_id}, entering poll loop",
          flush=True)

    # Non-blocking dispatch — poll loop never waits on a turn. Each uid
    # gets its own serial queue + worker thread so rapid-fire messages from
    # the same user don't get reordered, different users run in parallel.
    # This fixes "消息丢失" (iLink buffer overflow while the bot processes
    # a slow turn) and "没有回复" (one user's turn blocking the next).
    serializer = PerUidSerializer(
        handler_fn=lambda msg: _handle_msg(
            bot, msg, sessions, cfg, media_dir, allowed_users=allowed_users,
        ),
    )

    seen: set[int] = set()
    tick = 0
    try:
        while True:
            tick += 1
            t0 = time.time()
            try:
                msgs = bot.get_updates(poll_timeout)
            except WxSessionExpired as e:
                print(f"\n⚠️  {e}\n    去 web UI 的「渠道」页面点「扫码登录」重新获取 token。",
                      flush=True)
                return
            except Exception as e:
                print(f"[wechat] get_updates failed: {e!r} — retry in 5s", flush=True)
                time.sleep(5)
                continue
            dur = time.time() - t0
            print(f"[wechat] tick #{tick} got {len(msgs)} msg(s) in {dur:.1f}s "
                  f"· queued={sum(serializer.stats()['pending_by_uid'].values())}"
                  f" · active_uids={serializer.stats()['active_uids']}",
                  flush=True)
            for msg in msgs:
                mid = msg.get("message_id", 0)
                if mid in seen:
                    continue
                seen.add(mid)
                if len(seen) > 5000:
                    seen.intersection_update(set(list(seen)[-2000:]))
                uid = msg.get("from_user_id", "")
                if uid:
                    serializer.enqueue(uid, msg)
    finally:
        serializer.stop()


def _handle_msg(bot, msg, sessions, cfg, media_dir, *, allowed_users):
    """Process one wechat message. Runs in a PerUidSerializer worker thread,
    so same-uid messages are serialized but different uids run in parallel.
    Each stage logs a line so users can see where a message went missing.
    """
    mid = msg.get("message_id", 0)
    if not bot.is_user_msg(msg):
        return
    uid = msg.get("from_user_id", "")
    ctx_token = msg.get("context_token", "")
    text = bot.extract_text(msg).strip()
    inbound = bot.download_media(msg, media_dir)
    log.info("[wx] recv mid=%s uid=%s text_len=%d media=%d",
             mid, uid[:10], len(text), len(inbound))

    if allowed_users and uid not in allowed_users:
        log.info("[wx] drop uid=%s (not in allow list)", uid[:10])
        return

    # Non-text, non-media messages (语音 / 名片 / 位置 / 表情): don't silently
    # disappear — tell the user once so they don't think the bot's dead.
    if not text and not inbound:
        log.info("[wx] unsupported message from uid=%s (mtype=%s); replying politely",
                 uid[:10], msg.get("message_type"))
        try:
            bot.send_text(
                uid,
                "我现在只能处理文字和常见文件。语音/名片/位置消息暂时收不到,你打字告诉我就行。",
                context_token=ctx_token,
            )
        except Exception as e:
            log.error("[wx] polite reply failed: %s", e)
        return

    # Slash commands — handle without touching the LLM.
    if text.startswith("/"):
        reply = dispatch_command(text, uid=uid, sessions=sessions, cfg=cfg)
        if reply:
            try:
                bot.send_text(uid, reply.text, context_token=ctx_token)
            except Exception as e:
                log.error("send_text failed: %s", e)
            return

    # Compose prompt: text + inbound media paths so the agent can act on them.
    parts = []
    if text: parts.append(text)
    for p in inbound:
        parts.append(f"[用户发来文件: {p}]")
    prompt = "\n".join(parts) if parts else "(用户只发了附件)"
    # Console line — always flushed, untruncated. So watching the
    # runner log (tail / channel-log wechat / web log viewer) shows
    # exactly what came in. Full text also lands in this chat's
    # SessionLog via AgentLoop.add_user → Web 会话列表可回放。
    chat_id = sessions.active_chat(uid) if hasattr(sessions, "active_chat") else "default"
    _print_inbound(uid, chat_id, text, inbound)
    try:
        bot.send_typing(uid)
    except Exception:
        pass

    us = sessions.get_or_create(uid)
    # Profile preamble — injected once per session so the agent knows
    # who it's talking to. Not in frozen prefix → prompt cache stays stable.
    if not us.preamble_used and us.profile:
        pre = us.profile.preamble()
        if pre:
            prompt = pre + prompt
        us.preamble_used = True

    # Typing heartbeat — WeChat's typing indicator expires fast; keep it
    # alive every 5s so the user sees "agent is typing..." for long turns.
    typing_stop = threading.Event()
    def _typing_hb():
        while not typing_stop.is_set():
            try: bot.send_typing(uid)
            except Exception: pass
            if typing_stop.wait(5.0): break
    hb = threading.Thread(target=_typing_hb, daemon=True)
    hb.start()
    # Each worker runs its own asyncio loop (drive_turn is async). Using
    # asyncio.run means thread-safe event-loop isolation across uid workers.
    t_start = time.time()
    try:
        reply_raw, aborted = asyncio.run(drive_turn(us, prompt))
    except Exception as e:
        log.exception("[wx] turn failed uid=%s: %s", uid[:10], e)
        _safe_send(bot, uid, "抱歉,agent 遇到了临时错误。换个说法再试一次,或发 /new 重开会话。",
                   ctx_token)
        return
    finally:
        typing_stop.set()
        hb.join(timeout=1.0)
    log.info("[wx] turn done uid=%s in %.1fs reply_len=%d aborted=%s",
             uid[:10], time.time() - t_start, len(reply_raw or ""), aborted)

    # Outbound: strip [FILE:] markers for text path, convert markdown, split.
    files_to_send = _extract_files(reply_raw, media_dir, set(inbound))
    reply_for_text = _FILE_TAG_RE.sub("", reply_raw).strip()
    pretty = md_to_plain(reply_for_text) or "(已完成)"
    if aborted:
        pretty += "\n\n(用户要求中止,回合提前结束)"
    chunks = list(split_for_im(pretty))
    sent_ok = 0
    for chunk in chunks:
        if _safe_send(bot, uid, chunk, ctx_token):
            sent_ok += 1
        time.sleep(0.3)
    log.info("[wx] sent uid=%s chunks=%d/%d files=%d",
             uid[:10], sent_ok, len(chunks), len(files_to_send))
    for p in files_to_send:
        try:
            _send_file_routed(bot, uid, p, ctx_token)
            log.info("[wx] sent media uid=%s file=%s", uid[:10], p.name)
            time.sleep(0.3)
        except Exception as e:
            log.error("[wx] send_file uid=%s file=%s failed: %s", uid[:10], p, e)


def _safe_send(bot, uid, text, context_token) -> bool:
    """Send text with retry (send_text itself retries + raises on hard fail).
    Returns True on success, False on failure (already logged)."""
    try:
        bot.send_text(uid, text, context_token=context_token)
        return True
    except Exception as e:
        log.error("[wx] send_text failed uid=%s: %s", uid[:10], e)
        return False


def _print_inbound(uid: str, chat_id: str, text: str, files: list[Path]) -> None:
    """Write a multi-line, untruncated dump of the inbound message to
    stdout. Uses print() directly so the line shows even when log level
    is raised, and prefixes the uid + chat so multi-user logs are
    scannable."""
    import sys
    head = f"[wechat] uid={uid[:10]} chat={chat_id} @ {time.strftime('%H:%M:%S')}"
    if files:
        head += f" (+{len(files)} files)"
    body = (text or "(无文字)").strip()
    lines = [head]
    for ln in body.splitlines() or [body]:
        lines.append(f"  │ {ln}")
    for p in files:
        lines.append(f"  📎 {p}")
    print("\n".join(lines), flush=True)
