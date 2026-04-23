"""bonsai doctor — pre-flight self-check.

Walks the full stack and reports ✓ / ⚠ / ✗ for each piece with a concrete
remediation hint. Any ✗ exits non-zero so this can also gate CI / scripts.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

log = logging.getLogger(__name__)
console = Console()


@dataclass
class Check:
    name: str
    status: str   # "pass" | "warn" | "fail"
    detail: str
    hint: str = ""

    def icon(self) -> str:
        return {"pass": "[green]✓[/green]",
                "warn": "[yellow]⚠[/yellow]",
                "fail": "[red]✗[/red]"}[self.status]


def run_doctor(root: Path) -> int:
    """CLI entry: collect checks, print table, return POSIX exit code."""
    results = collect_checks(root)
    _print(results)
    fails = [r for r in results if r.status == "fail"]
    return 0 if not fails else 1


def collect_checks(root: Path) -> list[Check]:
    """Run every health check and return the result list. No I/O side-effects
    besides probing providers / embedders / DB. Safe to call from web API."""
    from ..cli.setup_wizard import (
        detect_state,
        ping_embedder,
        ping_provider_by_kind,
    )
    from ..config import load_config

    results: list[Check] = []

    # 1. Python version
    ver = sys.version_info
    if ver >= (3, 10):
        results.append(Check("Python version", "pass", f"{ver.major}.{ver.minor}.{ver.micro}"))
    else:
        results.append(Check("Python version", "fail",
                             f"{ver.major}.{ver.minor} (need 3.10+)",
                             hint="升级 Python 到 3.10 或更高"))

    # 2. Core deps
    for dep in ["httpx", "orjson", "typer", "rich", "pydantic"]:
        try:
            __import__(dep)
            results.append(Check(f"dep: {dep}", "pass", "installed"))
        except ImportError:
            results.append(Check(f"dep: {dep}", "fail", "missing",
                                  hint=f"pip install {dep}"))

    # 3. Config exists and parseable
    cfg_path = root / "config.toml"
    cfg = None
    if not cfg_path.exists():
        results.append(Check("config.toml", "fail", f"not found at {cfg_path}",
                             hint="运行 [cyan]bonsai setup[/cyan] 走一遍向导"))
        return results
    try:
        cfg = load_config(cfg_path)
        results.append(Check("config.toml", "pass", str(cfg_path)))
    except Exception as e:
        results.append(Check("config.toml", "fail", str(e),
                             hint="删掉 config.toml 重跑 `bonsai setup`"))
        return results

    # 4. Providers (test each — skip if key is $ref unresolved)
    if not cfg.providers:
        results.append(Check("providers", "fail", "no providers configured",
                             hint="运行 `bonsai setup` 至少配 1 个 provider"))
    else:
        for p in cfg.providers:
            name = p["name"]
            key = p.get("api_key") or ""
            if not key:
                results.append(Check(f"provider · {name}", "warn",
                                      "api_key 为空", hint="跑 `bonsai setup` 补上"))
                continue
            if key.startswith("$ref:"):
                results.append(Check(f"provider · {name}", "warn",
                                      f"未解析引用: {key}",
                                      hint="检查对应 env 是否设置"))
                continue
            ok, msg = ping_provider_by_kind(
                p["kind"], api_key=key, base_url=p["base_url"], model=p["model"],
            )
            if ok:
                results.append(Check(f"provider · {name}", "pass",
                                      f"{p['kind']} / {p['model']} · {msg}"))
            else:
                results.append(Check(f"provider · {name}", "fail", msg,
                                      hint="检查 key / 余额 / 网络"))

    # 5. Embedder
    mc = cfg.memory
    if mc.embed_provider == "hash":
        results.append(Check("embedder", "warn",
                              "hash (召回质量差)",
                              hint="`bonsai setup` 选 bge-m3 或其它真 embedder"))
    elif mc.embed_provider == "openai":
        key = mc.embed_api_key
        if not key or key.startswith("$ref:"):
            results.append(Check("embedder", "warn", f"key 未配置: {key}"))
        else:
            ok, msg = ping_embedder(key, mc.embed_base_url, mc.embed_model)
            if ok:
                results.append(Check("embedder", "pass",
                                      f"{mc.embed_model} · {msg}"))
            else:
                results.append(Check("embedder", "fail", msg,
                                      hint="检查 embedder key / 网络"))
    elif mc.embed_provider == "local":
        try:
            __import__("sentence_transformers")
            results.append(Check("embedder", "pass",
                                  f"local · {mc.embed_model}"))
        except ImportError:
            results.append(Check("embedder", "fail",
                                  "sentence-transformers 未装",
                                  hint="pip install sentence-transformers"))

    # 6. SkillStore
    state = detect_state(root)
    if state.skill_dir_exists and state.skill_l0_exists and state.skill_l1_exists:
        results.append(Check("SkillStore", "pass",
                              f"{cfg.memory.skill_dir} · "
                              f"L0 + L1 OK"))
    else:
        missing = []
        if not state.skill_dir_exists: missing.append("directory")
        if not state.skill_l0_exists: missing.append("L0.md")
        if not state.skill_l1_exists: missing.append("L1_index.txt")
        results.append(Check("SkillStore", "warn",
                              f"missing: {', '.join(missing)}",
                              hint="`bonsai setup` 会自动修补"))

    if state.sample_sops_present:
        results.append(Check("sample SOPs", "pass",
                              f"{cfg.memory.skill_dir}/L3"))
    else:
        results.append(Check("sample SOPs", "warn",
                              "no SOPs yet",
                              hint="`bonsai setup` 种 1-2 个样例,或写你自己的"))

    # 7. MemoryStore
    try:
        from ..stores.memory_store import MemoryStore
        db_path = (root / cfg.memory.memory_db.lstrip("./")).resolve()
        store = MemoryStore(db_path, embedder=None)
        stats = store.stats()
        store.close()
        results.append(Check("MemoryStore", "pass",
                              f"{db_path.name} · "
                              f"{stats['drawers']} drawers / "
                              f"{stats['rooms']} rooms / "
                              f"{stats['vectors']} vectors"))
    except Exception as e:
        results.append(Check("MemoryStore", "fail", str(e),
                              hint="`bonsai setup` 重建"))

    # 8. Channels — only check enabled ones; silent on unconfigured
    from ..channels.registry import KINDS, is_configured, get_adapter
    for kind, spec in KINDS.items():
        ccfg = (cfg.channels or {}).get(kind) or {}
        if not ccfg.get("enabled"):
            continue
        if spec.login_mode == "qr":
            # QR-based channels (wechat): "configured" = token file exists
            from ..channels.wechat_client import WxToken
            tf = root / "data" / "wechat_token.json"
            t = WxToken.load(tf)
            if t.bot_token:
                results.append(Check(f"channel · {spec.label}", "pass",
                                      f"已登录 · bot_id={t.ilink_bot_id or '?'} · {t.login_time}"))
            else:
                results.append(Check(f"channel · {spec.label}", "warn",
                                      "已启用但未登录",
                                      hint="在 web 配置页点击 '扫码登录',或删除 data/wechat_token.json 重登"))
            continue
        if not is_configured(kind, ccfg):
            missing = [k for k in spec.required if not (ccfg.get(k) or "").strip()]
            results.append(Check(f"channel · {spec.label}", "warn",
                                  f"已启用但缺: {', '.join(missing)}",
                                  hint="在 web 配置页面补齐,或直接编辑 config.toml"))
            continue
        try:
            res = get_adapter(kind).test(ccfg)
            if res.ok:
                results.append(Check(f"channel · {spec.label}", "pass", res.message))
            else:
                results.append(Check(f"channel · {spec.label}", "fail",
                                      res.message,
                                      hint="检查凭据 / 应用权限 / 网络"))
        except Exception as e:
            results.append(Check(f"channel · {spec.label}", "fail", str(e)))

    # 9. Tools schema
    schema = root / "tools" / "schema.json"
    if schema.exists():
        results.append(Check("tools/schema.json", "pass", str(schema)))
    else:
        results.append(Check("tools/schema.json", "fail",
                              "missing",
                              hint="项目可能不完整,重新 clone"))

    # 9. System prompt
    sp = root / "prompts" / "system.txt"
    if sp.exists():
        results.append(Check("prompts/system.txt", "pass", str(sp)))
    else:
        results.append(Check("prompts/system.txt", "warn",
                              "missing — will use fallback"))

    return results


def _print(results: list[Check]) -> None:
    t = Table(title="bonsai doctor", show_lines=False)
    t.add_column("", no_wrap=True)
    t.add_column("check")
    t.add_column("detail")
    t.add_column("hint", style="dim")
    for r in results:
        t.add_row(r.icon(), r.name, r.detail, r.hint)
    console.print(t)

    passed = sum(1 for r in results if r.status == "pass")
    warned = sum(1 for r in results if r.status == "warn")
    failed = sum(1 for r in results if r.status == "fail")
    style = "red" if failed else ("yellow" if warned else "green")
    console.print(
        f"[{style}]{passed} passed · {warned} warn · {failed} fail[/{style}]"
    )
    if failed:
        console.print("\n[red]有 fail 项,先修再 chat。[/red]")
    elif warned:
        console.print("\n[yellow]能 chat,但有警告项建议处理。[/yellow]")
    else:
        console.print("\n[green]所有检查通过 — 运行 [bold]bonsai chat[/bold] 开聊。[/green]")
