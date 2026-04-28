"""Shared helpers for the web routes — secret preservation, redaction,
event serialization, etc. Originally inline in web.py before the route
split."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


_REDACT_MARK = "__KEEP__"

_INVISIBLE_CHARS = "﻿​‌‍   "


_FALLBACK_HTML = """<!doctype html><html><head><title>Bonsai</title></head>
<body><h1>Bonsai Web</h1>
<p>assets/app.html missing. Reinstall or run from the source checkout.</p>
</body></html>"""


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
    from ...cli.setup_wizard import normalize_base_url
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
    from ...config import load_config
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
    from ...config import load_config

    if provided and not provided.startswith("••••") and provided != _REDACT_MARK \
            and not provided.startswith("$ref:"):
        return provided

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
    if isinstance(real, str) and real.startswith("$ref:env:"):
        real = os.getenv(real.split(":", 2)[2], "")
    return real


def _preserve_provider_secrets(root, providers: list) -> None:
    """UI 编辑现有 provider 时,api_key 字段是脱敏形态 "••••xxxx"。
    用户若没重新敲 key,前端原样把它发回来。不做这一步的话这个字面量就会被写进
    config.toml,下次启动拿 "••••xxxx" 去调 LLM 直接 401 ——
    看起来就像「密钥失效」。按 name 匹配回填磁盘上的真值。"""
    from ...config import load_config
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
    from ...config import load_config
    try:
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    if not mem.get("embed_api_key") and cfg.memory.embed_api_key:
        mem["embed_api_key"] = cfg.memory.embed_api_key


def _preserve_secrets_single(root, kind: str, cfg_in: dict) -> None:
    from ...config import load_config
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
        if isinstance(v, str) and (v.startswith("••••") or v == _REDACT_MARK):
            new_cfg[k] = existing.get(k, "")


def _redact_provider(p: dict) -> dict:
    key = (p.get("api_key") or "")
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
    from ...core.types import StreamEvent, ToolCall, Usage
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


def _autostart_channels(root: Path) -> None:
    """On server startup, spawn runners for channels that opt-in via
    `autostart = true` in their TOML section. Only runs for channels whose
    bind is actually valid (wechat must be logged in)."""
    try:
        from ...config import load_config
        cfg = load_config(root / "config.toml")
    except FileNotFoundError:
        return
    from ...channels.supervisor import start as sv_start, status as sv_status
    from ...channels.wechat_client import WxToken
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
