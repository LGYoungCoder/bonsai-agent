"""Shared chat-runtime utilities used by IM channel runners.

- PerUserSessions: keeps one AgentLoop per chat user, so messages stay in
  the same conversation tail; /new clears a user's session
- dispatch_command: parses and handles `/new`, `/stop`, `/help`, `/status`,
  `/llm` before the text ever reaches AgentLoop
- md_to_plain: strips markdown markers so IM clients don't show literal `**`
- split_for_im: chunk response for IM char caps while respecting code blocks

Designed so a Feishu / Telegram / WeCom runner can reuse everything below
the `send_text` / `send_typing` abstraction.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from ..core.loop import AgentLoop
from ..core.session_log import SessionLog, load_messages
from ..core.types import FrozenPrefix
from ..runtime import AgentContext, build_agent, render_wakeup_prefix
from ..writer.session_archiver import schedule_ingest
from .chat_profiles import UserProfile, load_profile, save_profile

# 单通道 tail 续聊窗口 — 超过这个时间视为新话题,不再接旧对话。
RESUME_WINDOW_DAYS = 7
# 长对话每 N 轮后台归档一次(幂等,bug/崩溃场景的保险)
PERIODIC_ARCHIVE_EVERY_N_TURNS = 10

log = logging.getLogger(__name__)


class PerUidSerializer:
    """Per-uid worker threads so one slow turn doesn't block the poll loop.

    Without this, a rapid-fire user ends up losing messages — the provider's
    inbound buffer (iLink, etc.) fills while the bot is mid-turn. With it,
    the poll loop dispatches in O(1) and keeps long-polling; each uid gets
    its own serial queue so messages from the same user stay ordered.

    Idle workers exit after `idle_timeout` seconds so the thread count stays
    bounded — active users each hold one thread, quiet users hold none.
    """

    def __init__(self, handler_fn: Callable[[dict], None],
                 *, idle_timeout: float = 300.0) -> None:
        import queue
        import threading
        self._handler_fn = handler_fn
        self._idle_timeout = idle_timeout
        self._queues: dict[str, queue.Queue] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._stopped = False

    def enqueue(self, uid: str, msg: dict) -> None:
        if self._stopped or not uid:
            return
        import queue
        import threading
        with self._lock:
            q = self._queues.get(uid)
            if q is None:
                q = queue.Queue()
                self._queues[uid] = q
                t = threading.Thread(target=self._worker, args=(uid, q),
                                      name=f"wx-worker-{uid[:8]}", daemon=True)
                self._threads[uid] = t
                t.start()
            q.put(msg)

    def stop(self) -> None:
        """Signal all workers to drain + exit. Best-effort: daemon threads
        die with the process anyway."""
        self._stopped = True

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_uids": len(self._threads),
                "pending_by_uid": {uid: q.qsize() for uid, q in self._queues.items()},
            }

    def _worker(self, uid: str, q) -> None:
        import queue as _q
        while not self._stopped:
            try:
                msg = q.get(timeout=self._idle_timeout)
            except _q.Empty:
                with self._lock:
                    # Double-check: racy enqueue between Empty and lock
                    if q.empty():
                        self._queues.pop(uid, None)
                        self._threads.pop(uid, None)
                        return
                    continue
            try:
                self._handler_fn(msg)
            except Exception:
                log.exception("PerUidSerializer: handler crashed (uid=%s)", uid[:8])


def install_maintenance(root: Path, cfg) -> None:
    """Start the maintenance daemon for this runner. Idempotent across calls
    + across runners under the same root."""
    from ..maintenance import start_maintenance
    try:
        start_maintenance(root, cfg)
    except Exception as e:
        log.warning("could not start maintenance: %s", e)


def install_shutdown_flush(sessions: PerUserSessions) -> None:
    """Hook SIGTERM/SIGINT/normal-exit so live sessions get archived before
    the runner process dies. Idempotent: re-registering is a no-op.
    """
    import atexit
    import signal
    if getattr(sessions, "_shutdown_hook_installed", False):
        return
    sessions._shutdown_hook_installed = True  # type: ignore[attr-defined]

    def _flush() -> None:
        try:
            sessions.flush_all(reason="shutdown")
        except Exception as e:
            log.warning("shutdown flush failed: %s", e)

    atexit.register(_flush)
    # SIGTERM (systemd stop) + SIGINT (Ctrl+C) both raise → atexit fires,
    # but we re-install to log the signal arrival for ops visibility.
    def _sig(signum, _frame):
        log.info("runner received signal %d, flushing live sessions", signum)
        _flush()
        # Restore default handler and re-raise so process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)
    try:
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
    except (ValueError, OSError):
        # Not in main thread → can't install; rely on atexit alone.
        pass


def _safe_segment(s: str, max_len: int = 16) -> str:
    """Filename-safe slug: keep word chars and dash, strip everything else."""
    cleaned = re.sub(r"[^\w\-]+", "-", s, flags=re.UNICODE).strip("-")
    return (cleaned[:max_len] or "x")


def _make_session_log(root: Path, source: str, uid: str, chat_id: str) -> SessionLog:
    uid_slug = _safe_segment(uid, max_len=12)
    chat_slug = _safe_segment(chat_id, max_len=16)
    ts = int(time.time())
    fname = f"{source}-{uid_slug}-{chat_slug}-{ts}.jsonl"
    return SessionLog(
        root / "logs" / "sessions" / fname,
        session_id=f"{source}:{uid}:{chat_id}",
    )


# ─────────────── Per-user session cache ───────────────

@dataclass
class UserSession:
    loop: AgentLoop
    uid: str = ""
    chat_id: str = "default"
    profile: UserProfile | None = None
    preamble_used: bool = False
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    turns: int = 0
    aborted: bool = False
    last_user_preview: str = ""
    session_log_path: Path | None = None  # so _archive can reach it
    _last_archive_turn: int = 0
    parent_sessions: PerUserSessions | None = None  # backref for note_turn_complete


class PerUserSessions:
    """Caches AgentLoop per (uid, chat_id) so each user can keep multiple
    named conversations. `active_chat[uid]` tracks which one is current.
    `default` is the chat id when the user never used /chat commands.

    Reuse the same `ctx` for all entries so providers / stores / handlers are
    shared — only the loop's tail differs. Profile is per-uid, shared across
    their chats."""

    def __init__(self, ctx: AgentContext, root: Path, *, idle_timeout: float = 3600.0,
                 source: str = "wechat"):
        self.ctx = ctx
        self.root = root
        self.idle_timeout = idle_timeout
        self.source = source  # channel kind, used as filename prefix
        self._sessions: dict[tuple[str, str], UserSession] = {}
        self._active: dict[str, str] = {}

    def active_chat(self, uid: str) -> str:
        return self._active.get(uid, "default")

    def _new_loop(self, uid: str = "", chat_id: str = "",
                  sess_log: SessionLog | None = None) -> AgentLoop:
        # Each (uid, chat_id) gets its own SessionLog so Web UI can list
        # the conversations separately. Falls back to the shared ctx log
        # when called without identity (legacy callers / non-channel use).
        if sess_log is None:
            if uid and chat_id:
                sess_log = _make_session_log(self.root, self.source, uid, chat_id)
            else:
                sess_log = self.ctx.session_log
        return AgentLoop(
            self.ctx.chain, self.ctx.prefix, self.ctx.handler,
            policy=self.ctx.policy, max_turns=self.ctx.max_turns,
            session_log=sess_log,
        )

    def _latest_log_for(self, uid: str, chat_id: str) -> Path | None:
        """Most recent session log file for this (source, uid, chat_id), if any."""
        uid_slug = _safe_segment(uid, max_len=12)
        chat_slug = _safe_segment(chat_id, max_len=16)
        pattern = f"{self.source}-{uid_slug}-{chat_slug}-*.jsonl"
        d = self.root / "logs" / "sessions"
        if not d.exists():
            return None
        candidates = sorted(d.glob(pattern),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    def _try_resume_then_attach(self, uid: str, chat_id: str) -> UserSession | None:
        us = self._try_resume(uid, chat_id)
        if us is not None:
            us.parent_sessions = self
        return us

    def _try_resume(self, uid: str, chat_id: str) -> UserSession | None:
        """If there's a recent-enough session log, rebuild the tail from it
        so a returning user doesn't start from a blank loop.
        """
        latest = self._latest_log_for(uid, chat_id)
        if latest is None:
            return None
        age_days = (time.time() - latest.stat().st_mtime) / 86400
        if age_days > RESUME_WINDOW_DAYS:
            return None
        msgs = load_messages(latest)
        if not msgs:
            return None
        # Reuse the existing file so the continued thread stays one logical
        # session on disk (rather than forking every time the user returns).
        sess_log = SessionLog(latest, session_id=latest.stem)
        loop = self._new_loop(uid, chat_id, sess_log=sess_log)
        loop.tail.messages = msgs
        # Compress the tail eagerly if it exceeds the soft budget — loop.run()
        # would do the same on first turn anyway; doing it now keeps the first
        # response faster and avoids one logged compression warning.
        from ..core.loop import _compress_tail, _estimate_total
        total = _estimate_total(msgs, self.ctx.prefix)
        if total > self.ctx.policy.soft:
            loop.tail.messages, _ = _compress_tail(
                msgs, self.ctx.prefix, self.ctx.policy, start_total=total,
            )
        profile = load_profile(self.root, uid)
        us = UserSession(loop=loop, uid=uid, profile=profile, chat_id=chat_id,
                         session_log_path=latest,
                         preamble_used=True)  # preamble already in the old tail
        us.turns = sum(1 for m in msgs if m.role == "user")
        us._last_archive_turn = us.turns  # don't re-archive what's already there
        log.info("resumed %s:%s from %s (%d msg, %.1fd old)",
                 uid[:8], chat_id, latest.name, len(msgs), age_days)
        return us

    def _refresh_prefix(self, us: UserSession) -> None:
        """Rebuild wakeup each turn so the bot picks up new SOPs / L0 / memory
        without restart. Byte-stable when nothing changed → cache stays hit.
        """
        ctx = self.ctx
        ss = getattr(ctx.handler, "skill_store", None)
        ms = getattr(ctx.handler, "memory_store", None)
        if ss is None:
            return
        fresh_sys = render_wakeup_prefix(ctx.base_system_prompt, ss, ms, cwd=self.root)
        if fresh_sys != us.loop.prefix.system_prompt:
            us.loop.prefix = FrozenPrefix(system_prompt=fresh_sys,
                                           tools=us.loop.prefix.tools,
                                           l1_index=us.loop.prefix.l1_index)

    def _archive(self, us: UserSession, *, reason: str = "periodic") -> None:
        """Fire-and-forget background ingest into MemoryStore. Idempotent."""
        if us.session_log_path is None or not us.session_log_path.exists():
            return
        cfg = self.ctx.cfg
        if cfg is None:
            return  # misconfigured context; skip rather than crash
        try:
            db_path = (self.root / cfg.memory.memory_db.lstrip("./")).resolve()
            schedule_ingest(
                us.session_log_path,
                db_path=db_path,
                embed_provider=cfg.memory.embed_provider,
                embed_api_key=getattr(cfg.memory, "embed_api_key", ""),
                embed_base_url=getattr(cfg.memory, "embed_base_url", ""),
                embed_model=cfg.memory.embed_model,
                wing=self.source,
                room=us.loop.session_log.session_id if us.loop.session_log else us.chat_id,
            )
            us._last_archive_turn = us.turns
            log.info("archive fired (%s) %s:%s %d turns", reason, us.uid[:8],
                     us.chat_id, us.turns)
        except Exception as e:
            log.warning("archive failed: %s", e)

    def note_turn_complete(self, us: UserSession) -> None:
        """Runner calls this after each assistant reply lands. Drives the
        'every N turns' periodic archival trigger (D) from the design doc.
        """
        us.turns += 1
        if us.turns - us._last_archive_turn >= PERIODIC_ARCHIVE_EVERY_N_TURNS:
            self._archive(us, reason=f"every-{PERIODIC_ARCHIVE_EVERY_N_TURNS}-turns")

    def flush_all(self, *, reason: str = "shutdown") -> None:
        """SIGTERM hook — archive every live session before the runner dies."""
        for us in list(self._sessions.values()):
            self._archive(us, reason=reason)

    def get_or_create(self, uid: str, chat_id: str | None = None) -> UserSession:
        self._gc()
        cid = chat_id or self.active_chat(uid)
        key = (uid, cid)
        us = self._sessions.get(key)
        if us is None:
            us = self._try_resume(uid, cid)
            if us is None:
                profile = load_profile(self.root, uid)
                loop = self._new_loop(uid, cid)
                us = UserSession(loop=loop, uid=uid, profile=profile,
                                 chat_id=cid,
                                 session_log_path=loop.session_log.path
                                 if loop.session_log else None)
            us.parent_sessions = self
            self._sessions[key] = us
        self._refresh_prefix(us)
        us.last_seen = time.time()
        self._active[uid] = cid
        return us

    def list_chats(self, uid: str) -> list[UserSession]:
        return sorted(
            [s for (u, _), s in self._sessions.items() if u == uid],
            key=lambda s: s.last_seen, reverse=True,
        )

    def switch(self, uid: str, chat_id: str) -> UserSession | None:
        """Switch active chat; returns the session or None if not found."""
        if (uid, chat_id) not in self._sessions:
            return None
        self._active[uid] = chat_id
        us = self._sessions[(uid, chat_id)]
        us.last_seen = time.time()
        return us

    def create_chat(self, uid: str, chat_id: str) -> UserSession:
        """Create a new chat and make it active. Overwrites any existing by same id."""
        profile = load_profile(self.root, uid)
        us = UserSession(loop=self._new_loop(uid, chat_id),
                         uid=uid, profile=profile, chat_id=chat_id)
        self._sessions[(uid, chat_id)] = us
        self._active[uid] = chat_id
        return us

    def remove_chat(self, uid: str, chat_id: str) -> bool:
        if (uid, chat_id) not in self._sessions:
            return False
        self._sessions.pop((uid, chat_id))
        if self._active.get(uid) == chat_id:
            # Fall back to most recently used, or 'default'
            remaining = self.list_chats(uid)
            self._active[uid] = remaining[0].chat_id if remaining else "default"
        return True

    def reset(self, uid: str) -> None:
        """/new — archive (A trigger) then clear the *active* chat only."""
        cid = self.active_chat(uid)
        us = self._sessions.get((uid, cid))
        if us is not None:
            self._archive(us, reason="/new")
        self._sessions.pop((uid, cid), None)

    def abort(self, uid: str) -> None:
        us = self._sessions.get((uid, self.active_chat(uid)))
        if us:
            us.aborted = True

    def reload_profile(self, uid: str) -> UserProfile:
        """Called after /name or /note changes disk; updates all of this uid's chats."""
        profile = load_profile(self.root, uid)
        for (u, _), us in self._sessions.items():
            if u == uid:
                us.profile = profile
        return profile

    def _gc(self) -> None:
        now = time.time()
        dead = [key for key, s in self._sessions.items()
                if now - s.last_seen > self.idle_timeout]
        for key in dead:
            us = self._sessions.get(key)
            if us is not None:
                self._archive(us, reason="idle-gc")
            self._sessions.pop(key, None)
        if dead:
            log.info("gc'd %d idle chat sessions", len(dead))


# ─────────────── Slash commands ───────────────

_CMD_RE = re.compile(r"^\s*(/[a-zA-Z_]+)(?:\s+(.*))?$")


@dataclass
class CommandReply:
    text: str
    handled: bool = True


def dispatch_command(text: str, *, uid: str, sessions: PerUserSessions,
                     cfg=None) -> CommandReply | None:
    """If `text` is a `/command`, handle it and return a reply. Otherwise None."""
    m = _CMD_RE.match(text)
    if not m:
        return None
    cmd, args = m.group(1), (m.group(2) or "").strip()
    cmd = cmd.lower()
    if cmd == "/help":
        return CommandReply(text=(
            "命令:\n"
            "/new          清空当前对话(等价于 /chat new)\n"
            "/stop         中止正在跑的长任务\n"
            "/status       当前会话 / 档案信息\n"
            "/llm          列出可用 provider\n"
            "/name <昵称>  设定我的昵称\n"
            "/note <内容>  追加一条关于你的备注\n"
            "/note list    列出备注  · /note clear 清空\n"
            "/chat list    列出我的多个对话\n"
            "/chat new [名] 新开一个对话并切过去\n"
            "/chat switch <名|序号> 切到另一个对话\n"
            "/chat rm <名|序号>     删掉某个对话\n"
            "/help         本帮助"
        ))
    if cmd == "/new":
        sessions.reset(uid)
        return CommandReply(text="✓ 已重置会话")
    if cmd == "/stop":
        sessions.abort(uid)
        return CommandReply(text="✓ 中止信号已发(下一回合结束后生效)")
    if cmd == "/status":
        us = sessions._sessions.get(uid)
        prof = (us.profile if us else load_profile(sessions.root, uid))
        lines = []
        if us:
            lines.append(
                f"会话自 {time.strftime('%H:%M', time.localtime(us.first_seen))} 开始 · "
                f"{us.turns} 轮 · 最后活跃 {time.strftime('%H:%M:%S', time.localtime(us.last_seen))}"
            )
        else:
            lines.append("(还没开始聊)")
        lines.append(f"昵称: {prof.nickname or '(未设,发 /name X 告诉我)'}")
        lines.append(f"备注 {len(prof.notes)} 条" + (":" if prof.notes else ""))
        for n in prof.notes[:5]:
            lines.append(f"  - {n}")
        if len(prof.notes) > 5:
            lines.append(f"  …还有 {len(prof.notes) - 5} 条")
        return CommandReply(text="\n".join(lines))
    if cmd == "/llm":
        if cfg is None:
            return CommandReply(text="(无 cfg 上下文)")
        providers = cfg.failover_providers() if hasattr(cfg, "failover_providers") else []
        lines = [f"  {'→' if i == 0 else '  '} [{i}] {p['name']} ({p.get('model','?')})"
                 for i, p in enumerate(providers)]
        return CommandReply(text="可用 provider:\n" + "\n".join(lines))
    if cmd == "/name":
        new = args.strip()[:40]
        if not new:
            return CommandReply(text="用法: /name 张三(清空:/name -)")
        prof = load_profile(sessions.root, uid)
        prof.nickname = "" if new == "-" else new
        save_profile(sessions.root, prof)
        sessions.reload_profile(uid)
        return CommandReply(text=f"✓ 昵称已设为 {prof.nickname or '(空)'}")
    if cmd == "/chat":
        return _dispatch_chat(uid=uid, args=args, sessions=sessions)
    if cmd == "/note":
        sub = args.split(maxsplit=1)
        prof = load_profile(sessions.root, uid)
        if not args or (sub and sub[0].lower() == "list"):
            if not prof.notes:
                return CommandReply(text="(还没有备注,发 /note <内容> 加一条)")
            return CommandReply(
                text="当前备注:\n" + "\n".join(f"  {i+1}. {n}"
                                              for i, n in enumerate(prof.notes))
            )
        if sub[0].lower() == "clear":
            prof.notes = []
            save_profile(sessions.root, prof)
            sessions.reload_profile(uid)
            return CommandReply(text="✓ 已清空")
        # Append one note, cap body at 200 chars, max 20 notes.
        text_note = args.strip()[:200]
        prof.notes.append(text_note)
        prof.notes = prof.notes[-20:]
        save_profile(sessions.root, prof)
        sessions.reload_profile(uid)
        return CommandReply(text=f"✓ 已加一条备注(共 {len(prof.notes)} 条)")
    return CommandReply(text=f"未知命令: {cmd}(发 /help 看支持的命令)")


def _slug_chat_id(raw: str) -> str:
    """Chat id: keep unicode word chars (CJK ok), drop path/fs-unsafe stuff, ≤ 24 chars."""
    cleaned = re.sub(r"[^\w\-]+", "-", raw.strip(), flags=re.UNICODE)[:24].strip("-")
    return cleaned or f"chat-{int(time.time()) % 100000}"


def _resolve_chat_ref(ref: str, chats: list[UserSession]) -> UserSession | None:
    """Match by 1-based index or exact/prefix chat_id."""
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(chats):
            return chats[idx]
        return None
    for c in chats:
        if c.chat_id == ref:
            return c
    for c in chats:
        if c.chat_id.startswith(ref):
            return c
    return None


def _dispatch_chat(*, uid: str, args: str, sessions: PerUserSessions) -> CommandReply:
    sub = args.split(maxsplit=1)
    op = (sub[0].lower() if sub else "list")
    rest = sub[1] if len(sub) > 1 else ""
    chats = sessions.list_chats(uid)
    active = sessions.active_chat(uid)

    if op == "list":
        if not chats:
            return CommandReply(text="(还没有对话,任何消息都会开启一个 default 对话)")
        lines = [f"{i+1}. {'→' if c.chat_id == active else ' '} {c.chat_id}"
                 f"  · {c.turns} 轮"
                 + (f" · {c.last_user_preview[:20]}" if c.last_user_preview else "")
                 for i, c in enumerate(chats)]
        return CommandReply(text="我的对话:\n" + "\n".join(lines) + "\n(→ 是当前)")

    if op == "new":
        cid = _slug_chat_id(rest) if rest else _slug_chat_id(time.strftime("chat-%m%d-%H%M"))
        sessions.create_chat(uid, cid)
        return CommandReply(text=f"✓ 新开对话 `{cid}` 并切过去了")

    if op == "switch":
        if not rest:
            return CommandReply(text="用法: /chat switch <名字或序号>")
        target = _resolve_chat_ref(rest, chats)
        if target is None:
            return CommandReply(text=f"找不到对话: {rest}。先 /chat list 看清楚")
        sessions.switch(uid, target.chat_id)
        return CommandReply(text=f"✓ 切到 `{target.chat_id}` ({target.turns} 轮)")

    if op == "rm":
        if not rest:
            return CommandReply(text="用法: /chat rm <名字或序号>")
        target = _resolve_chat_ref(rest, chats)
        if target is None:
            return CommandReply(text=f"找不到对话: {rest}")
        sessions.remove_chat(uid, target.chat_id)
        new_active = sessions.active_chat(uid)
        return CommandReply(text=f"✓ 删了 `{target.chat_id}`,当前对话 `{new_active}`")

    return CommandReply(text=f"未知 /chat 子命令: {op}(支持 list/new/switch/rm)")


# ─────────────── Markdown → plain text for IM ───────────────

_CODE_FENCE = re.compile(r"```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_HEADER = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HR = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_LIST = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_ORDLIST = re.compile(r"^(\s*)\d+\.\s+", re.MULTILINE)
_QUOTE = re.compile(r"^>\s?", re.MULTILINE)
_THINK_TAG = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)
_TOOLUSE_TAG = re.compile(r"<tool_use>.*?</tool_use>", re.DOTALL)


def md_to_plain(text: str, *, code_max_lines: int = 30) -> str:
    """Make markdown readable on plain-text IM clients. Keeps code *content*
    (but drops the fences), trims headers/bold/italic markers, shortens links."""
    text = _THINK_TAG.sub("", text)
    text = _TOOLUSE_TAG.sub("", text)

    def _code_replace(m):
        body = m.group(2).rstrip()
        lines = body.split("\n")
        if len(lines) > code_max_lines:
            body = "\n".join(lines[:code_max_lines]) + f"\n... ({len(lines) - code_max_lines} 行省略)"
        return "\n" + body + "\n"
    text = _CODE_FENCE.sub(_code_replace, text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADER.sub("", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _HR.sub("", text)
    text = _LIST.sub(r"\1• ", text)
    text = _ORDLIST.sub(r"\1", text)
    text = _QUOTE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or "..."


# ─────────────── Code-block-aware splitter ───────────────

def split_for_im(text: str, *, limit: int = 1800) -> list[str]:
    """Split a long reply into chunks ≤ limit chars. Tries to:
    1. Split at blank lines (paragraph breaks)
    2. If a code block would exceed limit, keep it on its own chunk (even if
       that means the chunk is bigger than limit — readability wins)
    """
    if len(text) <= limit:
        return [text]
    # Split into blocks: code block or plain paragraph, preserving order.
    # A code block starts with ``` on its own line and ends with ``` on a line.
    parts: list[tuple[str, str]] = []    # [(kind, content)]
    i = 0
    lines = text.split("\n")
    buf: list[str] = []
    in_code = False
    code_buf: list[str] = []
    for line in lines:
        if line.strip().startswith("```") and not in_code:
            if buf:
                parts.append(("p", "\n".join(buf).rstrip()))
                buf = []
            in_code = True
            code_buf = [line]
            continue
        if in_code and line.strip().startswith("```"):
            code_buf.append(line)
            parts.append(("c", "\n".join(code_buf)))
            code_buf = []
            in_code = False
            continue
        if in_code:
            code_buf.append(line)
        else:
            buf.append(line)
    if buf:
        parts.append(("p", "\n".join(buf).rstrip()))
    if code_buf:  # unterminated fence
        parts.append(("c", "\n".join(code_buf)))

    chunks: list[str] = []
    cur = ""
    for kind, content in parts:
        if kind == "c":
            # Flush current buffer first
            if cur:
                chunks.append(cur.rstrip())
                cur = ""
            chunks.append(content)  # code blocks go whole, even if > limit
            continue
        # Paragraph — try to append, split by blank line if too big
        for para in re.split(r"\n\n+", content):
            piece = para.strip()
            if not piece:
                continue
            if len(cur) + len(piece) + 2 <= limit:
                cur = cur + "\n\n" + piece if cur else piece
            else:
                if cur:
                    chunks.append(cur.rstrip())
                    cur = ""
                if len(piece) <= limit:
                    cur = piece
                else:
                    # Still too big — fall back to line chunking
                    for sub in _line_chunks(piece, limit):
                        chunks.append(sub)
    if cur:
        chunks.append(cur.rstrip())
    return chunks or ["..."]


def _line_chunks(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur); cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        out.append(cur)
    return out


# ─────────────── Drive one turn through the user's AgentLoop ───────────────

async def drive_turn(us: UserSession, user_text: str,
                     *, on_turn_update: Callable[[int], Awaitable[None]] | None = None
                     ) -> tuple[str, bool]:
    """Feed `user_text` into the user's loop and collect streamed text.
    Returns (reply_text, aborted_flag). Caller strips/splits for their IM."""
    loop = us.loop
    loop.add_user(user_text)
    us.aborted = False
    # Remember a preview (first 60 chars of stripped text) for /chat list.
    preview = user_text.strip().splitlines()[0][:60] if user_text.strip() else ""
    if preview and not preview.startswith("(系统"):
        us.last_user_preview = preview
    pieces: list[str] = []
    turn_count = 0
    async for ev in loop.run():
        if ev.kind == "text" and ev.data:
            pieces.append(ev.data)
        elif ev.kind == "tool_call":
            turn_count += 1
            if on_turn_update:
                try:
                    await on_turn_update(turn_count)
                except Exception:
                    pass
        elif ev.kind == "done":
            break
        if us.aborted:
            break
    # Increment + maybe-archive via the parent sessions container if attached.
    # Falls back to a bare counter bump when run outside a PerUserSessions
    # context (legacy callers, tests).
    if us.parent_sessions is not None:
        us.parent_sessions.note_turn_complete(us)
    else:
        us.turns += 1
    reply = "".join(pieces)
    return reply, us.aborted
