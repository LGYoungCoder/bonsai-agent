"""Distill SOPs from successful tool_call sequences. Runs off-chat.

Uses a cheap/fast model (configurable) to turn a transcript of successful
tool_calls into a structured SOP markdown. Never inline — always spawned as
a subprocess. Output must pass SkillStore.write_sop's evidence check.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import orjson

from ..stores.skill_store import SkillStore

log = logging.getLogger(__name__)


SOP_PROMPT = """你在看一段成功完成任务的 tool_call 记录。请把它提炼成一份可复用的 SOP:

要求:
1. 名字要短,描述目标(动词+对象)
2. 包含 "何时用"、"前置条件"、"步骤(1.2.3.)"、"典型坑" 四个小节
3. 对于关键命令,展示真实的命令行(来自记录)
4. 不要加任何我没在记录里看到的细节

输出格式:纯 markdown,不要 ```。
"""


def distill(session_file: Path, skill_store: SkillStore,
            backend_cfg: dict) -> Path | None:
    """Extract a SOP from a session and write it. Returns path or None."""
    if not session_file.exists():
        return None

    # Load the session turns + evidence.
    turns: list[dict] = []
    with session_file.open("rb") as f:
        for line in f:
            try:
                turns.append(orjson.loads(line))
            except Exception:
                continue

    tool_calls = [t for t in turns if t.get("tool_calls")]
    if not tool_calls:
        log.info("no tool_calls in session — nothing to distill")
        return None

    # Build the evidence record.
    evidence = {
        "session_file": str(session_file),
        "tool_calls": [
            {"name": tc.get("name"), "args": tc.get("args"),
             "turn": t.get("turn"), "is_error": tc.get("is_error", False)}
            for t in tool_calls for tc in (t.get("tool_calls") or [])
        ],
    }

    # Ask the LLM.
    import asyncio

    from ..adapters import build_adapter
    from ..core.types import DynamicTail, FrozenPrefix, Message

    transcript = _render_transcript(turns)
    backend = build_adapter(backend_cfg)
    prefix = FrozenPrefix(system_prompt=SOP_PROMPT, tools=[])
    tail = DynamicTail(messages=[Message(role="user", content=transcript)])

    async def run() -> str:
        resp = await backend.chat(prefix, tail, max_tokens=1500)
        return resp.content

    body = asyncio.run(run())
    if not body.strip():
        log.warning("distiller produced empty body")
        return None

    name = _pick_name(body)
    path = skill_store.write_sop(name=name, content=body, evidence=evidence)
    log.info("distilled SOP → %s", path)
    return path


def _render_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns[:60]:  # hard cap
        role = t.get("role", "?")
        if t.get("content"):
            lines.append(f"[{role}] {t['content'][:800]}")
        for tc in t.get("tool_calls") or []:
            lines.append(f"  → {tc.get('name')}({orjson.dumps(tc.get('args', {})).decode()[:300]})")
        for tr in t.get("tool_results") or []:
            lines.append(f"  ← {str(tr.get('content', ''))[:500]}")
    return "\n".join(lines)


def _pick_name(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:40]
        if line and not line.startswith("-"):
            return line[:40]
    return "distilled_sop"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_file", type=Path)
    ap.add_argument("--skill-dir", type=Path, required=True)
    ap.add_argument("--provider-config", type=Path, required=True,
                    help="JSON file with backend config")
    args = ap.parse_args()

    logging.basicConfig(level="INFO")
    store = SkillStore(args.skill_dir)
    store.init()
    cfg = orjson.loads(args.provider_config.read_bytes())
    distill(args.session_file, store, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
