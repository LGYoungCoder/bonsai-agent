"""End-to-end integration test against GLM-5 (Anthropic-native endpoint).

Exercises: single Q&A · tool use (file_read + code_run) · parallel tool use ·
memory search against a pre-seeded store · wake-up prefix · background ingest.

Run:
  PYTHONPATH=. python3 benchmarks/e2e_glm.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bonsai.adapters import build_adapter
from bonsai.config import load_config
from bonsai.core.backend import FailoverChain
from bonsai.core.budget import BudgetPolicy
from bonsai.core.cache_monitor import CacheMonitor
from bonsai.core.handler import Handler
from bonsai.core.loop import AgentLoop
from bonsai.core.session import Session
from bonsai.core.session_log import SessionLog
from bonsai.core.types import FrozenPrefix, StreamEvent
from bonsai.core.wakeup import build_wakeup
from bonsai.stores.embed import build_embedder
from bonsai.stores.memory_store import MemoryStore
from bonsai.stores.skill_store import SkillStore
from bonsai.tools.schema_spec import SPRINT2_TOOLS, load_tool_specs

logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("e2e")

ROOT = Path("/opt/lg/bonsai").resolve()


async def build_loop(cfg, *, skill_store, memory_store, session_log,
                      sys_prompt_suffix: str = "") -> tuple[AgentLoop, CacheMonitor]:
    providers = cfg.failover_providers()
    backends = [build_adapter(p) for p in providers]
    monitor = CacheMonitor(log_path=None)
    chain = FailoverChain(backends=backends, monitor=monitor)

    wakeup = build_wakeup(skill_store, memory_store)
    base_prompt = "你是 Bonsai —— 用户本人的 agent。答复简洁,动手优先。"
    sys_prompt = base_prompt + sys_prompt_suffix
    if wakeup.render():
        sys_prompt = f"{sys_prompt}\n\n{wakeup.render()}"

    specs = load_tool_specs(ROOT / "tools" / "schema.json",
                            names=SPRINT2_TOOLS, include_memory_recall=True)
    prefix = FrozenPrefix(system_prompt=sys_prompt, tools=specs)

    session = Session(cwd=ROOT)
    handler = Handler(session=session, memory_store=memory_store, skill_store=skill_store)

    loop = AgentLoop(chain, prefix, handler,
                     policy=BudgetPolicy(soft=40_000, hard=60_000),
                     max_turns=8, session_log=session_log)
    return loop, monitor


async def drive(loop: AgentLoop, user_text: str) -> dict:
    loop.add_user(user_text)
    summary = {"text": [], "tool_calls": [], "usage": None, "done": None}
    async for ev in loop.run():
        if ev.kind == "text" and ev.data:
            summary["text"].append(ev.data)
        elif ev.kind == "tool_call":
            summary["tool_calls"].append({"name": ev.data.name, "args": ev.data.args})
        elif ev.kind == "usage":
            summary["usage"] = ev.data
        elif ev.kind == "done":
            summary["done"] = ev.data
    return summary


async def test_1_simple_qa(cfg, skill_store, memory_store, session_log):
    print("\n═══ test 1: simple QA ═══")
    loop, mon = await build_loop(cfg, skill_store=skill_store,
                                  memory_store=memory_store, session_log=session_log)
    r = await drive(loop, "一句话介绍你自己(不要用工具)。")
    print(f"  reply: {''.join(r['text'])[:200]}")
    print(f"  tool_calls: {len(r['tool_calls'])}")
    print(f"  done: {r['done']}")
    assert r["done"] is not None, "agent didn't terminate"
    print("  ✓ passed")


async def test_2_tool_use_readfile(cfg, skill_store, memory_store, session_log):
    print("\n═══ test 2: read file via tool ═══")
    target = ROOT / "README.md"
    loop, mon = await build_loop(cfg, skill_store=skill_store,
                                  memory_store=memory_store, session_log=session_log)
    r = await drive(loop, f"用 file_read 读 {target} 并告诉我它前 3 行写的是什么。")
    print(f"  tool_calls: {[tc['name'] for tc in r['tool_calls']]}")
    print(f"  final reply: {''.join(r['text'])[:200]}")
    assert any(tc["name"] == "file_read" for tc in r["tool_calls"]), \
        "expected file_read call"
    print("  ✓ passed")


async def test_3_parallel_reads(cfg, skill_store, memory_store, session_log):
    print("\n═══ test 3: parallel tool use ═══")
    f1 = ROOT / "README.md"
    f2 = ROOT / "ARCHITECTURE.md"
    loop, mon = await build_loop(cfg, skill_store=skill_store,
                                  memory_store=memory_store, session_log=session_log)
    r = await drive(loop,
        f"并行读 {f1} 和 {f2} 两个文件各前 20 行,比较哪个更长。")
    reads = [tc for tc in r["tool_calls"] if tc["name"] == "file_read"]
    print(f"  file_read calls: {len(reads)}")
    print(f"  done: {r['done']}")
    # We accept either parallel (1 turn, 2 calls) or serial — don't fail,
    # just report.
    if r["done"] and r["done"].get("turns", 99) <= 2:
        print("  ✓ finished in ≤2 turns (likely parallel)")
    else:
        print(f"  ⚠ finished in {r['done'].get('turns') if r['done'] else '?'} turns (check parallel support)")


async def test_4_memory_roundtrip(cfg, skill_store, memory_store, session_log):
    print("\n═══ test 4: memory store & search roundtrip ═══")
    # Seed some drawers directly
    memory_store.ingest(closet="life", wing="e2e-test",
                        room="test-session",
                        kind="note",
                        content="用户偏好简洁的中文回答。喜欢命令式的步骤说明。")
    memory_store.ingest(closet="life", wing="e2e-test",
                        room="test-session",
                        kind="note",
                        content="2026-04-21 今天在 Bonsai 项目给 GLM-5 接上了 Anthropic 端点。")
    loop, mon = await build_loop(cfg, skill_store=skill_store,
                                  memory_store=memory_store, session_log=session_log)
    r = await drive(loop,
        "用 memory_search 查一下关于 GLM 的记忆,告诉我你找到了什么。")
    names = [tc["name"] for tc in r["tool_calls"]]
    print(f"  tool_calls: {names}")
    print(f"  final: {''.join(r['text'])[:300]}")
    assert "memory_search" in names, "expected memory_search call"
    print("  ✓ passed")


async def test_5_code_run(cfg, skill_store, memory_store, session_log):
    print("\n═══ test 5: code_run ═══")
    loop, mon = await build_loop(cfg, skill_store=skill_store,
                                  memory_store=memory_store, session_log=session_log)
    r = await drive(loop, "用 code_run 执行 python 算出 fib(15) 的值。")
    names = [tc["name"] for tc in r["tool_calls"]]
    print(f"  tool_calls: {names}")
    final = "".join(r["text"])
    print(f"  final: {final[:200]}")
    assert "code_run" in names, "expected code_run call"
    # fib(15) = 610
    assert "610" in final or "610" in str(r), "expected 610 in final answer"
    print("  ✓ passed")


async def main() -> None:
    cfg = load_config(ROOT / "config.toml")
    skill_store = SkillStore(ROOT / "skills")
    skill_store.init()
    memory_store = MemoryStore(
        ROOT / "memory" / "memory_e2e.db",
        embedder=build_embedder({"embed_provider": "hash"}),
    )
    log_path = ROOT / "logs" / "sessions" / f"e2e_{int(time.time())}.jsonl"
    session_log = SessionLog(log_path, session_id="e2e")

    t0 = time.time()
    try:
        await test_1_simple_qa(cfg, skill_store, memory_store, session_log)
        await test_2_tool_use_readfile(cfg, skill_store, memory_store, session_log)
        await test_3_parallel_reads(cfg, skill_store, memory_store, session_log)
        await test_4_memory_roundtrip(cfg, skill_store, memory_store, session_log)
        await test_5_code_run(cfg, skill_store, memory_store, session_log)
    finally:
        memory_store.close()
        print(f"\ntotal: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
