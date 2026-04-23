"""Config loader. Supports $ref:env:NAME and $ref:file:PATH substitutions.

$ref:secrets:NAME (system keyring) is documented in docs/SAFETY.md but not
implemented in Sprint 1 — it requires `keyring` which we don't want as a
base dependency. Falls back to env with a warning.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)


_REF_PATTERN = re.compile(r"^\$ref:(env|file|secrets):(.+)$")


def _resolve_ref(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    m = _REF_PATTERN.match(v)
    if not m:
        return v
    kind, name = m.group(1), m.group(2)
    if kind == "env":
        val = os.environ.get(name)
        if val is None:
            log.warning("env var %s not set", name)
            return ""
        return val
    if kind == "file":
        path = Path(name).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            log.warning("ref file not found: %s", path)
            return ""
    if kind == "secrets":
        # Try keyring; fall back to env BONSAI_<NAME>.
        try:
            import keyring  # type: ignore
            val = keyring.get_password("bonsai", name)
            if val:
                return val
        except Exception:
            pass
        fallback = os.environ.get(f"BONSAI_{name}")
        if fallback:
            return fallback
        log.warning("secret %s not found in keyring or BONSAI_%s env", name, name)
        return ""
    return v


def _walk_and_resolve(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_and_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_resolve(v) for v in obj]
    return _resolve_ref(obj)


@dataclass
class AgentConfig:
    max_turns: int = 40
    budget_hard: int = 60_000
    budget_soft: int = 40_000
    working_dir: str = "."


@dataclass
class MemoryConfig:
    skill_dir: str = "./skills"
    memory_db: str = "./memory.db"
    embed_model: str = "bge-m3"
    embed_remote: bool = False
    embed_provider: str = "hash"
    embed_api_key: str = ""
    embed_base_url: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"
    cache_stats: str = "./logs/cache_stats.jsonl"


@dataclass
class MaintenanceConfig:
    """长期跑下的清理 — 在 bonsai 进程内自动周期跑, 用户在 config 里开关。"""
    gc_enabled: bool = True
    gc_retention_days: int = 15
    gc_interval_hours: int = 24


@dataclass
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    providers: list[dict] = field(default_factory=list)
    failover_chain: list[str] = field(default_factory=list)
    frontend_default: str = "cli"
    channels: dict[str, dict] = field(default_factory=dict)

    def provider(self, name: str) -> dict:
        for p in self.providers:
            if p["name"] == name:
                return p
        raise KeyError(f"provider {name!r} not in config")

    def failover_providers(self) -> list[dict]:
        return [self.provider(n) for n in self.failover_chain] \
               if self.failover_chain else list(self.providers)


def _find_config_path(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    candidates = [
        Path.cwd() / "config.toml",
        Path.home() / ".bonsai" / "config.toml",
        Path.home() / ".config" / "bonsai" / "config.toml",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"No config.toml found. Checked: {[str(p) for p in candidates]}. "
        "Copy config.example.toml to ./config.toml or ~/.bonsai/config.toml."
    )


def load_config(path: Path | None = None) -> Config:
    cfg_path = _find_config_path(path)
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)
    raw = _walk_and_resolve(raw)

    agent = AgentConfig(**(raw.get("agent") or {}))
    memory = MemoryConfig(**(raw.get("memory") or {}))
    logging_cfg = LoggingConfig(**(raw.get("logging") or {}))
    maintenance_cfg = MaintenanceConfig(**(raw.get("maintenance") or {}))
    providers = raw.get("providers") or []
    failover_chain = (raw.get("failover") or {}).get("chain") or []
    frontend_default = (raw.get("frontend") or {}).get("default", "cli")

    channels = raw.get("channels") or {}

    return Config(
        agent=agent,
        memory=memory,
        logging=logging_cfg,
        maintenance=maintenance_cfg,
        providers=providers,
        failover_chain=failover_chain,
        frontend_default=frontend_default,
        channels=channels,
    )
