"""Agent assembly — shared between CLI chat and channel runners.

build_agent() takes a Config + root and hands back everything a caller needs
to drive AgentLoop turn-by-turn. Factored out so chat REPL, WeChat bot, and
future channel runners construct the same agent identically.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import build_adapter
from .config import Config, load_config
from .core.backend import FailoverChain, MutableBackend
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

log = logging.getLogger(__name__)


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
    raw_chain = FailoverChain(backends=backends, monitor=monitor)
    # Wrap so config-driven provider changes can be hot-swapped into live
    # AgentLoops without rebuilding the whole context. See reload_agent_context().
    chain = MutableBackend(raw_chain)

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


# ─────────────────────── Hot-reload infrastructure ────────────────────────
#
# Goal: edit `config.toml` (via the Web UI 🔑 tab or hand-edit) and have
# providers / failover order / agent params take effect *without* restarting
# `bonsai serve` or any channel runner.
#
# Two propagation paths cover the architecture:
#   - Same process (web UI):    POST /api/config calls trigger_reload() directly
#   - Cross process (runners):  start_config_watcher() polls config.toml mtime
#                                and calls trigger_reload() on change

_RELOADERS: list[Callable[[Config], None]] = []
_RELOADERS_LOCK = threading.Lock()


def register_hot_reloader(fn: Callable[[Config], None]) -> Callable[[], None]:
    """Register a callback that receives the freshly loaded Config when
    config.toml changes. Returns an unregister callable."""
    with _RELOADERS_LOCK:
        _RELOADERS.append(fn)
    def _unreg() -> None:
        with _RELOADERS_LOCK:
            try:
                _RELOADERS.remove(fn)
            except ValueError:
                pass
    return _unreg


def trigger_reload(cfg: Config) -> int:
    """Run all registered reloaders with `cfg`. Returns success count."""
    with _RELOADERS_LOCK:
        snapshot = list(_RELOADERS)
    n = 0
    for fn in snapshot:
        try:
            fn(cfg)
            n += 1
        except Exception as e:
            log.warning("hot reloader %r failed: %s", fn, e)
    return n


_MEMORY_KEYS = ("memory_db", "embed_provider", "embed_model",
                "embed_api_key", "embed_base_url")


def _memory_settings_changed(old_cfg: Config | None, new_cfg: Config) -> bool:
    if old_cfg is None:
        return False
    o, n = old_cfg.memory, new_cfg.memory
    return any(getattr(o, k, None) != getattr(n, k, None) for k in _MEMORY_KEYS)


def _rebuild_memory_store(root: Path, cfg: Config) -> MemoryStore:
    return MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=build_embedder({
            "embed_provider": cfg.memory.embed_provider,
            "embed_api_key": cfg.memory.embed_api_key,
            "embed_base_url": cfg.memory.embed_base_url,
            "embed_model": cfg.memory.embed_model,
        }),
    )


def reload_agent_context(ctx: AgentContext, cfg: Config, *,
                         root: Path | None = None) -> None:
    """Apply a fresh Config to an existing AgentContext in-place.

    Hot-swaps providers + failover (immediately visible to live AgentLoops via
    MutableBackend) and refreshes agent params + cfg ref (visible to *new*
    AgentLoop instances created after this call — old loops keep their
    captured policy/max_turns until they end).

    If `root` is given and memory.embed_provider / embed_model / memory_db
    changed, also rebuilds MemoryStore and swaps it into ctx.handler.
    """
    providers = cfg.failover_providers()
    if not providers:
        log.warning("hot reload skipped: cfg has no providers")
        return
    new_chain = FailoverChain(
        backends=[build_adapter(p) for p in providers],
        monitor=ctx.monitor,
    )
    if isinstance(ctx.chain, MutableBackend):
        ctx.chain.swap(new_chain)
    else:
        # Pre-existing context that wasn't built with MutableBackend; wrap now.
        ctx.chain = MutableBackend(new_chain)
    ctx.policy = BudgetPolicy(soft=cfg.agent.budget_soft, hard=cfg.agent.budget_hard)
    ctx.max_turns = cfg.agent.max_turns
    if root is not None and _memory_settings_changed(ctx.cfg, cfg):
        old_store = ctx.handler.memory_store
        try:
            new_store = _rebuild_memory_store(root, cfg)
        except Exception as e:
            log.warning("memory hot-reload skipped (rebuild failed): %s", e)
        else:
            ctx.handler.memory_store = new_store
            try:
                if old_store is not None:
                    old_store.close()
            except Exception as e:
                log.debug("old memory_store close failed: %s", e)
            log.info("memory store hot-reloaded "
                     "(embed_provider=%s embed_model=%s db=%s)",
                     cfg.memory.embed_provider, cfg.memory.embed_model,
                     cfg.memory.memory_db)
    ctx.cfg = cfg
    log.info("agent context hot-reloaded: providers=%s failover=%s max_turns=%d",
             [p["name"] for p in cfg.providers],
             cfg.failover_chain or "(default)", cfg.agent.max_turns)


def start_config_watcher(config_path: Path, *, interval: float = 2.0,
                         daemon: bool = True) -> threading.Thread:
    """Background thread: poll config.toml mtime; on change, load_config +
    trigger_reload. One per process (web server + each runner subprocess).

    File polling instead of inotify because we run on bare Linux without
    extra deps; 2s tick is plenty for a personal agent's config flips.
    """
    last_mtime = [config_path.stat().st_mtime if config_path.exists() else 0.0]

    def _watch() -> None:
        while True:
            try:
                if config_path.exists():
                    mt = config_path.stat().st_mtime
                    if mt != last_mtime[0]:
                        last_mtime[0] = mt
                        try:
                            cfg = load_config(config_path)
                        except Exception as e:
                            log.warning("config.toml mtime changed but parse failed: %s", e)
                        else:
                            n = trigger_reload(cfg)
                            log.info("config.toml hot-reloaded → %d listener(s)", n)
            except Exception as e:
                log.debug("config watcher tick failed: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_watch, daemon=daemon, name="bonsai-config-watcher")
    t.start()
    return t
