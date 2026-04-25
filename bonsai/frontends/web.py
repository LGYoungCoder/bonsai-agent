"""FastAPI + WebSocket frontend. Serves the unified app.html.

Usage:
  bonsai serve --host 0.0.0.0 --port 7878
then open http://localhost:7878/ in a browser.

Routes:
  GET  /                       → app.html (unified chat+config+skills+status)
  GET  /api/bootstrap          → first-run detection + version info
  GET  /api/config             → current config.toml (secrets redacted)
  POST /api/config             → write config.toml (atomic + backup)
  POST /api/config/test        → ping a single provider
  GET  /api/skills             → SkillStore listing (L1/L2/L3)
  GET  /api/doctor             → run doctor checks, return JSON
  WS   /ws                     → chat stream
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import orjson
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)


def make_app(root: Path, chat_factory) -> FastAPI:
    """chat_factory is a callable returning (AgentLoop, Session, Handler, prompt_resolver)
    per new websocket session. prompt_resolver is an async fn to resolve ask_user."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        _autostart_channels(root)
        # Background scheduler task — runs alongside serve, stops with it.
        from ..scheduler import scheduler_loop
        stop_evt = asyncio.Event()
        sched_task = asyncio.create_task(scheduler_loop(root, stop_evt=stop_evt))
        # Periodic gc daemon (in-process, configurable via [maintenance]).
        try:
            from ..config import load_config as _lc
            from ..maintenance import start_maintenance
            start_maintenance(root, _lc(None))
        except Exception as e:
            log.warning("could not start maintenance: %s", e)
        try:
            yield
        finally:
            stop_evt.set()
            sched_task.cancel()
            try:
                await sched_task
            except (asyncio.CancelledError, Exception):
                pass

    app = FastAPI(title="Bonsai Web", lifespan=lifespan)

    app_html = root / "assets" / "app.html"

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if app_html.exists():
            return HTMLResponse(app_html.read_text(encoding="utf-8"))
        return HTMLResponse(_FALLBACK_HTML)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    # ───────────────────────── Bootstrap / first-run ─────────────────────────

    @app.get("/api/bootstrap")
    async def api_bootstrap() -> JSONResponse:
        """Tell the frontend whether config exists so it can default to the
        config tab on first run."""
        from ..cli.setup_wizard import detect_state
        state = detect_state(root)
        return JSONResponse({
            "has_config": state.has_config,
            "has_skills": state.skill_dir_exists and state.skill_l0_exists,
            "has_memory": state.memory_db_exists,
            "is_partial": state.is_partial,
            "version": "0.1",
        })

    # ───────────────────────── Config read / write ───────────────────────────

    @app.get("/api/config")
    async def api_get_config() -> JSONResponse:
        from ..config import load_config
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

    @app.post("/api/config")
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
        from ..config import load_config as _load_cfg
        config_path = root / "config.toml"
        old_channels: dict[str, dict] = {}
        if config_path.exists():
            try:
                old_channels = dict(_load_cfg(config_path).channels or {})
            except Exception:
                old_channels = {}
        try:
            from ..cli.setup_wizard import _write_config, init_stores
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
            from ..runtime import trigger_reload
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
            from ..channels.supervisor import (
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

    # ───────────────────────── Sessions (chat history) ────────────────────────

    _SESSIONS_DIR = root / "logs" / "sessions"

    @app.get("/api/sessions")
    async def api_sessions_list(limit: int = 50) -> JSONResponse:
        import json as _json
        d = _SESSIONS_DIR
        out = []
        if not d.exists():
            return JSONResponse({"sessions": []})
        for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            preview = ""
            user_turns = 0
            total_lines = 0
            try:
                with p.open("r", encoding="utf-8") as f:
                    for raw in f:
                        total_lines += 1
                        if not raw.strip():
                            continue
                        try:
                            e = _json.loads(raw)
                        except Exception:
                            continue
                        if e.get("role") == "user":
                            user_turns += 1
                            if not preview and e.get("content"):
                                preview = str(e["content"])[:80]
            except OSError:
                continue
            # Filename scheme: `{source}-{uid}-{chat}-{ts}.jsonl` for
            # channel logs; bare session_id for web/CLI logs.
            stem = p.stem
            source = "web"
            for sfx in ("wechat", "feishu", "wecom", "telegram", "dingtalk"):
                if stem.startswith(sfx + "-"):
                    source = sfx
                    break
            out.append({
                "id": stem,
                "preview": preview or "(空)",
                "mtime": p.stat().st_mtime,
                "turns": user_turns,
                "lines": total_lines,
                "size": p.stat().st_size,
                "source": source,
            })
            if len(out) >= limit:
                break
        return JSONResponse({"sessions": out})

    @app.get("/api/sessions/{sid}")
    async def api_session_read(sid: str) -> JSONResponse:
        import json as _json
        p = _SESSIONS_DIR / f"{sid}.jsonl"
        try:
            p.resolve().relative_to(_SESSIONS_DIR.resolve())
        except ValueError:
            raise HTTPException(400, "path escapes sessions dir")
        if not p.exists():
            raise HTTPException(404, "not found")
        entries = []
        with p.open("r", encoding="utf-8") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    entries.append(_json.loads(raw))
                except Exception:
                    continue
        return JSONResponse({"id": sid, "entries": entries})

    @app.delete("/api/sessions/{sid}")
    async def api_session_delete(sid: str) -> JSONResponse:
        p = _SESSIONS_DIR / f"{sid}.jsonl"
        try:
            p.resolve().relative_to(_SESSIONS_DIR.resolve())
        except ValueError:
            raise HTTPException(400, "path escapes sessions dir")
        if not p.exists():
            raise HTTPException(404, "not found")
        p.unlink()
        return JSONResponse({"ok": True})

    # ───────────────────────── Memory status ─────────────────────────────────

    @app.get("/api/memory/status")
    async def api_memory_status() -> JSONResponse:
        from ..config import load_config
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            return JSONResponse({"initialized": False, "reason": "no config.toml"})
        skill_dir = (root / cfg.memory.skill_dir.lstrip("./")).resolve()
        db_path = (root / cfg.memory.memory_db.lstrip("./")).resolve()
        out = {
            "initialized": False,
            "skill_dir": str(skill_dir),
            "skill_dir_exists": skill_dir.exists(),
            "l0_exists": (skill_dir / "L0.md").exists(),
            "l1_exists": (skill_dir / "L1_index.txt").exists(),
            "l2_exists": (skill_dir / "L2_facts.txt").exists(),
            "l3_count": len(list((skill_dir / "L3").glob("*.md"))) if (skill_dir / "L3").exists() else 0,
            "memory_db": str(db_path),
            "memory_db_exists": db_path.exists(),
            "memory_db_size": db_path.stat().st_size if db_path.exists() else 0,
            "embed_provider": cfg.memory.embed_provider,
            "embed_model": cfg.memory.embed_model,
        }
        if db_path.exists():
            try:
                from ..stores.memory_store import MemoryStore
                ms = MemoryStore(db_path, embedder=None)
                st = ms.stats()
                ms.close()
                out.update(st)
            except Exception as e:
                out["db_error"] = str(e)
        out["initialized"] = (
            out["skill_dir_exists"] and out["l0_exists"]
            and out["l1_exists"] and out["memory_db_exists"]
        )
        return JSONResponse(out)

    @app.post("/api/memory/reseed")
    async def api_memory_reseed() -> JSONResponse:
        """Force-reseed sample SOPs + recreate L0/L1/L2 if missing."""
        from ..config import load_config
        from ..cli.setup_wizard import init_stores
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "先保存 config.toml")
        mem = {
            "skill_dir": cfg.memory.skill_dir,
            "memory_db": cfg.memory.memory_db,
        }
        return JSONResponse({"ok": True, "stores": init_stores(root, mem)})

    @app.post("/api/memory/embed_test")
    async def api_memory_embed_test() -> JSONResponse:
        """实例化当前 config 里配置的 embedder, embed 一段示例文本,
        返回是否连得通 + 维度 + 耗时。本地 sentence-transformers 第一次会下模型,
        所以默认时限 60s 由各 embedder 自带。"""
        import time
        from ..config import load_config
        from ..stores.embed import build_embedder
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "先保存 config.toml")
        mem_cfg = {
            "embed_provider": cfg.memory.embed_provider,
            "embed_model": cfg.memory.embed_model,
            "embed_base_url": cfg.memory.embed_base_url,
            "embed_api_key": cfg.memory.embed_api_key,
        }
        try:
            emb = build_embedder(mem_cfg)
        except Exception as e:
            return JSONResponse({"ok": False, "stage": "build",
                                 "provider": mem_cfg["embed_provider"],
                                 "error": f"{type(e).__name__}: {e}"})
        t0 = time.monotonic()
        try:
            # embed 是同步的(httpx / sentence-transformers 都是阻塞);
            # 扔到线程池别堵住 event loop。
            vecs = await asyncio.to_thread(emb.embed, ["bonsai 连接测试"])
        except Exception as e:
            return JSONResponse({"ok": False, "stage": "embed",
                                 "provider": mem_cfg["embed_provider"],
                                 "model": mem_cfg["embed_model"],
                                 "error": f"{type(e).__name__}: {e}",
                                 "latency_ms": int((time.monotonic() - t0) * 1000)})
        return JSONResponse({
            "ok": True,
            "provider": getattr(emb, "name", mem_cfg["embed_provider"]),
            "model": mem_cfg["embed_model"],
            "dim": len(vecs[0]) if vecs else 0,
            "latency_ms": int((time.monotonic() - t0) * 1000),
        })

    # ───────────────────────── Channels ──────────────────────────────────────

    @app.get("/api/channels/kinds")
    async def api_channel_kinds() -> JSONResponse:
        from ..channels.registry import KINDS
        return JSONResponse({"kinds": [
            {"kind": s.kind, "label": s.label, "fields": s.fields,
             "required": s.required, "login_mode": s.login_mode, "docs": s.docs}
            for s in KINDS.values()
        ]})

    @app.post("/api/channels/wechat/login_start")
    async def api_wechat_login_start() -> JSONResponse:
        from ..channels.wechat_client import WxBotClient
        bot = WxBotClient(root / "data" / "wechat_token.json")
        try:
            qr = bot.login_qr_start()
        except Exception as e:
            raise HTTPException(500, f"iLink 无法获取二维码: {e}")
        return JSONResponse({
            "qr_id": qr.qr_id,
            "qr_image_url": qr.qr_image_url,
            "qr_svg": _render_qr_svg(qr.qr_image_url),
        })

    @app.get("/api/channels/wechat/login_status")
    async def api_wechat_login_status(qr_id: str) -> JSONResponse:
        from ..channels.wechat_client import WxBotClient
        bot = WxBotClient(root / "data" / "wechat_token.json")
        try:
            cur = bot.login_qr_poll(qr_id)
        except Exception as e:
            raise HTTPException(500, f"轮询失败: {e}")
        # 扫码确认成功 → 必须停掉还在跑的旧 runner。它装着旧 bot_token 在内存里,
        # 新 token 在文件里,runner 不知道要切;结果是新号消息发过来 iLink 路由到
        # 新 bot,旧 runner 永远空转 → 用户看到「登录成功但没反应」。
        if cur.status == "confirmed":
            from ..channels.supervisor import stop as sv_stop, status as sv_status
            st = sv_status(root, "wechat")
            if st.get("running"):
                try:
                    sv_stop(root, "wechat")
                    log.info("stopped old wechat runner after new QR login (pid %s)",
                             st.get("pid"))
                except Exception as e:
                    log.warning("failed to stop old wechat runner: %s", e)
        return JSONResponse({
            "status": cur.status,
            "bot_id": cur.ilink_bot_id,
            "logged_in": bot.logged_in,
        })

    @app.get("/api/channels/wechat/status")
    async def api_wechat_status() -> JSONResponse:
        from ..channels.wechat_client import WxBotClient, WxToken
        tf = root / "data" / "wechat_token.json"
        t = WxToken.load(tf)
        return JSONResponse({
            "logged_in": bool(t.bot_token),
            "bot_id": t.ilink_bot_id,
            "login_time": t.login_time,
            "token_file": str(tf),
        })

    @app.post("/api/channels/wechat/logout")
    async def api_wechat_logout() -> JSONResponse:
        tf = root / "data" / "wechat_token.json"
        if tf.exists():
            tf.unlink()
        return JSONResponse({"ok": True})

    # ─────────── Runner lifecycle (supervisor) ───────────

    @app.get("/api/channels/{kind}/runner/status")
    async def api_runner_status(kind: str) -> JSONResponse:
        from ..channels.supervisor import status
        return JSONResponse(status(root, kind))

    @app.post("/api/channels/{kind}/runner/start")
    async def api_runner_start(kind: str, request: Request) -> JSONResponse:
        from ..channels.supervisor import start
        try:
            body = orjson.loads(await request.body() or b"{}")
        except Exception:
            body = {}
        allow = body.get("allow", "") or ""
        try:
            st = start(root, kind, allow=allow)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.exception("runner start failed")
            raise HTTPException(500, f"启动失败: {e}")
        return JSONResponse(st)

    @app.post("/api/channels/{kind}/runner/stop")
    async def api_runner_stop(kind: str) -> JSONResponse:
        from ..channels.supervisor import stop
        try:
            return JSONResponse(stop(root, kind))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/channels/{kind}/runner/log")
    async def api_runner_log(kind: str, lines: int = 200) -> JSONResponse:
        from ..channels.supervisor import log_tail
        return JSONResponse({"text": log_tail(root, kind, lines=lines)})

    @app.post("/api/channels/test")
    async def api_channel_test(request: Request) -> JSONResponse:
        body = orjson.loads(await request.body())
        kind = body.get("kind") or ""
        cfg = body.get("cfg") or {}
        # if any secret is the redaction marker, pull real value from disk
        _preserve_secrets_single(root, kind, cfg)
        from ..channels.registry import get_adapter
        try:
            adapter = get_adapter(kind)
        except KeyError as e:
            raise HTTPException(400, str(e))
        res = adapter.test(cfg)
        return JSONResponse({"ok": res.ok, "message": res.message})

    @app.post("/api/config/test")
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
        from ..cli.setup_wizard import ping_provider_by_kind
        ok, msg = ping_provider_by_kind(kind, api_key=api_key, base_url=base_url, model=model)
        return JSONResponse({"ok": ok, "message": msg})

    # ───────────────────────── Skills / Doctor ───────────────────────────────

    @app.get("/api/skills")
    async def api_skills() -> JSONResponse:
        from ..stores.skill_store import SkillStore
        try:
            from ..config import load_config
            cfg = load_config(root / "config.toml")
            skill_dir = Path(cfg.memory.skill_dir)
            if not skill_dir.is_absolute():
                skill_dir = root / skill_dir
        except Exception:
            skill_dir = root / "skills"
        if not skill_dir.exists():
            return JSONResponse({"skill_dir": str(skill_dir), "exists": False,
                                 "l1": "", "l2": "", "sops": []})
        store = SkillStore(skill_dir)
        sops = []
        for entry in store.list_sops():
            sops.append({
                "name": entry.name,
                "path": str(entry.path.relative_to(skill_dir)) if entry.path.is_relative_to(skill_dir) else str(entry.path),
                "keywords": entry.keywords,
                "created": entry.created,
                "verified_on": entry.verified_on,
            })
        return JSONResponse({
            "skill_dir": str(skill_dir),
            "exists": True,
            "l1": store.l1_text(max_chars=4000),
            "l2": store.l2_text(max_chars=1000),
            "sops": sops,
        })

    def _resolve_skill_dir() -> Path:
        try:
            from ..config import load_config
            cfg = load_config(root / "config.toml")
            sd = Path(cfg.memory.skill_dir)
            return sd if sd.is_absolute() else (root / sd)
        except Exception:
            return root / "skills"

    def _safe_skill_target(path: str) -> Path:
        skill_dir = _resolve_skill_dir()
        target = (skill_dir / path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            raise HTTPException(400, "path escapes skill_dir")
        return target

    @app.get("/api/skills/read")
    async def api_skill_read(path: str) -> JSONResponse:
        target = _safe_skill_target(path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "not found")
        return JSONResponse({"path": path, "content": target.read_text(encoding="utf-8")})

    @app.post("/api/skills/write")
    async def api_skill_write(request: Request) -> JSONResponse:
        """Create or overwrite an L3 SOP. Body: {name, keywords (list|str), content}
        - `name` lower-snake,<40 chars
        - content 可以自带 frontmatter;没带就用 name+keywords 自动生成
        - 落到 <skill_dir>/L3/<name>.md,然后 rebuild L1 索引
        """
        import re as _re
        import time as _time
        body = orjson.loads(await request.body())
        name = (body.get("name") or "").strip()
        if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}", name):
            raise HTTPException(400, "name 必须是字母/数字/下划线/连字符,1-40 字符")
        kws = body.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(",") if k.strip()]
        content = body.get("content") or ""
        if not content.strip():
            raise HTTPException(400, "content 不能为空")
        skill_dir = _resolve_skill_dir()
        l3_dir = skill_dir / "L3"
        l3_dir.mkdir(parents=True, exist_ok=True)
        target = l3_dir / f"{name}.md"
        if not content.lstrip().startswith("---"):
            today = _time.strftime("%Y-%m-%d")
            frontmatter = (
                f"---\nname: {name}\nkeywords: {kws}\n"
                f"created: {today}\nverified_on: {today}\nsource: manual\n---\n\n"
            )
            content = frontmatter + content.lstrip()
        target.write_text(content, encoding="utf-8")
        # Rebuild L1 index so lookup() sees the new entry.
        try:
            from ..stores.skill_store import SkillStore
            SkillStore(skill_dir)._rebuild_l1()
        except Exception as e:
            log.warning("rebuild L1 failed: %s", e)
        return JSONResponse({"ok": True,
                              "path": str(target.relative_to(skill_dir)),
                              "overwritten": target.stat().st_size > 0})

    @app.delete("/api/skills/delete")
    async def api_skill_delete(path: str) -> JSONResponse:
        """Delete one L3 SOP. `path` is relative to skill_dir (e.g. 'L3/foo.md')."""
        target = _safe_skill_target(path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "not found")
        # Refuse to delete anything outside L3/ — evidence, L0, L1/L2 indexes are off-limits.
        skill_dir = _resolve_skill_dir()
        if target.parent != (skill_dir / "L3").resolve():
            raise HTTPException(400, "只允许删除 L3/ 下的 SOP")
        target.unlink()
        try:
            from ..stores.skill_store import SkillStore
            SkillStore(skill_dir)._rebuild_l1()
        except Exception as e:
            log.warning("rebuild L1 failed: %s", e)
        return JSONResponse({"ok": True})

    @app.get("/api/doctor")
    async def api_doctor() -> JSONResponse:
        from ..cli.doctor import collect_checks
        try:
            checks = collect_checks(root)
        except Exception as e:
            log.exception("doctor failed")
            return JSONResponse({"error": str(e), "checks": []}, status_code=200)
        return JSONResponse({
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                for c in checks
            ],
        })

    # ───────────────────────── Usage stats (数据中心) ──────────────────────────

    def _stats_log_path() -> Path:
        from ..config import load_config
        try:
            cfg = load_config(root / "config.toml")
            return root / cfg.logging.cache_stats.lstrip("./")
        except Exception:
            return root / "logs" / "cache_stats.jsonl"

    @app.get("/api/stats/usage")
    async def api_stats_usage(days: int = 14) -> JSONResponse:
        from ..stats import load_usage, report_to_dict
        r = load_usage(_stats_log_path(), window_days=max(1, min(days, 60)))
        return JSONResponse(report_to_dict(r))

    @app.get("/api/stats/today")
    async def api_stats_today() -> JSONResponse:
        from ..stats import load_today
        return JSONResponse(load_today(_stats_log_path()))

    @app.get("/api/stats/hourly")
    async def api_stats_hourly(date: str | None = None) -> JSONResponse:
        from ..stats import load_hourly
        return JSONResponse(load_hourly(_stats_log_path(), date=date))

    @app.get("/api/stats/weekly")
    async def api_stats_weekly(weeks: int = 8) -> JSONResponse:
        from ..stats import load_weekly
        return JSONResponse(load_weekly(_stats_log_path(),
                                          weeks=max(1, min(weeks, 52))))

    @app.get("/api/stats/monthly")
    async def api_stats_monthly() -> JSONResponse:
        from ..stats import load_monthly_compare
        return JSONResponse(load_monthly_compare(_stats_log_path()))

    @app.get("/api/stats/hit-rate-trend")
    async def api_stats_hit_rate(days: int = 14) -> JSONResponse:
        from ..stats import hit_rate_trend
        return JSONResponse({"trend":
            hit_rate_trend(_stats_log_path(), days=max(1, min(days, 60)))})

    @app.get("/api/stats/anomalies")
    async def api_stats_anomalies(days: int = 14) -> JSONResponse:
        from ..stats import detect_anomalies
        return JSONResponse({"anomalies":
            detect_anomalies(_stats_log_path(), days=max(3, min(days, 60)))})

    @app.get("/api/stats/export.csv")
    async def api_stats_export(days: int = 30) -> "Response":
        from fastapi.responses import Response
        from ..stats import export_csv
        body = export_csv(_stats_log_path(), days=max(1, min(days, 365)))
        return Response(
            content=body, media_type="text/csv; charset=utf-8",
            headers={
                "content-disposition":
                    f"attachment; filename=bonsai-usage-{days}d.csv",
            },
        )

    # ───────────────────────── Autonomous workspace ───────────────────────────

    @app.get("/api/autonomous/state")
    async def api_auto_state() -> JSONResponse:
        from ..autonomous import AutonomousWorkspace
        w = AutonomousWorkspace(root)
        return JSONResponse({
            "initialized": w.initialized,
            "dir": str(w.dir),
            "todo": w.get_todo(),
            "history": w.get_history(30),
            "reports": w.list_reports(),
        })

    @app.post("/api/autonomous/init")
    async def api_auto_init(request: Request) -> JSONResponse:
        from ..autonomous import AutonomousWorkspace
        try:
            body = orjson.loads(await request.body() or b"{}")
        except Exception:
            body = {}
        w = AutonomousWorkspace(root)
        w.init(overwrite=bool(body.get("overwrite")))
        return JSONResponse({"ok": True, "dir": str(w.dir)})

    @app.post("/api/autonomous/todo")
    async def api_auto_todo_save(request: Request) -> JSONResponse:
        from ..autonomous import AutonomousWorkspace
        body = orjson.loads(await request.body())
        w = AutonomousWorkspace(root)
        w.set_todo(body.get("text") or "")
        return JSONResponse({"ok": True})

    @app.get("/api/autonomous/reports/{fname}")
    async def api_auto_report(fname: str) -> JSONResponse:
        from ..autonomous import AutonomousWorkspace
        w = AutonomousWorkspace(root)
        try:
            content = w.read_report(fname)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, str(e))
        return JSONResponse({"file": fname, "content": content})

    # ───────────────────────── Scheduler ──────────────────────────────────────

    @app.get("/api/scheduler/tasks")
    async def api_sched_list() -> JSONResponse:
        from ..scheduler import list_tasks, _last_run
        from dataclasses import asdict
        out = []
        for t in list_tasks(root):
            d = asdict(t)
            last = _last_run(root, t.name)
            d["last_run"] = last.isoformat(timespec="minutes") if last else None
            out.append(d)
        return JSONResponse({"tasks": out})

    @app.post("/api/scheduler/tasks")
    async def api_sched_save(request: Request) -> JSONResponse:
        from ..scheduler import Task, save_task
        body = orjson.loads(await request.body())
        try:
            task = Task(
                name=(body.get("name") or "").strip(),
                schedule=(body.get("schedule") or "").strip(),
                prompt=body.get("prompt") or "",
                repeat=body.get("repeat") or "daily",
                enabled=bool(body.get("enabled", True)),
                max_delay_hours=int(body.get("max_delay_hours", 6)),
            )
            path = save_task(root, task)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"ok": True, "path": str(path)})

    @app.delete("/api/scheduler/tasks/{name}")
    async def api_sched_delete(name: str) -> JSONResponse:
        from ..scheduler import delete_task
        if not delete_task(root, name):
            raise HTTPException(404, "不存在")
        return JSONResponse({"ok": True})

    @app.post("/api/scheduler/tasks/{name}/run")
    async def api_sched_run_now(name: str) -> JSONResponse:
        """Fire the task right now, out of schedule."""
        from ..scheduler import list_tasks, run_once
        from ..config import load_config
        task = next((t for t in list_tasks(root) if t.name == name), None)
        if task is None:
            raise HTTPException(404, "不存在")
        try:
            cfg = load_config(root / "config.toml")
        except FileNotFoundError:
            raise HTTPException(400, "config.toml 缺失")
        try:
            path = await run_once(root, task, cfg)
        except Exception as e:
            log.exception("manual run failed")
            raise HTTPException(500, f"运行失败: {e}")
        return JSONResponse({"ok": True, "report": path.name})

    @app.get("/api/scheduler/reports")
    async def api_sched_reports(task: str | None = None, limit: int = 50) -> JSONResponse:
        from ..scheduler import list_reports
        return JSONResponse({"reports": list_reports(root, task_name=task, limit=limit)})

    @app.get("/api/scheduler/reports/{fname}")
    async def api_sched_report_read(fname: str) -> JSONResponse:
        from ..scheduler import reports_dir
        p = (reports_dir(root) / fname).resolve()
        if not str(p).startswith(str(reports_dir(root).resolve())) or not p.exists():
            raise HTTPException(404, "not found")
        return JSONResponse({"file": fname, "content": p.read_text(encoding="utf-8")})

    # ───────────────────────── WebSocket chat ────────────────────────────────

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        session_ctx = None
        pending_prompt: asyncio.Future | None = None
        run_task: asyncio.Task | None = None

        async def prompt_user(question: str, candidates: list[str] | None) -> str:
            nonlocal pending_prompt
            pending_prompt = asyncio.get_event_loop().create_future()
            await sock.send_bytes(orjson.dumps({
                "kind": "ask_user",
                "question": question,
                "candidates": candidates or [],
            }))
            return await pending_prompt

        async def drive(loop_obj) -> None:
            try:
                async for ev in loop_obj.run():
                    data = _event_to_wire(ev)
                    await sock.send_bytes(orjson.dumps(data))
            except asyncio.CancelledError:
                # 用户按了停止 / WS 断了。给前端一个收尾事件再退出。
                try:
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "text", "data": "\n[已被用户中止]"}))
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "done", "data": {"reason": "stopped"}}))
                except Exception:
                    pass
                raise
            except Exception as e:
                log.exception("agent loop crashed: %s", e)
                try:
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "error", "data": str(e)}))
                except Exception:
                    pass

        try:
            session_ctx = chat_factory(prompt_user)
            # 一个 ws 连接 = 一次对话。AgentLoop 复用 → self.tail.messages 跨
            # turn 累积上下文。之前每次 user turn 新建 loop → 每次 tail 都是空 →
            # agent 失忆。
            loop = session_ctx.new_loop()
            while True:
                raw = await sock.receive_text()
                msg = orjson.loads(raw)
                kind = msg.get("kind")
                if kind == "user":
                    if run_task and not run_task.done():
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "error", "data": "上一轮还在跑,请先停止"}))
                        continue
                    loop.add_user(msg.get("text", ""))
                    run_task = asyncio.create_task(drive(loop))
                elif kind == "stop":
                    if run_task and not run_task.done():
                        run_task.cancel()
                elif kind == "resume":
                    # 切到某条历史会话继续聊。把旧 .jsonl 的消息塞进新 loop 的
                    # tail,SessionLog 改成 append 到同一个文件。
                    sid = msg.get("sid", "")
                    try:
                        pre = session_ctx.resume(sid)
                        loop = session_ctx.new_loop(pre_messages=pre)
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "resumed", "sid": sid, "turns": len(pre)}))
                    except Exception as e:
                        await sock.send_bytes(orjson.dumps(
                            {"kind": "error", "data": f"resume failed: {e}"}))
                elif kind == "new":
                    # 开新会话 — 换 session_id + 换新 log 文件 + 清空 tail。
                    session_ctx.reset()
                    loop = session_ctx.new_loop()
                    await sock.send_bytes(orjson.dumps(
                        {"kind": "new_session", "sid": session_ctx.session_id}))
                elif kind == "ask_user_reply" and pending_prompt is not None:
                    pending_prompt.set_result(msg.get("reply", ""))
                    pending_prompt = None
        except WebSocketDisconnect:
            log.info("ws disconnected")
        except Exception as e:
            log.exception("ws error: %s", e)
            try:
                await sock.send_bytes(orjson.dumps({"kind": "error", "data": str(e)}))
            except Exception:
                pass
        finally:
            if run_task and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except BaseException:
                    pass
            if session_ctx:
                session_ctx.cleanup()

    return app


_REDACT_MARK = "__KEEP__"


def _autostart_channels(root: Path) -> None:
    """On server startup, spawn runners for channels that opt-in via
    `autostart = true` in their TOML section. Only runs for channels whose
    bind is actually valid (wechat must be logged in)."""
    try:
        from ..config import load_config
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    from ..channels.supervisor import start as sv_start, status as sv_status
    from ..channels.wechat_client import WxToken
    for kind, ccfg in (cfg.channels or {}).items():
        if not isinstance(ccfg, dict) or not ccfg.get("enabled") or not ccfg.get("autostart"):
            continue
        if kind == "wechat":
            tf = root / "data" / "wechat_token.json"
            if not WxToken.load(tf).bot_token:
                log.info("autostart wechat skipped: not logged in")
                continue
            st = sv_status(root, kind)
            if st.get("running"):
                log.info("wechat runner already running (pid %s)", st.get("pid"))
                continue
            allow = ccfg.get("allowed_users") or ""
            try:
                new_st = sv_start(root, kind, allow=allow)
                log.info("autostart wechat ok: pid=%s", new_st.get("pid"))
            except Exception as e:
                log.error("autostart wechat failed: %s", e)
        # other kinds: runtime not yet implemented, skip

_INVISIBLE_CHARS = "﻿​‌‍   "


def _scrub(s):
    """Strip BOM / zero-width / NBSP / etc. from a string value."""
    if not isinstance(s, str):
        return s
    s = s.strip()
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    return s


def _scrub_invisibles(providers: list, memory_cfg: dict, channels_in: dict) -> None:
    """Defensive cleanup on save — users paste keys from GUIs that include
    invisible junk, which later breaks httpx header encoding. Also
    normalizes base_url so users who paste the full endpoint (`.../v1/
    embeddings`, `.../v1/chat/completions`) don't get doubled paths."""
    from ..cli.setup_wizard import normalize_base_url
    for p in providers:
        for k in ("name", "kind", "model", "base_url", "api_key"):
            if k in p and isinstance(p[k], str):
                p[k] = _scrub(p[k])
        if p.get("base_url"):
            p["base_url"] = normalize_base_url(p["base_url"], kind=p.get("kind", "openai"))
    for k in ("skill_dir", "memory_db", "embed_provider", "embed_model",
              "embed_base_url", "embed_api_key"):
        if k in memory_cfg and isinstance(memory_cfg[k], str):
            memory_cfg[k] = _scrub(memory_cfg[k])
    if memory_cfg.get("embed_base_url"):
        memory_cfg["embed_base_url"] = normalize_base_url(memory_cfg["embed_base_url"])
    for cfg in channels_in.values():
        if not isinstance(cfg, dict):
            continue
        for k, v in list(cfg.items()):
            if isinstance(v, str) and not k.startswith("_"):
                cfg[k] = _scrub(v)


def _render_qr_svg(data: str) -> str:
    """Best-effort server-side QR → SVG. Returns empty string if qrcode
    isn't installed so the frontend can fall back to showing the raw URL."""
    if not data:
        return ""
    try:
        import io
        import qrcode
        import qrcode.image.svg as svg
        img = qrcode.make(data, image_factory=svg.SvgPathImage, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception as e:
        log.info("qrcode svg render skipped: %s", e)
        return ""


def _redact_channel(cfg: dict) -> dict:
    """Secrets get redacted; UI sends back __KEEP__ if untouched so the
    server knows to reuse the on-disk value."""
    out = {}
    for k, v in (cfg or {}).items():
        if any(s in k for s in ("secret", "token", "api_key")) and isinstance(v, str) and v:
            out[k] = ("••••" + v[-4:]) if len(v) > 4 else "••••"
            out[f"{k}_set"] = True
        else:
            out[k] = v
    return out


def _channel_creds_changed(old: dict, new: dict) -> bool:
    """Return True if any non-internal field differs between old and new.
    Triggers auto-restart of a running runner so credential edits land
    without the user manually clicking stop+start."""
    keys = (set(old.keys()) | set(new.keys()))
    for k in keys:
        if k.startswith("_"):
            continue
        if old.get(k) != new.get(k):
            return True
    return False


def _preserve_secrets(root, channels_in: dict) -> None:
    """Channels is a dict[kind -> cfg]. Replace redaction markers / empty
    secret fields with the on-disk value so saves don't wipe credentials."""
    from ..config import load_config
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    existing = cfg.channels or {}
    for kind, new_cfg in channels_in.items():
        _merge_one(existing.get(kind) or {}, new_cfg)


def _resolve_real_api_key(root, *, name: str, base_url: str, model: str,
                           provided: str) -> str:
    """UI 送来的 api_key 可能是:
      - 真 key (用户刚粘贴) → 直接用
      - "••••xxxx" 脱敏串 → 从 config.toml 按 name 回填
      - "$ref:env:ANTHROPIC_API_KEY" → 读环境变量
    回填失败返回空串, 调用方显示友好错误。
    """
    import os
    from ..config import load_config

    # 情况 1: 真 key
    if provided and not provided.startswith("••••") and provided != _REDACT_MARK \
            and not provided.startswith("$ref:"):
        return provided

    # 情况 2+3: 拉 config 里真值(load_config 已把 $ref:env:X 解析成真字符串)
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return ""
    match = None
    for p in (cfg.providers or []):
        if name and p.get("name") == name:
            match = p; break
        if not name and p.get("base_url") == base_url and p.get("model") == model:
            match = p; break
    if match is None:
        return ""
    real = match.get("api_key") or ""
    # 双保险: 万一 $ref: 漏解, 这里兜底一次。
    if isinstance(real, str) and real.startswith("$ref:env:"):
        real = os.getenv(real.split(":", 2)[2], "")
    return real


def _preserve_provider_secrets(root, providers: list) -> None:
    """UI 编辑现有 provider 时,api_key 字段是脱敏形态 "••••xxxx"。用户若没
    重新敲 key,前端原样把它发回来。不做这一步的话这个字面量就会被写进
    config.toml,下次启动拿 "••••xxxx" 去调 LLM 直接 401 —— 看起来就像
    「密钥失效」。按 name 匹配回填磁盘上的真值。"""
    from ..config import load_config
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    by_name = {p.get("name"): p for p in (cfg.providers or []) if p.get("name")}
    for p in providers:
        key = p.get("api_key") or ""
        if isinstance(key, str) and (key.startswith("••••") or key == _REDACT_MARK):
            existing = by_name.get(p.get("name"))
            if existing:
                p["api_key"] = existing.get("api_key", "")


def _preserve_memory_secrets(root, mem: dict) -> None:
    """If embed_api_key is absent and an existing non-empty value is on disk,
    keep the existing one — the UI doesn't re-send secrets by default."""
    from ..config import load_config
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    if not mem.get("embed_api_key") and cfg.memory.embed_api_key:
        mem["embed_api_key"] = cfg.memory.embed_api_key


def _preserve_secrets_single(root, kind: str, cfg_in: dict) -> None:
    from ..config import load_config
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    existing = (cfg.channels or {}).get(kind) or {}
    _merge_one(existing, cfg_in)


def _merge_one(existing: dict, new_cfg: dict) -> None:
    for k, v in list(new_cfg.items()):
        is_secret = any(s in k for s in ("secret", "token", "api_key"))
        if not is_secret:
            continue
        # user left it redacted → keep existing
        if isinstance(v, str) and (v.startswith("••••") or v == _REDACT_MARK):
            new_cfg[k] = existing.get(k, "")


def _redact_provider(p: dict) -> dict:
    key = (p.get("api_key") or "")
    # Preserve $ref:… references intact; redact inline secrets to last 4 chars.
    if key.startswith("$ref:"):
        redacted = key
        has_secret = True
    elif key:
        redacted = ("••••" + key[-4:]) if len(key) > 4 else "••••"
        has_secret = True
    else:
        redacted = ""
        has_secret = False
    return {
        "name": p.get("name", ""),
        "kind": p.get("kind", ""),
        "model": p.get("model", ""),
        "base_url": p.get("base_url", ""),
        "api_key": redacted,
        "api_key_set": has_secret,
        "max_tokens": p.get("max_tokens"),
    }


def _event_to_wire(ev) -> dict:
    from ..core.types import StreamEvent, ToolCall, Usage
    assert isinstance(ev, StreamEvent)
    if ev.kind == "text":
        return {"kind": "text", "data": ev.data}
    if ev.kind == "tool_call":
        tc: ToolCall = ev.data
        return {"kind": "tool_call", "data": {"id": tc.id, "name": tc.name, "args": tc.args}}
    if ev.kind == "usage":
        u: Usage = ev.data
        return {"kind": "usage", "data": {
            "in": u.input_tokens, "out": u.output_tokens,
            "cache_read": u.cache_read_tokens, "cache_create": u.cache_creation_tokens,
        }}
    if ev.kind == "done":
        return {"kind": "done", "data": ev.data}
    if ev.kind == "error":
        return {"kind": "error", "data": str(ev.data)}
    return {"kind": ev.kind, "data": ev.data}


_FALLBACK_HTML = """<!doctype html><html><head><title>Bonsai</title></head>
<body><h1>Bonsai Web</h1>
<p>assets/app.html missing. Reinstall or run from the source checkout.</p>
</body></html>"""
