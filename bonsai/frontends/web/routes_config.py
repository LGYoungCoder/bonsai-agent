"""Config GET / POST + provider connectivity test."""

from __future__ import annotations

import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ._common import (
    _channel_creds_changed,
    _preserve_memory_secrets,
    _preserve_provider_secrets,
    _preserve_secrets,
    _redact_channel,
    _redact_provider,
    _resolve_real_api_key,
    _scrub_invisibles,
)

log = logging.getLogger(__name__)


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config")
    async def api_get_config() -> JSONResponse:
        from ...config import load_config
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            return JSONResponse({"exists": False, "providers": [],
                                 "failover": [], "memory": {}, "agent": {}})
        return JSONResponse({
            "exists": True,
            "providers": [_redact_provider(p) for p in cfg.providers],
            "failover": list(cfg.failover_chain),
            "memory": {
                "skill_dir": cfg.memory.skill_dir,
                "memory_db": cfg.memory.memory_db,
                "embed_provider": cfg.memory.embed_provider,
                "embed_model": cfg.memory.embed_model,
                "embed_base_url": cfg.memory.embed_base_url,
                "embed_api_key_set": bool(cfg.memory.embed_api_key),
            },
            "agent": {
                "max_turns": cfg.agent.max_turns,
                "budget_soft": cfg.agent.budget_soft,
                "budget_hard": cfg.agent.budget_hard,
                "working_dir": cfg.agent.working_dir,
            },
            "logging": {
                "level": cfg.logging.level,
                "cache_stats": cfg.logging.cache_stats,
            },
            "maintenance": {
                "gc_enabled": cfg.maintenance.gc_enabled,
                "gc_retention_days": cfg.maintenance.gc_retention_days,
                "gc_interval_hours": cfg.maintenance.gc_interval_hours,
            },
            "channels": {k: _redact_channel(v) for k, v in (cfg.channels or {}).items()},
        })

    @router.post("/api/config")
    async def api_save_config(request: Request) -> JSONResponse:
        body = orjson.loads(await request.body())
        providers = body.get("providers") or []
        failover_chain = body.get("failover") or [p["name"] for p in providers if p.get("name")]
        memory_cfg = dict(body.get("memory") or {})
        channels_in = body.get("channels") or {}
        agent_in = body.get("agent") or {}
        logging_in = body.get("logging") or {}
        if not providers:
            raise HTTPException(400, "at least one provider required")
        # Scrub invisibles from values that end up in HTTP headers.
        _scrub_invisibles(providers, memory_cfg, channels_in)
        # merge saved-but-redacted secrets back from existing config so users
        # aren't forced to retype them every save
        _preserve_provider_secrets(root, providers)
        _preserve_secrets(root, channels_in)
        _preserve_memory_secrets(root, memory_cfg)
        memory_cfg["_channels"] = channels_in
        memory_cfg["_agent"] = agent_in
        memory_cfg["_logging"] = logging_in
        memory_cfg["_maintenance"] = body.get("maintenance") or {}
        # Snapshot OLD channel block before we overwrite, so we can detect which
        # already-running runners need restart due to credential changes.
        from ...config import load_config as _load_cfg
        config_path = root / "config.toml"
        old_channels: dict[str, dict] = {}
        if config_path.exists():
            try:
                old_channels = dict(_load_cfg(config_path).channels or {})
            except Exception:
                old_channels = {}
        try:
            from ...cli.setup_wizard import _write_config, init_stores
            _write_config(config_path, providers=providers, memory_cfg=memory_cfg,
                          failover_chain=failover_chain)
            stores = init_stores(root, memory_cfg)
        except Exception as e:
            log.exception("config write failed")
            raise HTTPException(500, f"write failed: {e}")
        # Hot-reload: push the freshly written config to all in-process listeners
        # immediately (live web sessions, in-process channel ctx if any). Channel
        # subprocesses pick up via their own file watchers within ~2s.
        reloaded = 0
        new_cfg = None
        try:
            from ...runtime import trigger_reload
            new_cfg = _load_cfg(config_path)
            reloaded = trigger_reload(new_cfg)
        except Exception as e:
            log.warning("hot-reload after save failed: %s", e)
        # Channel binding: restart already-running runners whose credentials
        # changed. enabled-toggle stays user-driven via existing start/stop
        # buttons (won't auto-start a disabled runner).
        restarted: list[str] = []
        try:
            new_channels = dict((new_cfg.channels if new_cfg else {}) or {})
            from ...channels.supervisor import (
                _SUPPORTED, status as _ch_status, stop as _ch_stop, start as _ch_start,
            )
            for kind in _SUPPORTED:
                if not _ch_status(root, kind).get("running"):
                    continue
                old_block = old_channels.get(kind, {}) or {}
                new_block = new_channels.get(kind, {}) or {}
                if not _channel_creds_changed(old_block, new_block):
                    continue
                allow = (new_block.get("allow") or "")
                if isinstance(allow, list):
                    allow = ",".join(allow)
                try:
                    _ch_stop(root, kind)
                    _ch_start(root, kind, allow=allow)
                    restarted.append(kind)
                    log.info("channel %s auto-restarted (creds changed)", kind)
                except Exception as e:
                    log.warning("channel %s auto-restart failed: %s", kind, e)
        except Exception as e:
            log.warning("channel auto-restart sweep failed: %s", e)
        return JSONResponse({"ok": True, "path": str(config_path),
                             "stores": stores, "hot_reloaded": reloaded,
                             "restarted_channels": restarted})

    @router.post("/api/config/test")
    async def api_test_provider(request: Request) -> JSONResponse:
        body = orjson.loads(await request.body())
        kind = body.get("kind") or ""
        api_key = body.get("api_key") or ""
        base_url = body.get("base_url") or ""
        model = body.get("model") or ""
        name = body.get("name") or ""
        if not (kind and base_url and model):
            raise HTTPException(400, "kind / base_url / model required")
        # 重启后 UI 里拿到的 api_key 是脱敏串 ("••••xxxx") 或 $ref:env:X 形态。
        # 直接扔给 provider 必 401。先落回磁盘取真值 + 解 $ref:。
        api_key = _resolve_real_api_key(root, name=name, base_url=base_url,
                                         model=model, provided=api_key)
        if not api_key:
            return JSONResponse({
                "ok": False,
                "message": "api_key 是空的 / 脱敏串 / $ref 解不出来。"
                           "请在输入框重新粘贴 key 再点 Test。"
            })
        from ...cli.setup_wizard import ping_provider_by_kind
        ok, msg = ping_provider_by_kind(kind, api_key=api_key, base_url=base_url, model=model)
        return JSONResponse({"ok": ok, "message": msg})

    return router
