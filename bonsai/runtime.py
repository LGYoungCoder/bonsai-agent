"""Agent assembly — shared between CLI chat and channel runners.

build_agent() takes a Config + root and hands back everything a caller needs
to drive AgentLoop turn-by-turn. Factored out so chat REPL, WeChat bot, and
future channel runners construct the same agent identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters import build_adapter
from .config import Config
from .core.backend import FailoverChain
from .core.budget import BudgetPolicy
from .core.cache_monitor import CacheMonitor
from .core.handler import Handler
from .core.session import Session
from .core.session_log import SessionLog
from .core.types import FrozenPrefix
from .core.wakeup import build_wakeup
from .stores.embed import build_embedder
from .stores.memory_store import MemoryStore
from .stores.skill_store import SkillStore
from .tools.schema_spec import ALL_TOOLS, load_tool_specs


@dataclass
class AgentContext:
    chain: FailoverChain
    prefix: FrozenPrefix
    handler: Handler
    policy: BudgetPolicy
    session: Session
    session_log: SessionLog
    monitor: CacheMonitor
    max_turns: int
    # Needed so downstream (archive, gc) can reach memory.db / embedder conf
    # without re-parsing config. Added in the "永运行" sprint.
    cfg: Any = None
    # Base system prompt (without wakeup) so callers can rebuild the prefix
    # each turn with a fresh wakeup rendering — picks up new SOPs, new L0,
    # new memory drawers without restarting the bot.
    base_system_prompt: str = ""


def build_agent(root: Path, cfg: Config, *,
                system_prompt: str = "",
                prompt_fn: Callable | None = None) -> AgentContext:
    providers = cfg.failover_providers()
    if not providers:
        raise RuntimeError("config.toml 没有配置任何 provider")

    backends = [build_adapter(p) for p in providers]
    monitor = CacheMonitor(log_path=Path(cfg.logging.cache_stats))
    chain = FailoverChain(backends=backends, monitor=monitor)

    skill_store = SkillStore(root / cfg.memory.skill_dir.lstrip("./"))
    skill_store.init()
    embedder = build_embedder({
        "embed_provider": cfg.memory.embed_provider,
        "embed_api_key": cfg.memory.embed_api_key,
        "embed_base_url": cfg.memory.embed_base_url,
        "embed_model": cfg.memory.embed_model,
    })
    memory_store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=embedder,
    )

    base_sys = system_prompt
    prefix_text = render_wakeup_prefix(base_sys, skill_store, memory_store)

    schema_path = root / "tools" / "schema.json"
    # Always expose web_* tools — Handler lazy-spawns a managed chromium
    # on first call so web UI / IM bots get browser capability without an
    # explicit flag. build_agent is the non-interactive path (IM bots,
    # scheduler), so we pin headless=True here. Interactive paths (CLI /
    # web session) construct Handler directly and let the display auto-
    # detect kick in.
    tool_specs = (
        load_tool_specs(schema_path, names=ALL_TOOLS, include_memory_recall=True)
        if schema_path.exists() else []
    )
    prefix = FrozenPrefix(system_prompt=prefix_text, tools=tool_specs)

    session = Session(cwd=root)
    # Evidence recorder feeds `bonsai distill`; without it web UI / IM bot
    # usage is invisible to SOP distillation. Keyed on this context's
    # session_id — for multi-user IM that's a channel-wide bucket, better
    # than nothing until a per-user design lands.
    from .stores.evidence import EvidenceRecorder
    evidence = EvidenceRecorder(skill_store.root, session_id=session.session_id)
    handler = Handler(session=session, schema_path=schema_path,
                      prompt_fn=prompt_fn or _default_prompt,
                      memory_store=memory_store, skill_store=skill_store,
                      evidence=evidence, browser_headless=True)
    policy = BudgetPolicy(soft=cfg.agent.budget_soft, hard=cfg.agent.budget_hard)
    sess_log = SessionLog(
        root / "logs" / "sessions" / f"{session.session_id}.jsonl",
        session.session_id,
    )

    return AgentContext(chain=chain, prefix=prefix, handler=handler,
                         policy=policy, session=session, session_log=sess_log,
                         monitor=monitor, max_turns=cfg.agent.max_turns,
                         cfg=cfg, base_system_prompt=base_sys)


def render_wakeup_prefix(base_sys: str, skill_store, memory_store) -> str:
    """Compose system_prompt + current wakeup render. Called both at
    build_agent() time AND every turn by callers that want dynamic refresh.

    Deterministic: given identical skill_store / memory_store state the
    output is byte-identical, so repeated use keeps the prompt cache hot.
    """
    w = build_wakeup(skill_store, memory_store).render()
    if not w:
        return base_sys
    return f"{base_sys}\n\n{w}" if base_sys else w


async def _default_prompt(question: str, candidates: list[str] | None) -> str:
    """ask_user in non-interactive contexts — decline politely instead of blocking."""
    return "(ask_user unavailable in this channel)"
