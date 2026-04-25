"""Bonsai CLI — REPL and utility commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from ..adapters import build_adapter
from ..config import load_config
from ..core.backend import FailoverChain
from ..core.budget import BudgetPolicy
from ..core.cache_monitor import CacheMonitor
from ..core.handler import Handler
from ..core.loop import AgentLoop
from ..core.session import Session
from ..core.session_log import SessionLog
from ..core.types import FrozenPrefix
from ..core.wakeup import build_wakeup
from ..stores.embed import build_embedder
from ..stores.memory_store import MemoryStore
from ..stores.skill_store import SkillStore
from ..tools.schema_spec import ALL_TOOLS, load_tool_specs
from ..writer.session_archiver import schedule_ingest
from .doctor import run_doctor
from .setup_wizard import run_wizard

app = typer.Typer(add_completion=False, no_args_is_help=False,
                  help="Bonsai — a constrained-but-growing personal agent")
console = Console()


def _project_root() -> Path:
    # Assume cwd; user can override via --project.
    return Path.cwd()


def _load_system_prompt(root: Path) -> str:
    sp = root / "prompts" / "system.txt"
    if sp.exists():
        return sp.read_text(encoding="utf-8")
    return (
        "You are Bonsai — a disciplined personal agent.\n"
        "Use tools to act, don't guess. Run tools in parallel when safe.\n"
        "Be terse in replies; verbose only in code/files."
    )


async def _ask_user(question: str, candidates: list[str] | None) -> str:
    console.print(Panel(question, title="agent asks", style="yellow"))
    if candidates:
        for i, c in enumerate(candidates, 1):
            console.print(f"  [bold]{i}[/bold] {c}")
    # Use asyncio-compatible prompt.
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(">>> ").strip())


@app.command()
def setup(
    project: Path = typer.Option(None, "--project", "-p", help="project root"),
) -> None:
    """交互式向导 — 首次上手或想重新配置时运行。可以重复触发,不会丢数据。"""
    root = (project or _project_root()).resolve()
    run_wizard(root)


@app.command()
def doctor(
    project: Path = typer.Option(None, "--project", "-p", help="project root"),
) -> None:
    """自检 —— Python / 依赖 / config / provider / embedder / stores 全链路检查。"""
    root = (project or _project_root()).resolve()
    raise typer.Exit(run_doctor(root))


@app.command()
def chat(
    config: Path = typer.Option(None, "--config", "-c", help="path to config.toml"),
    project: Path = typer.Option(None, "--project", "-p", help="project root"),
    provider: str | None = typer.Option(None, help="override failover primary"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    browser: str | None = typer.Option(
        None, "--browser",
        help=(
            "enable browser tools. Values:\n"
            "  'managed'          — spawn an isolated chromium (login not shared)\n"
            "  'managed-headless' — same, headless\n"
            "  'attach' or URL    — attach to user's Chrome "
            "(launch with --remote-debugging-port=9222 first; default URL "
            "http://127.0.0.1:9222)\n"
            "  'bridge'           — drive an already-running Chrome via the "
            "extension under assets/chrome_bridge/ (no relaunch needed; install "
            "extension once, then `pip install bonsai-agent[bridge]`)"
        )),
) -> None:
    """Interactive REPL."""
    # Pre-config: minimal console logging so config-parse errors show up.
    logging.basicConfig(level="DEBUG" if verbose else "INFO",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(config)
    except FileNotFoundError:
        console.print(Panel(
            "[bold red]未找到 config.toml[/bold red]\n\n"
            "请先运行: [cyan]bonsai setup[/cyan]\n"
            "(可重复触发,不会丢数据)",
            border_style="red", title="配置缺失",
        ))
        raise typer.Exit(1)
    except Exception as e:
        console.print(Panel(
            f"[bold red]config.toml 解析失败[/bold red]\n\n{e}\n\n"
            "修复建议: [cyan]bonsai doctor[/cyan] 诊断 / [cyan]bonsai setup[/cyan] 重建",
            border_style="red",
        ))
        raise typer.Exit(1)
    root = (project or _project_root()).resolve()

    # Post-config: full dual-output. --verbose bumps console to DEBUG; file
    # always captures DEBUG for postmortem of silent disconnects etc.
    from ..log_setup import setup_logging
    console_lvl = "DEBUG" if verbose else (cfg.logging.console_level or cfg.logging.level)
    log_path = setup_logging(
        log_file=cfg.logging.log_file, console_level=console_lvl,
        file_level=cfg.logging.file_level, project_root=root, force=True,
    )
    console.print(f"[dim]  log → {log_path}[/dim]")

    providers_cfg = cfg.failover_providers()
    if provider:
        primary = next((p for p in cfg.providers if p["name"] == provider), None)
        if primary is None:
            console.print(f"[red]provider {provider!r} not in config[/red]")
            raise typer.Exit(1)
        providers_cfg = [primary] + [p for p in providers_cfg if p["name"] != provider]

    if not providers_cfg:
        console.print(Panel(
            "[bold yellow]没有配置任何 provider[/bold yellow]\n\n"
            "请先运行: [cyan]bonsai setup[/cyan] 添加至少 1 个 provider",
            border_style="yellow",
        ))
        raise typer.Exit(1)

    backends = [build_adapter(p) for p in providers_cfg]
    monitor = CacheMonitor(log_path=Path(cfg.logging.cache_stats))
    chain = FailoverChain(backends=backends, monitor=monitor)

    # Stores
    skill_store = SkillStore(root / cfg.memory.skill_dir.lstrip("./"))
    skill_store.init()
    embed_cfg = {
        "embed_provider": getattr(cfg.memory, "embed_provider", "hash"),
        "embed_api_key": getattr(cfg.memory, "embed_api_key", ""),
        "embed_base_url": getattr(cfg.memory, "embed_base_url", ""),
        "embed_model": cfg.memory.embed_model,
    }
    embedder = build_embedder(embed_cfg)
    memory_store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=embedder,
    )

    # Wake-up is rebuilt every user turn (see _fresh_prefix below) so new
    # SOPs / L0 edits take effect without restart. First build is just the
    # initial value — `prefix` is reassigned each loop iteration.
    base_sys = _load_system_prompt(root)
    from ..runtime import render_wakeup_prefix

    schema_path = root / "tools" / "schema.json"
    # Always expose web_* — Handler lazy-spawns headless managed chromium
    # on first call when no --browser was given.
    tool_specs = (
        load_tool_specs(schema_path, names=ALL_TOOLS, include_memory_recall=True)
        if schema_path.exists() else []
    )

    def _fresh_prefix() -> FrozenPrefix:
        return FrozenPrefix(
            system_prompt=render_wakeup_prefix(base_sys, skill_store, memory_store),
            tools=tool_specs,
        )

    prefix = _fresh_prefix()

    browser_session = None
    bridge_pending = False
    if browser:
        from ..tools.browser import BrowserSession
        mode = browser.strip().lower()
        if mode == "bridge":
            bridge_pending = True
            console.print(
                "[dim]browser: bridge mode — install assets/chrome_bridge/ in "
                "Chrome first.[/dim]"
            )
        elif mode in ("managed", "managed-headless"):
            import asyncio as _asyncio
            browser_session = _asyncio.run(
                BrowserSession.managed(headless=(mode == "managed-headless"))
            )
            console.print(
                f"[dim]browser: managed chromium at {browser_session.debug_url}[/dim]"
            )
        else:
            url = "http://127.0.0.1:9222" if mode == "attach" else browser
            browser_session = BrowserSession(debug_url=url)
            console.print(f"[dim]browser: attaching to {url}[/dim]")

    from ..stores.evidence import EvidenceRecorder as _EvRec
    session = Session(cwd=root)
    evidence_recorder = _EvRec(skill_store.root, session_id=session.session_id)
    handler = Handler(
        session=session, schema_path=schema_path, prompt_fn=_ask_user,
        memory_store=memory_store, skill_store=skill_store,
        browser=browser_session, evidence=evidence_recorder,
    )
    policy = BudgetPolicy(soft=cfg.agent.budget_soft, hard=cfg.agent.budget_hard)

    session_log_path = root / "logs" / "sessions" / f"{session.session_id}.jsonl"
    sess_log = SessionLog(session_log_path, session.session_id)

    console.print(Panel.fit(
        f"[bold]Bonsai[/bold] · session={session.session_id} · "
        f"providers={[b.name for b in backends]}",
        style="green",
    ))
    console.print("  Ctrl+D or /quit to exit · /stats to print cache stats\n")

    async def main_loop() -> None:
        if bridge_pending:
            from ..tools.browser.bridge_server import BridgeServer
            from ..tools.browser.bridge_session import BridgeSession
            server = BridgeServer()
            try:
                await server.start()
            except RuntimeError as e:
                console.print(f"[red]bridge: {e}[/red]")
            else:
                console.print(
                    "[dim]browser: bridge listening on ws://127.0.0.1:18765 — "
                    "waiting for extension (≤30s)...[/dim]"
                )
                try:
                    await server.wait_for_extension(timeout=30.0)
                except Exception as e:
                    console.print(
                        f"[yellow]bridge: {e} — continuing without browser.[/yellow]"
                    )
                    await server.stop()
                else:
                    handler.browser = BridgeSession(server=server)
                    console.print(
                        f"[dim]browser: bridge connected, "
                        f"{len(server.tabs)} tab(s)[/dim]"
                    )
        while True:
            try:
                user_text = console.input("[bold cyan]you >[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_text:
                continue
            if user_text in ("/quit", "/exit"):
                break
            if user_text == "/stats":
                console.print(monitor.summary())
                continue
            if user_text == "/session":
                console.print(f"session_id={session.session_id} turns={session.turns}")
                continue
            if user_text == "/continue" or user_text.startswith("/continue "):
                parts = user_text.split(maxsplit=1)
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                _handle_continue(root, n, loop_maker=lambda pre: AgentLoop(
                    chain, pre, handler,
                    policy=policy, max_turns=cfg.agent.max_turns,
                    session_log=sess_log, soft_landing=False,
                ))
                continue

            loop = AgentLoop(chain, _fresh_prefix(), handler,
                             policy=policy, max_turns=cfg.agent.max_turns,
                             session_log=sess_log, soft_landing=False)
            loop.add_user(user_text)
            try:
                async for ev in loop.run():
                    _render_event(ev)
            except Exception as e:
                console.print(f"[red]error:[/red] {e}")
                if verbose:
                    console.print_exception()

    asyncio.run(main_loop())
    console.print("\n" + monitor.summary())

    # Fire-and-forget session archive into MemoryStore.
    if session_log_path.exists():
        console.print("[dim]  archiving session to memory store (background)...[/dim]")
        schedule_ingest(
            session_log_path,
            db_path=(root / cfg.memory.memory_db.lstrip("./")).resolve(),
            embed_provider=embed_cfg["embed_provider"],
            embed_api_key=embed_cfg["embed_api_key"],
            embed_base_url=embed_cfg["embed_base_url"],
            embed_model=embed_cfg["embed_model"],
            wing="chat",
            room=session.session_id,
        )
    memory_store.close()


def _render_event(ev) -> None:
    from ..core.types import ToolCall, Usage
    if ev.kind == "text" and ev.data:
        console.print(Markdown(ev.data))
    elif ev.kind == "tool_call":
        tc: ToolCall = ev.data
        arg_preview = _preview_args(tc.args)
        console.print(f"[magenta]⚙ {tc.name}[/magenta] {arg_preview}")
    elif ev.kind == "usage":
        u: Usage = ev.data
        if u.input_tokens:
            console.print(f"[dim]  tokens in={u.input_tokens} out={u.output_tokens} "
                          f"cache_read={u.cache_read_tokens} "
                          f"cache_create={u.cache_creation_tokens}[/dim]")
    elif ev.kind == "done":
        console.print(f"[dim]  done: {ev.data}[/dim]")
    elif ev.kind == "error":
        console.print(f"[red]  error: {ev.data}[/red]")


def _handle_continue(root: Path, n: int | None, *, loop_maker) -> None:
    """List past sessions, or summarize and describe session N."""
    sessions_dir = root / "logs" / "sessions"
    if not sessions_dir.exists():
        console.print("[yellow]no past sessions[/yellow]")
        return
    files = sorted(sessions_dir.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        console.print("[yellow]no past sessions[/yellow]")
        return
    if n is None:
        t = Table(title="past sessions")
        t.add_column("#"); t.add_column("session"); t.add_column("modified")
        t.add_column("turns")
        import time as _t
        for i, p in enumerate(files[:10], 1):
            n_turns = sum(1 for _ in p.open("rb"))
            t.add_row(str(i), p.stem,
                      _t.strftime("%Y-%m-%d %H:%M", _t.localtime(p.stat().st_mtime)),
                      str(n_turns))
        console.print(t)
        console.print("Type `/continue N` to load a specific session.")
        return
    if not (1 <= n <= len(files)):
        console.print(f"[red]no session #{n}[/red]")
        return
    target = files[n - 1]
    console.print(Panel(f"Loading session {target.stem}", style="cyan"))
    # Render AAAK summary via session_compactor if available.
    import orjson as _oj

    from ..writer.session_compactor import _aaak
    turns = [_oj.loads(ln) for ln in target.open("rb")]
    summary = _aaak(turns) if turns else "(empty)"
    console.print(Panel(summary, title="session summary", style="dim"))
    # We don't actually replay tool calls; we just show the summary so the
    # user knows where they were. True state-recovery would need more care.


def _preview_args(args: dict) -> str:
    if not args:
        return ""
    keys_show = []
    for k, v in list(args.items())[:3]:
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "..."
        keys_show.append(f"{k}={v!r}")
    return "(" + ", ".join(keys_show) + ")"


@app.command(name="autonomous")
def autonomous_cmd(
    action: str = typer.Argument("init",
        help="init = 建 data/autonomous/ 目录 + 种 todo 模板"),
    project: Path = typer.Option(None, "--project", "-p"),
    overwrite: bool = typer.Option(False, "--overwrite",
        help="init 时覆盖已有 todo.md (默认保留)"),
) -> None:
    """自主任务工作区管理。"""
    from ..autonomous import AutonomousWorkspace
    root = (project or _project_root()).resolve()
    w = AutonomousWorkspace(root)
    if action == "init":
        existed = w.initialized
        w.init(overwrite=overwrite)
        if existed and not overwrite:
            console.print(f"[yellow]⚠[/yellow] 已初始化过 ({w.dir})。加 --overwrite 重建 todo 模板。")
        else:
            console.print(f"[green]✓[/green] 建好 {w.dir}")
            console.print("  编辑 todo.md → 在 ⏰ 定时任务新建一条 prompt 跑 autonomous_mode SOP。")
    elif action == "status":
        console.print(f"目录: {w.dir}")
        console.print(f"已初始化: {w.initialized}")
        console.print(f"报告数: {len(w.list_reports())}")
        console.print(f"最近 3 条 history:\n  " + "\n  ".join(w.get_history(3)))
    else:
        console.print(f"[red]未知 action: {action}[/red] (支持 init / status)")
        raise typer.Exit(1)


_RUNNER_LOCK_PORTS = {"wechat": 19528, "telegram": 19529, "qq": 19530,
                      "feishu": 19531, "dingtalk": 19532}


@app.command(name="channel-run")
def channel_run(
    kind: str = typer.Argument(..., help="支持: wechat | telegram | qq | feishu | dingtalk"),
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
    allow: str = typer.Option("", "--allow",
        help="逗号分隔的用户 id 白名单;空 = 任何人都能触发"),
) -> None:
    """启动一个外部渠道的收发循环(阻塞)。"""
    import socket
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    from ..log_setup import setup_logging
    setup_logging(
        log_file=cfg.logging.log_file,
        console_level=cfg.logging.console_level or cfg.logging.level,
        file_level=cfg.logging.file_level, project_root=root, force=True,
    )
    # Hot-reload logging on config.toml change (channel runner subprocess).
    from ..runtime import register_hot_reloader as _reg_logging_reload
    def _reload_logging(_cfg) -> None:
        setup_logging(
            log_file=_cfg.logging.log_file,
            console_level=_cfg.logging.console_level or _cfg.logging.level,
            file_level=_cfg.logging.file_level, project_root=root, force=True,
        )
    _reg_logging_reload(_reload_logging)
    lock_port = _RUNNER_LOCK_PORTS.get(kind)
    if lock_port is None:
        console.print(f"[red]未实现的 kind: {kind}[/red]")
        raise typer.Exit(1)
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", lock_port))
    except OSError:
        console.print(f"[red]另一个 {kind} runner 已在运行 (port {lock_port} 被占)[/red]")
        raise typer.Exit(1)
    allowed = {u.strip() for u in allow.split(",") if u.strip()} if allow else None
    # Hot-reload: watch config.toml from this subprocess so provider/failover
    # edits saved via Web UI propagate without restarting the runner.
    from ..runtime import start_config_watcher
    _config_path = (config or (root / "config.toml")).resolve()
    start_config_watcher(_config_path)
    console.print(Panel.fit(
        f"[bold]{kind} runner[/bold] · root={root}\n"
        f"allowed_users={allowed or '(config/任何人)'}\n"
        f"config hot-reload: {_config_path}",
        style="green", title="启动",
    ))
    try:
        if kind == "wechat":
            from ..channels.runners import run_wechat
            run_wechat(root, cfg, allowed_users=allowed)
        elif kind == "telegram":
            from ..channels.runners_telegram import run_telegram
            run_telegram(root, cfg, allowed_users=allowed)
        elif kind == "qq":
            from ..channels.runners_qq import run_qq
            run_qq(root, cfg, allowed_users=allowed)
        elif kind == "feishu":
            from ..channels.runners_feishu import run_feishu
            run_feishu(root, cfg, allowed_users=allowed)
        elif kind == "dingtalk":
            from ..channels.runners_dingtalk import run_dingtalk
            run_dingtalk(root, cfg, allowed_users=allowed)
    except KeyboardInterrupt:
        console.print("\n[yellow]已退出[/yellow]")


@app.command()
def distill(
    action: str = typer.Argument(
        "list", help="list | inspect <session> | propose <session> <sop_name>"),
    session_id: str | None = typer.Argument(None),
    sop_name: str | None = typer.Argument(None),
    project: Path = typer.Option(None, "--project", "-p"),
    config: Path = typer.Option(None, "--config", "-c"),
    body_from: Path | None = typer.Option(
        None, "--body-from",
        help="read SOP body from file (skips interactive stdin)"),
) -> None:
    """Propose SOPs from captured evidence."""
    from .distill import print_candidates, print_inspection, propose_sop
    cfg = load_config(config) if config or Path("config.toml").exists() else None
    root = (project or _project_root()).resolve()
    if cfg is not None:
        skills_root = root / cfg.memory.skill_dir.lstrip("./")
    else:
        skills_root = root / "skills"
    action = action.lower()
    if action == "list":
        raise typer.Exit(print_candidates(skills_root))
    if action == "inspect":
        if not session_id:
            console.print("[red]inspect 需要 <session_id>[/red]")
            raise typer.Exit(1)
        raise typer.Exit(print_inspection(skills_root, session_id))
    if action == "propose":
        if not session_id or not sop_name:
            console.print("[red]propose 需要 <session_id> <sop_name>[/red]")
            raise typer.Exit(1)
        raise typer.Exit(propose_sop(skills_root, session_id, sop_name,
                                       body_from=body_from))
    console.print(f"[red]未知 action: {action}[/red] (支持 list / inspect / propose)")
    raise typer.Exit(1)


@app.command()
def providers(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """List configured providers."""
    cfg = load_config(config)
    t = Table(title="providers")
    t.add_column("name"); t.add_column("kind"); t.add_column("model"); t.add_column("base_url")
    for p in cfg.providers:
        t.add_row(p["name"], p["kind"], p["model"], p.get("base_url", "(default)"))
    console.print(t)
    if cfg.failover_chain:
        console.print(f"failover chain: {' → '.join(cfg.failover_chain)}")


@app.command()
def init(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
) -> None:
    """Initialize SkillStore + MemoryStore directories."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    skill_dir = (root / cfg.memory.skill_dir.lstrip("./")).resolve()
    db_path = (root / cfg.memory.memory_db.lstrip("./")).resolve()

    SkillStore(skill_dir).init()
    store = MemoryStore(db_path, embedder=build_embedder({
        "embed_provider": cfg.memory.embed_provider,
        "embed_model": cfg.memory.embed_model,
    }))
    store.close()
    console.print(f"[green]✓[/green] SkillStore at {skill_dir}")
    console.print(f"[green]✓[/green] MemoryStore at {db_path}")


@app.command(name="wake-up")
def wake_up(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
) -> None:
    """Print current L0 + L1 wake-up context (what the agent sees each session)."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    skill_store = SkillStore((root / cfg.memory.skill_dir.lstrip("./")).resolve())
    memory_store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=build_embedder({"embed_provider": cfg.memory.embed_provider,
                                  "embed_model": cfg.memory.embed_model}),
    )
    w = build_wakeup(skill_store, memory_store)
    console.print(Panel(w.render() or "(empty wake-up)", title="wake-up", style="cyan"))
    memory_store.close()


@app.command()
def search(
    query: str,
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
    wing: str = typer.Option(None),
    room: str = typer.Option(None),
    n: int = typer.Option(5),
) -> None:
    """Standalone MemoryStore search."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=build_embedder({"embed_provider": cfg.memory.embed_provider,
                                  "embed_model": cfg.memory.embed_model}),
    )
    drawers = store.search(query, wing=wing, room=room, n=n)
    store.close()
    if not drawers:
        console.print(f"[yellow]no hits for {query!r}[/yellow]")
        return
    for d in drawers:
        scope = f"{d.wing}/{d.room}" if d.wing else "(unscoped)"
        console.print(Panel(d.content, title=f"{scope} · score={d.score:.2f}"))


@app.command()
def mine(
    source: Path,
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
    wing: str = typer.Option("imported"),
    room: str = typer.Option("misc"),
) -> None:
    """Bulk-ingest files from SOURCE into MemoryStore."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=build_embedder({"embed_provider": cfg.memory.embed_provider,
                                  "embed_model": cfg.memory.embed_model}),
    )
    src = Path(source).expanduser().resolve()
    if src.is_file():
        files = [src]
    else:
        files = [p for p in src.rglob("*") if p.is_file() and p.suffix in
                 {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".rst"}]
    n = 0
    for p in files:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            console.print(f"[red]skip {p}: {e}[/red]")
            continue
        if not content.strip():
            continue
        if store.ingest(closet="life", wing=wing, room=room,
                        kind=p.suffix.lstrip("."), content=content,
                        meta={"path": str(p)}):
            n += 1
    store.close()
    console.print(f"[green]✓[/green] ingested {n}/{len(files)} file(s) into {wing}/{room}")


@app.command()
def reembed(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
    batch_size: int = typer.Option(32),
) -> None:
    """Re-embed all drawers with the currently-configured embedder.

    Use after switching embed_provider (hash → bge-m3, etc)."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    embedder = build_embedder({
        "embed_provider": cfg.memory.embed_provider,
        "embed_api_key": cfg.memory.embed_api_key,
        "embed_base_url": cfg.memory.embed_base_url,
        "embed_model": cfg.memory.embed_model,
    })
    store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=embedder,
    )
    before = store.stats()
    console.print(f"[dim]before: {before}[/dim]")
    console.print(f"[cyan]re-embedding {before['drawers']} drawer(s) "
                  f"with {cfg.memory.embed_model}...[/cyan]")
    n = store.reembed_all(batch_size=batch_size)
    console.print(f"[green]✓[/green] {n} drawer(s) re-embedded")
    store.close()


@app.command(name="memory-stats")
def memory_stats(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
) -> None:
    """Print MemoryStore counts."""
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=None,
    )
    stats = store.stats()
    store.close()
    t = Table(title="memory")
    t.add_column("metric"); t.add_column("count")
    for k, v in stats.items():
        t.add_row(k, str(v))
    console.print(t)


@app.command()
def serve(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(7878),
) -> None:
    """Start the Web UI (FastAPI + WebSocket)."""
    try:
        import uvicorn

        from ..frontends.web import make_app
    except ImportError as e:
        console.print(f"[red]✗ Web UI 依赖缺失:[/red] {e}")
        console.print("[yellow]更新一下依赖:[/yellow]")
        console.print("  [cyan]pip install -e . --upgrade[/cyan]")
        console.print("fastapi + uvicorn[standard] 现在是核心依赖（v0.1）。")
        raise typer.Exit(1)

    root = (project or _project_root()).resolve()
    # Dual-output logging so WS disconnects / provider timeouts leave a trail.
    # Config may not exist yet (first-run 🔑 tab); use defaults if so.
    from ..log_setup import setup_logging
    try:
        _cfg_for_log = load_config(config)
        _lcfg = _cfg_for_log.logging
        _con = _lcfg.console_level or _lcfg.level
        _lf, _fl = _lcfg.log_file, _lcfg.file_level
    except Exception:
        _con, _lf, _fl = "INFO", "./logs/bonsai.log", "DEBUG"
    log_path = setup_logging(log_file=_lf, console_level=_con,
                             file_level=_fl, project_root=root, force=True)
    console.print(f"[dim]log → {log_path}[/dim]")
    # Hot-reload: re-apply logging config when config.toml changes.
    from ..runtime import register_hot_reloader as _reg_logging_reload
    def _reload_logging(_cfg) -> None:
        setup_logging(
            log_file=_cfg.logging.log_file,
            console_level=_cfg.logging.console_level or _cfg.logging.level,
            file_level=_cfg.logging.file_level,
            project_root=root, force=True,
        )
    _reg_logging_reload(_reload_logging)
    # Web UI must start even without config — that's the whole point of the
    # 🔑 Models first-run tab. Don't eager-load. Each chat session lazy-loads
    # and fails cleanly if the user hasn't configured yet.
    config_override = config

    def chat_factory(prompt_user):
        try:
            cfg = load_config(config_override)
        except FileNotFoundError as e:
            raise RuntimeError(
                "尚未配置。请先在 🔑 模型 标签页填写 provider 并保存。"
            ) from e
        return _WebSessionCtx(cfg=cfg, root=root, prompt_user=prompt_user)

    web_app = make_app(root, chat_factory)
    # Hot-reload: cross-process config sync. Web UI's POST /api/config also
    # triggers reload directly (in-process), but the watcher catches hand-edits
    # and (when running) backs up that direct path.
    from ..runtime import start_config_watcher
    _config_path = (config or (root / "config.toml")).resolve()
    start_config_watcher(_config_path)
    console.print(f"[green]Bonsai Web[/green] on http://{host}:{port}/")
    console.print(f"[dim]config hot-reload: watching {_config_path}[/dim]")
    console.print("[dim]首次使用请在浏览器中进入 🔑 模型 标签页配置 provider。[/dim]")
    # access_log=False silences the per-request INFO lines (the poll
    # endpoints spam every ~2s). Real errors still come through at WARNING.
    uvicorn.run(web_app, host=host, port=port, log_level="warning", access_log=False)


@app.command()
def mcp(
    config: Path = typer.Option(None, "--config", "-c"),
    project: Path = typer.Option(None, "--project", "-p"),
) -> None:
    """Start Bonsai as an MCP server (stdio). Used by Claude Code / Cursor."""
    import asyncio as _asyncio

    from ..frontends.mcp_server import run_stdio
    cfg = load_config(config)
    root = (project or _project_root()).resolve()
    _asyncio.run(run_stdio(cfg, root))


@app.command()
def version() -> None:
    from bonsai import __version__
    console.print(f"bonsai {__version__}")


@app.command()
def gc(
    project: Path = typer.Option(None, "--project", "-p"),
    days: int = typer.Option(15, "--days",
                              help="retention window (default 15d)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                  help="report only, don't delete"),
) -> None:
    """清理 15 天前的 session / evidence JSONL(已归档到 MemoryStore 的内容不受影响)。"""
    from .gc import run_gc
    root = (project or _project_root()).resolve()
    try:
        cfg = load_config(None)
        skill_root = (root / cfg.memory.skill_dir.lstrip("./")).resolve()
    except Exception:
        skill_root = root / "skills"
    report = run_gc(root, retention_days=days, skill_root=skill_root,
                    dry_run=dry_run)
    console.print(report.render())


from ..core.session_log import load_messages as _load_messages_from_jsonl  # noqa: E402


class _WebSessionCtx:
    """Holds stores + chain for one websocket session. Used by web frontend."""

    def __init__(self, cfg, root, prompt_user):
        self._cfg = cfg
        self._root = root
        self._prompt = prompt_user
        providers_cfg = cfg.failover_providers()
        backends = [build_adapter(p) for p in providers_cfg]
        self._monitor = CacheMonitor(log_path=Path(cfg.logging.cache_stats))
        # Wrap in MutableBackend so config.toml changes can hot-swap the chain
        # into this live session without forcing a browser reconnect.
        from ..core.backend import MutableBackend
        from ..runtime import register_hot_reloader
        self._chain = MutableBackend(FailoverChain(backends=backends, monitor=self._monitor))
        self._unregister_reload = register_hot_reloader(self._on_config_reload)

        self._skill_store = SkillStore((root / cfg.memory.skill_dir.lstrip("./")).resolve())
        self._skill_store.init()
        embed_cfg = {"embed_provider": cfg.memory.embed_provider,
                     "embed_model": cfg.memory.embed_model}
        self._memory_store = MemoryStore(
            (root / cfg.memory.memory_db.lstrip("./")).resolve(),
            embedder=build_embedder(embed_cfg),
        )

        # base_sys is captured so new_loop() can rebuild a fresh wakeup each
        # time — picks up SOPs / L0 changes without restarting `bonsai serve`.
        from ..runtime import render_wakeup_prefix
        self._base_sys = _load_system_prompt(root)
        self._render_wakeup_prefix = render_wakeup_prefix

        schema_path = root / "tools" / "schema.json"
        self._tool_specs = load_tool_specs(schema_path, names=ALL_TOOLS,
                                            include_memory_recall=True) if schema_path.exists() else []
        self._prefix = self._build_prefix()
        self._session = Session(cwd=root)
        from ..stores.evidence import EvidenceRecorder as _EvRec
        evidence = _EvRec(self._skill_store.root, session_id=self._session.session_id)
        self._handler = Handler(
            session=self._session, schema_path=schema_path, prompt_fn=prompt_user,
            memory_store=self._memory_store, skill_store=self._skill_store,
            evidence=evidence,
        )
        self._policy = BudgetPolicy(soft=cfg.agent.budget_soft, hard=cfg.agent.budget_hard)
        self._log_path = root / "logs" / "sessions" / f"{self._session.session_id}.jsonl"
        self._sess_log = SessionLog(self._log_path, self._session.session_id)

    def _build_prefix(self) -> FrozenPrefix:
        """Compose system_prompt + current wakeup. Byte-stable for cache."""
        return FrozenPrefix(
            system_prompt=self._render_wakeup_prefix(
                self._base_sys, self._skill_store, self._memory_store),
            tools=self._tool_specs,
        )

    def new_loop(self, pre_messages=None) -> AgentLoop:
        # Rebuild prefix each call so /new + reconnect pick up new SOPs etc.
        # Web UI is interactive — user can /continue, so no soft landing.
        self._prefix = self._build_prefix()
        loop = AgentLoop(
            self._chain, self._prefix, self._handler,
            policy=self._policy, max_turns=self._cfg.agent.max_turns,
            session_log=self._sess_log, soft_landing=False,
        )
        if pre_messages:
            loop.tail.messages = list(pre_messages)
        return loop

    @property
    def session_id(self) -> str:
        return self._session.session_id

    def resume(self, sid: str) -> list:
        """Switch to an existing session's log file; return its prior messages
        to seed the next AgentLoop's tail."""
        old_path = self._root / "logs" / "sessions" / f"{sid}.jsonl"
        if not old_path.exists():
            raise ValueError(f"no such session: {sid}")
        from ..core.session_log import SessionLog
        self._session.session_id = sid
        self._log_path = old_path
        self._sess_log = SessionLog(old_path, sid)
        return _load_messages_from_jsonl(old_path)

    def reset(self) -> None:
        """Start a fresh conversation: new session_id + new log file + cleared tail."""
        import uuid
        from ..core.session_log import SessionLog
        new_sid = uuid.uuid4().hex[:12]
        self._session.session_id = new_sid
        self._log_path = self._root / "logs" / "sessions" / f"{new_sid}.jsonl"
        self._sess_log = SessionLog(self._log_path, new_sid)

    def _on_config_reload(self, cfg) -> None:
        """Hot-reload callback: swap the provider chain + refresh agent params
        + rebuild memory store if embedder/db changed. Visible immediately to
        in-flight loop.run() on the next backend.stream() call. New AgentLoop
        instances pick up max_turns / budget too."""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            new_chain = FailoverChain(
                backends=[build_adapter(p) for p in cfg.failover_providers()],
                monitor=self._monitor,
            )
            self._chain.swap(new_chain)
            self._policy = BudgetPolicy(soft=cfg.agent.budget_soft, hard=cfg.agent.budget_hard)
        except Exception as e:
            _log.warning("_WebSessionCtx provider hot reload skipped: %s", e)
            return
        # Memory store: only rebuild when something memory-related changed.
        from ..runtime import _memory_settings_changed, _rebuild_memory_store
        if _memory_settings_changed(self._cfg, cfg):
            try:
                new_store = _rebuild_memory_store(self._root, cfg)
            except Exception as e:
                _log.warning("memory hot reload skipped (rebuild failed): %s", e)
            else:
                old_store = self._memory_store
                self._memory_store = new_store
                self._handler.memory_store = new_store
                try:
                    if old_store is not None:
                        old_store.close()
                except Exception as e:
                    _log.debug("old memory_store close failed: %s", e)
                _log.info("web ctx memory store hot-reloaded "
                          "(embed_provider=%s db=%s)",
                          cfg.memory.embed_provider, cfg.memory.memory_db)
        self._cfg = cfg

    def cleanup(self) -> None:
        try:
            self._unregister_reload()
        except Exception:
            pass
        if self._log_path.exists():
            schedule_ingest(
                self._log_path,
                db_path=(self._root / self._cfg.memory.memory_db.lstrip("./")).resolve(),
                embed_provider=self._cfg.memory.embed_provider,
                embed_model=self._cfg.memory.embed_model,
                wing="chat", room=self._session.session_id,
            )
        self._memory_store.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
