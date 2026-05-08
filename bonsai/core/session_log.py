"""Session log — append-only JSONL of turns. Consumed by background writer."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import orjson

from .types import Message, ToolCall, ToolResult

log = logging.getLogger(__name__)


def load_messages(path: Path) -> list[Message]:
    """Re-hydrate a session's JSONL back into AgentLoop-shaped Messages.

    Used by the IM / CLI resume flow so users returning to a conversation
    pick up with the prior tail rather than a blank slate. Best-effort:
    malformed lines are skipped.
    """
    msgs: list[Message] = []
    if not path.exists():
        return msgs
    with path.open("rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                e = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            role = e.get("role")
            if role == "user" and e.get("content"):
                msgs.append(Message(role="user", content=e["content"]))
            elif role == "assistant":
                tcs = [ToolCall(id=tc.get("id", ""), name=tc.get("name", ""),
                                args=tc.get("args") or {})
                       for tc in (e.get("tool_calls") or [])]
                msgs.append(Message(role="assistant",
                                    content=e.get("content"), tool_calls=tcs))
            elif role == "tool":
                trs = [ToolResult(tool_call_id=tr.get("id", ""),
                                  content=tr.get("content", ""),
                                  is_error=bool(tr.get("is_error")))
                       for tr in (e.get("tool_results") or [])]
                msgs.append(Message(role="tool", tool_results=trs))
    return msgs


class SessionLog:
    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: dict) -> None:
        entry.setdefault("t", time.time())
        entry.setdefault("session_id", self.session_id)
        with self.path.open("ab") as f:
            f.write(orjson.dumps(entry, default=str) + b"\n")

    def record_user(self, text: str) -> None:
        self._write({"role": "user", "content": text})

    def record_assistant(self, msg: Message, *, provider: str = "",
                        model: str = "", turn: int = 0) -> None:
        entry: dict = {"role": "assistant", "turn": turn,
                       "provider": provider, "model": model}
        if msg.content:
            entry["content"] = msg.content
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in msg.tool_calls
            ]
        self._write(entry)

    def record_tool_results(self, msg: Message, *, turn: int = 0) -> None:
        self._write({
            "role": "tool", "turn": turn,
            "tool_results": [
                {"id": tr.tool_call_id, "content": tr.content,
                 "is_error": tr.is_error}
                for tr in msg.tool_results
            ],
        })

    def reload_into(self, loop) -> int:
        """Refresh `loop.tail.messages` from this session's jsonl on disk.

        Cross-process consistency: web server (in-process) and channel runner
        (subprocess) both share this jsonl as the source of truth. Calling
        this before `add_user` lets each side pick up messages the other side
        wrote since the last turn — so a conversation can flow across web
        and WeChat without diverging.

        Returns the message count loaded. Best-effort: missing or unreadable
        files leave `loop.tail.messages` untouched.
        """
        try:
            msgs = load_messages(self.path)
        except Exception as e:
            log.warning("reload_into %s failed: %s", self.path.name, e)
            return len(loop.tail.messages)
        # Replace wholesale — disk is source of truth post-reload, including
        # other-process turns we haven't seen. Compression cache is dropped;
        # next loop.run() will re-compress if needed.
        loop.tail.messages = msgs
        return len(msgs)

    def record_tool_call_pending(self, tc: ToolCall, *, turn: int = 0) -> None:
        # 心跳行: stream 还没 commit 整轮 record_assistant 时,先写一条
        # 让 web live-poll 能立刻看到"agent 决定调工具了"。被随后到来的
        # record_assistant 在视觉上覆盖,load_messages 不重放此 role。
        self._write({
            "role": "tool_call_pending", "turn": turn,
            "tcid": tc.id, "name": tc.name, "args": tc.args,
        })
