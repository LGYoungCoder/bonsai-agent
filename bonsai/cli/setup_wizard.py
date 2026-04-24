"""Interactive setup wizard.

Design goals:
  1. Idempotent — running it again on a configured project must not destroy data
  2. State-aware — reads existing config & stores, prefills sensible defaults
  3. Partial-init aware — detects half-initialized state and offers repair
  4. Fail-fast — validate keys via real API ping before writing config

Flow:
  Step 1  Detect state  (existing config? stores initialized?)
  Step 2  Providers     (add / edit / delete / keep)
  Step 3  Memory + embedder  (toggle on/off, pick embedder)
  Step 4  Initialize stores  (create or repair SkillStore/MemoryStore)
  Step 5  Write config.toml (preserving existing values where user chose keep)
  Step 6  Summary + next-step hint
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)
console = Console()


# ───────────────────────── provider presets ──────────────────────────

@dataclass
class ProviderPreset:
    label: str
    kind: str
    default_model: str
    default_base_url: str
    hint: str
    env_hint: str = ""

    def prompt_extra(self) -> dict[str, Any]:
        return {}


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "glm-anthropic": ProviderPreset(
        label="GLM (智谱, Anthropic 原生格式)",
        kind="claude",
        default_model="glm-5",
        default_base_url="https://open.bigmodel.cn/api/anthropic",
        hint="国内可直连,tool use 稳定,不走 OpenAI 兼容层",
        env_hint="在 https://open.bigmodel.cn 注册 → 取 key",
    ),
    "glm": ProviderPreset(
        label="GLM (智谱, OpenAI 兼容)",
        kind="glm",
        default_model="glm-4.6",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        hint="OpenAI 兼容端点,所有 OpenAI SDK 能用",
        env_hint="在 https://open.bigmodel.cn 注册 → 取 key",
    ),
    "qwen": ProviderPreset(
        label="通义千问 (阿里 DashScope)",
        kind="qwen",
        default_model="qwen3-max",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        hint="阿里云,免费额度,中文强",
        env_hint="在 https://dashscope.console.aliyun.com 创建 key",
    ),
    "deepseek": ProviderPreset(
        label="DeepSeek (V4 / V3 / reasoner)",
        kind="deepseek",
        default_model="deepseek-v4-flash",
        default_base_url="https://api.deepseek.com/v1",
        hint="便宜、支持 cache、编程强;V4-flash 最快,reasoner 带思考链",
        env_hint="在 https://platform.deepseek.com 取 key",
    ),
    "minimax": ProviderPreset(
        label="MiniMax (abab / M2)",
        kind="minimax",
        default_model="abab6.5-chat",
        default_base_url="https://api.minimax.chat/v1",
        hint="长上下文、中文强",
        env_hint="在 https://www.minimaxi.com 取 key",
    ),
    "claude": ProviderPreset(
        label="Claude (Anthropic 官方)",
        kind="claude",
        default_model="claude-sonnet-4-6",
        default_base_url="https://api.anthropic.com",
        hint="cache 最便宜、工具调用最稳,需要海外网络",
        env_hint="在 https://console.anthropic.com 取 key",
    ),
    "openai": ProviderPreset(
        label="OpenAI (gpt-4.1 / gpt-5 / ...)",
        kind="openai",
        default_model="gpt-4.1",
        default_base_url="https://api.openai.com/v1",
        hint="需要海外网络",
        env_hint="在 https://platform.openai.com 取 key",
    ),
    "custom": ProviderPreset(
        label="自定义 (手动填 kind / model / base_url)",
        kind="openai",
        default_model="",
        default_base_url="",
        hint="用于中转站、其它 OpenAI 兼容服务",
    ),
}


# ───────────────────────── embedder presets ──────────────────────────

@dataclass
class EmbedderPreset:
    label: str
    provider: str
    model: str
    base_url: str
    hint: str


EMBEDDER_PRESETS: dict[str, EmbedderPreset] = {
    "siliconflow-bge-m3": EmbedderPreset(
        label="SiliconFlow · BAAI/bge-m3 (推荐,免费额度,中英强)",
        provider="openai",
        model="BAAI/bge-m3",
        base_url="https://api.siliconflow.cn/v1",
        hint="注册送 14 元,个人用基本永远用不完",
    ),
    "zhipu-embedding-3": EmbedderPreset(
        label="智谱 · embedding-3 (用已有 GLM key 即可)",
        provider="openai",
        model="embedding-3",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        hint="¥0.0005/1K tokens,能复用 GLM 账户",
    ),
    "openai": EmbedderPreset(
        label="OpenAI · text-embedding-3-small",
        provider="openai",
        model="text-embedding-3-small",
        base_url="https://api.openai.com/v1",
        hint="$0.02/1M tokens,需要海外网络",
    ),
    "local": EmbedderPreset(
        label="本地 sentence-transformers (需装 GPU 栈)",
        provider="local",
        model="BAAI/bge-m3",
        base_url="",
        hint="零花费,首次下载 ~2GB",
    ),
    "hash": EmbedderPreset(
        label="hash (零依赖,质量差,兜底用)",
        provider="hash",
        model="baseline",
        base_url="",
        hint="只用于 MVP 测试,召回质量接近随机",
    ),
}


# ───────────────────────── state detection ──────────────────────────

@dataclass
class ProjectState:
    root: Path
    config_path: Path
    config: dict = field(default_factory=dict)
    has_config: bool = False
    skill_dir_exists: bool = False
    skill_l0_exists: bool = False
    skill_l1_exists: bool = False
    memory_db_exists: bool = False
    sample_sops_present: bool = False

    @property
    def is_partial(self) -> bool:
        if not self.has_config:
            return False
        # Config exists but stores look incomplete
        want_skills = all([self.skill_dir_exists,
                           self.skill_l0_exists,
                           self.skill_l1_exists])
        return not (want_skills and self.memory_db_exists)


def detect_state(root: Path) -> ProjectState:
    config_path = root / "config.toml"
    state = ProjectState(root=root, config_path=config_path)
    if config_path.exists():
        try:
            state.config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            state.has_config = True
        except Exception as e:
            console.print(f"[yellow]⚠ 现有 config.toml 解析失败: {e}[/yellow]")
            state.config = {}

    skill_dir_str = (state.config.get("memory") or {}).get("skill_dir", "./skills")
    memory_db_str = (state.config.get("memory") or {}).get("memory_db", "./memory/memory.db")
    skill_dir = (root / skill_dir_str.lstrip("./")).resolve()
    memory_db = (root / memory_db_str.lstrip("./")).resolve()

    state.skill_dir_exists = skill_dir.exists() and skill_dir.is_dir()
    state.skill_l0_exists = (skill_dir / "L0.md").exists()
    state.skill_l1_exists = (skill_dir / "L1_index.txt").exists()
    state.memory_db_exists = memory_db.exists()
    state.sample_sops_present = (skill_dir / "L3").exists() and \
        any((skill_dir / "L3").glob("*.md"))
    return state


# ───────────────────────── ping validators ──────────────────────────


_URL_SUFFIX_NOISE = (
    # Users often paste the full endpoint (from provider docs) into the
    # base_url field. Strip only the trailing endpoint path — keep the
    # `/v1` or equivalent prefix intact.
    "/chat/completions",
    "/embeddings",
    "/messages",
)


def normalize_base_url(base_url: str, *, kind: str = "openai") -> str:
    """Strip endpoint suffixes users commonly paste from provider docs.
    Caller passes `kind` so we know which version prefix belongs and
    which is noise — Anthropic wants `/v1` appended by the client, while
    OpenAI-compat wants `/v1` kept."""
    if not base_url:
        return base_url
    u = base_url.strip().rstrip("/")
    for sfx in _URL_SUFFIX_NOISE:
        if u.endswith(sfx):
            u = u[:-len(sfx)].rstrip("/")
            break
    # For Claude, the adapter appends `/v1/messages` itself, so strip a
    # trailing `/v1` too — users paste `.../anthropic/v1` thinking it's
    # the base.
    if kind == "claude" and u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def ping_anthropic_provider(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    url = f"{normalize_base_url(base_url, kind='claude')}/v1/messages"
    try:
        r = httpx.post(url, json={
            "model": model, "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, timeout=15.0)
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def ping_openai_compat_provider(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    url = f"{normalize_base_url(base_url)}/chat/completions"
    try:
        r = httpx.post(url, json={
            "model": model, "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, timeout=15.0)
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def ping_embedder(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    url = f"{normalize_base_url(base_url)}/embeddings"
    try:
        r = httpx.post(url, json={"model": model, "input": ["hi"]},
                       headers={
                           "Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                       }, timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            dim = len(data["data"][0]["embedding"])
            return True, f"ok · dim={dim}"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def ping_provider_by_kind(kind: str, *, api_key: str, base_url: str,
                          model: str) -> tuple[bool, str]:
    if kind == "claude":
        return ping_anthropic_provider(api_key, base_url, model)
    return ping_openai_compat_provider(api_key, base_url, model)


# ───────────────────────── the wizard ──────────────────────────

def run_wizard(root: Path) -> None:
    _banner(root)
    state = detect_state(root)
    _print_state_table(state)

    existing_providers = list(state.config.get("providers") or [])

    # Step 1: providers
    providers = _providers_phase(existing_providers)
    if not providers:
        console.print("[red]没有配置任何 provider,向导退出。[/red]")
        return

    # Step 2: memory + embedder
    memory_cfg = _memory_phase(state.config.get("memory") or {})

    # Step 3: initialize stores
    _stores_phase(root, state, memory_cfg)

    # Step 4: write config.toml
    _write_config(state.config_path, providers=providers, memory_cfg=memory_cfg,
                  failover_chain=_PhaseResult.chain, existing=state.config)

    # Step 5: summary
    _final_summary(state.config_path)


def _banner(root: Path) -> None:
    console.print(Panel.fit(
        "[bold green]Bonsai Setup[/bold green]\n"
        f"project: {root}\n"
        "按 Enter 使用方括号里的默认值; Ctrl+C 中断(已写入的不会丢失)",
        border_style="green",
    ))


def _print_state_table(state: ProjectState) -> None:
    t = Table(title="当前项目状态", show_header=True, header_style="bold")
    t.add_column("项")
    t.add_column("状态")
    t.add_row("config.toml", _yn(state.has_config))
    t.add_row("SkillStore 目录", _yn(state.skill_dir_exists))
    t.add_row("skills/L0.md (身份)", _yn(state.skill_l0_exists))
    t.add_row("skills/L1_index.txt", _yn(state.skill_l1_exists))
    t.add_row("样例 SOP(skills/L3/*.md)", _yn(state.sample_sops_present))
    t.add_row("MemoryStore DB", _yn(state.memory_db_exists))
    console.print(t)
    if state.is_partial:
        console.print("[yellow]检测到半初始化状态,向导会修补缺失的部分。[/yellow]")


def _yn(b: bool) -> str:
    return "[green]✓[/green]" if b else "[red]✗[/red]"


# ---------- Phase 1: providers ---------------------------------------

def _providers_phase(existing: list[dict]) -> list[dict]:
    console.rule("[bold]Step 1: Providers (模型)[/bold]")
    providers = [dict(p) for p in existing]  # copy

    if providers:
        console.print("[cyan]已配置的 provider:[/cyan]")
        for i, p in enumerate(providers, 1):
            console.print(f"  {i}. [bold]{p['name']}[/bold] · {p.get('kind')} · {p.get('model')}")

        action = Prompt.ask(
            "\n如何处理?",
            choices=["keep", "add", "edit", "delete", "clear"],
            default="keep",
        )
        if action == "keep":
            pass
        elif action == "clear":
            providers = []
        elif action == "delete":
            idx = int(Prompt.ask("删除哪个编号")) - 1
            if 0 <= idx < len(providers):
                removed = providers.pop(idx)
                console.print(f"[dim]删除 {removed['name']}[/dim]")
        elif action == "edit":
            idx = int(Prompt.ask("编辑哪个编号")) - 1
            if 0 <= idx < len(providers):
                providers[idx] = _prompt_single_provider(existing=providers[idx])
        elif action == "add":
            providers.append(_prompt_single_provider())

    # If no providers configured yet, force at least one.
    while not providers:
        console.print("[yellow]至少需要配置 1 个 provider。[/yellow]")
        providers.append(_prompt_single_provider())
        if Confirm.ask("继续添加更多 provider?", default=False):
            continue
        break

    # Offer to add more even in the keep branch.
    while Confirm.ask("\n要再添加 provider 吗?(多个可做 failover)", default=False):
        providers.append(_prompt_single_provider())

    # Failover chain
    names = [p["name"] for p in providers]
    if len(names) == 1:
        chain = names
    else:
        default_chain = ",".join(names)
        raw = Prompt.ask(
            f"Failover 顺序(逗号分隔,第一个为主; 现有 provider: {names})",
            default=default_chain,
        )
        chain = [s.strip() for s in raw.split(",") if s.strip() in names]
        if not chain:
            chain = names

    # Attach chain back onto providers list container via a module-level tuple
    _PhaseResult.chain = chain
    return providers


class _PhaseResult:
    """Small ghost holder to pass the failover chain between phases."""
    chain: list[str] = []


def _prompt_single_provider(existing: dict | None = None) -> dict:
    existing = existing or {}

    # Show preset menu
    console.print("\n[bold]选择预设:[/bold]")
    keys = list(PROVIDER_PRESETS.keys())
    for i, k in enumerate(keys, 1):
        p = PROVIDER_PRESETS[k]
        console.print(f"  [bold]{i}[/bold]  {p.label}")
        console.print(f"     [dim]{p.hint}[/dim]")
    idx = int(Prompt.ask("选择 (输入序号)", default="1", show_default=True))
    preset = PROVIDER_PRESETS[keys[max(1, min(idx, len(keys))) - 1]]
    if preset.env_hint:
        console.print(f"[dim]  提示: {preset.env_hint}[/dim]")

    default_name = existing.get("name") or preset.label.split()[0].lower().replace("(", "").replace(")", "")
    default_name = _sanitize_name(default_name)
    name = Prompt.ask("provider 名字 (用于 failover 链中引用)", default=default_name)

    kind = existing.get("kind") or preset.kind
    if preset.label.startswith("自定义"):
        kind = Prompt.ask("kind", choices=["claude", "openai", "glm", "qwen", "minimax"],
                          default=kind)

    model = Prompt.ask("model", default=existing.get("model") or preset.default_model)
    base_url = Prompt.ask("base_url", default=existing.get("base_url") or preset.default_base_url)

    existing_key = existing.get("api_key", "")
    key_default = existing_key if existing_key and not existing_key.startswith("$ref:") else ""
    api_key = Prompt.ask(
        "api_key (直接粘贴 或 留空跳过)",
        default=key_default, show_default=bool(key_default),
        password=True if not key_default else False,
    )
    if not api_key:
        api_key = existing_key

    prov: dict = {"name": name, "kind": kind, "model": model,
                  "base_url": base_url, "api_key": api_key}

    # Optional ping
    if api_key and not api_key.startswith("$ref:") and \
       Confirm.ask(f"立即测试 {name} 连通性?", default=True):
        ok, msg = ping_provider_by_kind(kind, api_key=api_key,
                                         base_url=base_url, model=model)
        if ok:
            console.print(f"  [green]✓ 连通[/green] · {msg}")
        else:
            console.print(f"  [red]✗ 连通失败[/red]: {msg}")
            if not Confirm.ask("仍然保留此 provider?", default=True):
                return _prompt_single_provider(existing)
    return prov


def _sanitize_name(s: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9\-_]+", "-", s).strip("-") or "provider"


# ---------- Phase 2: memory ------------------------------------------

def _memory_phase(existing: dict) -> dict:
    console.rule("[bold]Step 2: 长期记忆 + Embedder[/bold]")

    use_memory = Confirm.ask("启用长期记忆系统?(不启用就只是一个会话工具)",
                              default=True)
    if not use_memory:
        return {
            "skill_dir": existing.get("skill_dir", "./skills"),
            "memory_db": existing.get("memory_db", "./memory/memory.db"),
            "embed_provider": "hash",
            "embed_model": "baseline",
        }

    console.print("\n[bold]选择 embedder:[/bold]")
    keys = list(EMBEDDER_PRESETS.keys())
    for i, k in enumerate(keys, 1):
        e = EMBEDDER_PRESETS[k]
        console.print(f"  [bold]{i}[/bold]  {e.label}")
        console.print(f"     [dim]{e.hint}[/dim]")

    # Try to pick a sensible default based on existing config.
    default_idx = 1
    if existing.get("embed_model"):
        for i, k in enumerate(keys, 1):
            if EMBEDDER_PRESETS[k].model == existing.get("embed_model"):
                default_idx = i
                break

    idx = int(Prompt.ask("选择", default=str(default_idx)))
    embed_preset = EMBEDDER_PRESETS[keys[max(1, min(idx, len(keys))) - 1]]

    skill_dir = Prompt.ask("skill_dir", default=existing.get("skill_dir", "./skills"))
    memory_db = Prompt.ask("memory_db", default=existing.get("memory_db", "./memory/memory.db"))

    mem_cfg: dict = {
        "skill_dir": skill_dir,
        "memory_db": memory_db,
        "embed_provider": embed_preset.provider,
        "embed_model": embed_preset.model,
    }
    if embed_preset.base_url:
        mem_cfg["embed_base_url"] = embed_preset.base_url

    if embed_preset.provider == "openai":
        existing_key = existing.get("embed_api_key", "")
        key_default = existing_key if existing_key and not existing_key.startswith("$ref:") else ""
        api_key = Prompt.ask(
            "embed_api_key (直接粘贴 或 留空跳过)",
            default=key_default, show_default=bool(key_default),
            password=not bool(key_default),
        )
        if not api_key:
            api_key = existing_key
        mem_cfg["embed_api_key"] = api_key

        if api_key and not api_key.startswith("$ref:") and \
           Confirm.ask("测试 embedder 连通性?", default=True):
            ok, msg = ping_embedder(api_key, embed_preset.base_url, embed_preset.model)
            if ok:
                console.print(f"  [green]✓ 连通[/green] · {msg}")
            else:
                console.print(f"  [red]✗ 失败[/red]: {msg}")
    elif embed_preset.provider == "local":
        console.print("[dim]  提示: 启动时会自动下载 ~2GB 模型,仅首次[/dim]")

    return mem_cfg


# ---------- Phase 3: initialize stores -------------------------------

def init_stores(root: Path, mem_cfg: dict) -> dict:
    """Idempotent store init, safe to call from HTTP context.
    Creates skill dir layout, seeds L0 + sample SOPs, opens memory DB."""
    skill_dir = (root / mem_cfg.get("skill_dir", "./skills").lstrip("./")).resolve()
    memory_db = (root / mem_cfg.get("memory_db", "./memory/memory.db").lstrip("./")).resolve()

    from ..stores.skill_store import SkillStore
    SkillStore(skill_dir).init()
    _seed_l0_if_missing(skill_dir)
    _seed_sample_sop_if_missing(root, skill_dir)

    memory_db.parent.mkdir(parents=True, exist_ok=True)
    from ..stores.memory_store import MemoryStore
    ms = MemoryStore(memory_db, embedder=None)
    ms.close()
    return {"skill_dir": str(skill_dir), "memory_db": str(memory_db)}


def _stores_phase(root: Path, state: ProjectState, mem_cfg: dict) -> None:
    console.rule("[bold]Step 3: 初始化存储[/bold]")

    skill_dir = (root / mem_cfg["skill_dir"].lstrip("./")).resolve()
    memory_db = (root / mem_cfg["memory_db"].lstrip("./")).resolve()

    # SkillStore
    from ..stores.skill_store import SkillStore
    store = SkillStore(skill_dir)
    need_skill_init = not (state.skill_dir_exists and state.skill_l0_exists
                           and state.skill_l1_exists)
    if need_skill_init:
        action = "初始化"
        if state.skill_dir_exists:
            action = "修补(已有文件不动)"
        console.print(f"[cyan]SkillStore[/cyan] {action}: {skill_dir}")
        store.init()
        _seed_l0_if_missing(skill_dir)
        _seed_sample_sop_if_missing(root, skill_dir)
    else:
        if Confirm.ask(f"SkillStore 已完整 ({skill_dir})。要重新种子样例 SOP 吗?",
                        default=False):
            _seed_sample_sop_if_missing(root, skill_dir, force=True)

    # MemoryStore — only init schema, don't touch existing data
    from ..stores.memory_store import MemoryStore
    memory_db.parent.mkdir(parents=True, exist_ok=True)
    if not memory_db.exists():
        console.print(f"[cyan]MemoryStore[/cyan] 初始化: {memory_db}")
    else:
        console.print(f"[dim]MemoryStore[/dim] 已存在: {memory_db}(保留现有数据)")
    # Build with no embedder just to create the schema quickly
    store_ms = MemoryStore(memory_db, embedder=None)
    stats = store_ms.stats()
    store_ms.close()
    console.print(f"  [dim]现有: {stats['drawers']} drawers / "
                  f"{stats['rooms']} rooms / {stats['wings']} wings[/dim]")


def _seed_l0_if_missing(skill_dir: Path) -> None:
    p = skill_dir / "L0.md"
    if p.exists() and p.read_text(encoding="utf-8").strip():
        return
    console.print(f"  [green]+[/green] 写入 {p.relative_to(skill_dir.parent)}")
    p.write_text(_L0_TEMPLATE, encoding="utf-8")


def _seed_sample_sop_if_missing(root: Path, skill_dir: Path, *, force: bool = False) -> None:
    sample_src = root / "skills" / "L3_samples"
    sample_dst = skill_dir / "L3"
    if not sample_src.exists():
        return
    sample_dst.mkdir(parents=True, exist_ok=True)
    for src_file in sample_src.glob("*.md"):
        dst_file = sample_dst / src_file.name
        if dst_file.exists() and not force:
            continue
        dst_file.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"  [green]+[/green] 种子样例 SOP: {dst_file.relative_to(skill_dir)}")


_L0_TEMPLATE = """# L0 Identity

## 关于我(用户本人)
- 我是 Bonsai 的使用者 / 主人
- (编辑本文件,用简洁语言描述你自己:职业 / 偏好的语言 / 常用工具)

## 协作偏好
- 答复要简洁,能一句话不写两句
- 不用夸奖开场(不要"好的!""明白!")
- 每次回合先查 skill(`skill_lookup`)再决定动不动手
- 能并行工具就并行,能一个回合做完别分两个

## 红线(不做)
- 不主动做破坏性操作(删文件 / 改数据库 / 推代码)之前要先问我
- 不写我没让你写的 .md(除非我明确要求)
"""


# ---------- Phase 4: write config ------------------------------------

def _write_config(config_path: Path, *, providers: list[dict],
                  memory_cfg: dict,
                  failover_chain: list[str] | None = None,
                  existing: dict | None = None) -> None:
    console.rule("[bold]Step 4: 写入 config.toml[/bold]")

    chain = failover_chain or _PhaseResult.chain or [p["name"] for p in providers]

    out = _render_toml(providers=providers, failover_chain=chain,
                       memory_cfg=memory_cfg, existing=existing or {})
    if config_path.exists():
        backup = config_path.with_suffix(".toml.bak")
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[dim]  旧配置备份到: {backup}[/dim]")
    config_path.write_text(out, encoding="utf-8")
    console.print(f"[green]✓[/green] 写入 {config_path}")


def _render_toml(*, providers: list[dict], failover_chain: list[str],
                 memory_cfg: dict, existing: dict) -> str:
    """Hand-write the TOML so we can include helpful comments."""
    agent = dict(existing.get("agent") or {})
    frontend = existing.get("frontend") or {}
    logging_cfg = dict(existing.get("logging") or {})
    # Web save route piggybacks agent/logging overrides through memory_cfg
    # so the CLI signature stays stable.
    if isinstance(memory_cfg, dict):
        if memory_cfg.get("_agent"):
            agent.update({k: v for k, v in memory_cfg["_agent"].items() if v not in (None, "")})
        if memory_cfg.get("_logging"):
            logging_cfg.update({k: v for k, v in memory_cfg["_logging"].items() if v not in (None, "")})

    lines = [
        "# Bonsai config — generated by `bonsai setup`",
        "# Re-run the wizard any time: `bonsai setup`",
        "# Sensitive values can use $ref:env:NAME to pull from env instead.",
        "",
        "[agent]",
        f"max_turns = {agent.get('max_turns', 40)}",
        f"budget_hard = {agent.get('budget_hard', 60000)}",
        f"budget_soft = {agent.get('budget_soft', 40000)}",
        f'working_dir = "{agent.get("working_dir", ".")}"',
        "",
        "[frontend]",
        f'default = "{frontend.get("default", "cli")}"',
        "",
    ]
    for p in providers:
        lines.append("[[providers]]")
        lines.append(f'name = "{p["name"]}"')
        lines.append(f'kind = "{p["kind"]}"')
        lines.append(f'model = "{p["model"]}"')
        lines.append(f'base_url = "{p.get("base_url", "")}"')
        lines.append(f'api_key = {_quote_secret(p.get("api_key", ""))}')
        if p.get("max_tokens"):
            lines.append(f'max_tokens = {p["max_tokens"]}')
        lines.append("")
    lines.append("[failover]")
    lines.append(f"chain = {list(failover_chain)!r}".replace("'", '"'))
    lines.append("")
    lines.append("[memory]")
    lines.append(f'skill_dir = "{memory_cfg.get("skill_dir", "./skills")}"')
    lines.append(f'memory_db = "{memory_cfg.get("memory_db", "./memory/memory.db")}"')
    lines.append(f'embed_provider = "{memory_cfg.get("embed_provider", "hash")}"')
    lines.append(f'embed_model = "{memory_cfg.get("embed_model", "baseline")}"')
    if memory_cfg.get("embed_base_url"):
        lines.append(f'embed_base_url = "{memory_cfg["embed_base_url"]}"')
    if memory_cfg.get("embed_api_key"):
        lines.append(f'embed_api_key = {_quote_secret(memory_cfg["embed_api_key"])}')
    lines.append("")
    lines.append("[logging]")
    lines.append(f'level = "{logging_cfg.get("level", "INFO")}"')
    lines.append(f'cache_stats = "{logging_cfg.get("cache_stats", "./logs/cache_stats.jsonl")}"')
    lines.append("")

    # maintenance — 永运行下 daemon 线程每 N 小时清一次旧日志。
    # 即使用户不改, 也总是写出这段, 这样他们在配置页能看到且改值(不可删)。
    maint = dict(existing.get("maintenance") or {})
    if isinstance(memory_cfg, dict) and memory_cfg.get("_maintenance"):
        maint.update({k: v for k, v in memory_cfg["_maintenance"].items()
                      if v not in (None, "")})
    lines.append("[maintenance]")
    lines.append(f"gc_enabled = {str(bool(maint.get('gc_enabled', True))).lower()}")
    lines.append(f"gc_retention_days = {int(maint.get('gc_retention_days', 15))}")
    lines.append(f"gc_interval_hours = {int(maint.get('gc_interval_hours', 24))}")
    lines.append("")

    # channels can come from existing (CLI keep path) OR via memory_cfg["_channels"]
    # piggyback (web route passes user-edited channels in the save body)
    piggy = memory_cfg.get("_channels") if isinstance(memory_cfg, dict) else None
    channels = piggy if piggy is not None else (existing.get("channels") or {})
    for kind, ccfg in channels.items():
        if not isinstance(ccfg, dict):
            continue
        lines.append(f"[channels.{kind}]")
        for k, v in ccfg.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = {_quote_secret(str(v))}')
        lines.append("")
    return "\n".join(lines)


def _quote_secret(v: str) -> str:
    if not v:
        return '""'
    # Preserve $ref:... references as bare-quoted strings.
    return f'"{v}"'


# ---------- Phase 5: summary -----------------------------------------

def _final_summary(config_path: Path) -> None:
    console.rule("[bold green]准备就绪[/bold green]")
    console.print(f"[green]✓[/green] config: {config_path}")
    console.print("\n[bold]下一步:[/bold]")
    console.print("  1. 自检    [cyan]bonsai doctor[/cyan]")
    console.print("  2. 开聊    [cyan]bonsai chat[/cyan]")
    console.print("  3. Web UI  [cyan]bonsai serve[/cyan]")
    console.print("  4. 挂 MCP  [cyan]bonsai mcp[/cyan]")
    console.print("  5. 看身份  [cyan]edit skills/L0.md[/cyan](告诉 Bonsai 你是谁)")
