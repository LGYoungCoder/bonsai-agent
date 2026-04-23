"""Cron-style scheduler for recurring agent tasks.

Each task lives as one JSON file under `<root>/sche_tasks/`. The scheduler
loop(s) every 60 seconds, compares task schedule to last-run time (derived
from the newest report file), and fires due tasks through AgentLoop.

Reports land in `<root>/sche_tasks/done/<YYYY-MM-DD_HHMM>_<task>.md` — the
presence of today's report is how we know a daily task was already run.

Task JSON shape:
  {
    "name": "stock_watch",
    "schedule": "08:00",           # HH:MM local time
    "repeat": "daily",             # daily | weekday | weekly | once | every_Nh | every_Nd
    "prompt": "查一下今天 A 股大盘...",
    "enabled": true,
    "max_delay_hours": 6           # optional: if we're > N hours late, skip this fire
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, date
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}")


@dataclass
class Task:
    name: str
    schedule: str             # "HH:MM" local
    prompt: str
    repeat: str = "daily"     # daily | weekday | weekly | once | every_Nh | every_Nd
    enabled: bool = True
    max_delay_hours: int = 6

    def validate(self) -> None:
        if not _SAFE_NAME_RE.fullmatch(self.name):
            raise ValueError("name 必须 a-z0-9_- 1-40 字符")
        try:
            datetime.strptime(self.schedule, "%H:%M")
        except ValueError:
            raise ValueError("schedule 要 HH:MM (24 小时制)")
        if not self.prompt.strip():
            raise ValueError("prompt 不能为空")
        if self.repeat not in ("daily", "weekday", "weekly", "once"):
            if not re.fullmatch(r"every_\d+[hd]", self.repeat):
                raise ValueError("repeat 必须是 daily/weekday/weekly/once/every_Nh/every_Nd")


def tasks_dir(root: Path) -> Path:
    return root / "sche_tasks"


def reports_dir(root: Path) -> Path:
    return root / "sche_tasks" / "done"


def _task_path(root: Path, name: str) -> Path:
    return tasks_dir(root) / f"{name}.json"


def list_tasks(root: Path) -> list[Task]:
    d = tasks_dir(root)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(Task(**{k: data.get(k) for k in Task.__dataclass_fields__ if k in data}))
        except Exception as e:
            log.warning("skip malformed task %s: %s", p, e)
    return out


def save_task(root: Path, task: Task) -> Path:
    task.validate()
    tasks_dir(root).mkdir(parents=True, exist_ok=True)
    path = _task_path(root, task.name)
    path.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def delete_task(root: Path, name: str) -> bool:
    path = _task_path(root, name)
    if not path.exists():
        return False
    path.unlink()
    return True


# ─────────────── Due-time calculation ───────────────

def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _last_run(root: Path, name: str) -> datetime | None:
    """Parse the newest matching report filename; returns None if none yet."""
    rd = reports_dir(root)
    if not rd.exists():
        return None
    newest: datetime | None = None
    for p in rd.glob(f"*_{name}.md"):
        # filename: YYYY-MM-DD_HHMM_<name>.md
        try:
            stem = p.stem
            ts_part = "_".join(stem.split("_")[:2])   # "YYYY-MM-DD_HHMM"
            dt = datetime.strptime(ts_part, "%Y-%m-%d_%H%M")
            if newest is None or dt > newest:
                newest = dt
        except (ValueError, IndexError):
            continue
    return newest


def is_due(task: Task, now: datetime, last: datetime | None) -> bool:
    """Should this task fire right now?"""
    if not task.enabled:
        return False
    h, m = _parse_hm(task.schedule)
    scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)

    # every_Nh / every_Nd — pure interval, schedule is just first-time anchor
    m_iv = re.fullmatch(r"every_(\d+)([hd])", task.repeat)
    if m_iv:
        n = int(m_iv.group(1))
        unit = timedelta(hours=n) if m_iv.group(2) == "h" else timedelta(days=n)
        if last is None:
            return now >= scheduled_today
        return now - last >= unit

    if task.repeat == "once":
        if last is not None:
            return False
        return now >= scheduled_today

    # daily / weekday / weekly — require scheduled_today <= now, not yet run today,
    # and within max_delay_hours window.
    if now < scheduled_today:
        return False
    if last and last.date() == now.date():
        return False  # already ran today
    if (now - scheduled_today) > timedelta(hours=task.max_delay_hours):
        return False  # too late, skip this window

    if task.repeat == "weekday":
        return now.weekday() < 5         # Mon-Fri
    if task.repeat == "weekly":
        # Run on the same weekday as `last` OR if never run, today counts
        if last is None:
            return True
        return (now - last).days >= 7 and now.weekday() == last.weekday()
    # daily
    return True


# ─────────────── Run a task through AgentLoop ───────────────

async def run_once(root: Path, task: Task, cfg) -> Path:
    """Invoke AgentLoop with task.prompt, write a report. Returns report path."""
    from .runtime import build_agent
    from .core.loop import AgentLoop

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = reports_dir(root) / f"{ts}_{task.name}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = build_agent(root, cfg, system_prompt="")
    loop = AgentLoop(ctx.chain, ctx.prefix, ctx.handler,
                      policy=ctx.policy, max_turns=ctx.max_turns,
                      session_log=ctx.session_log)
    loop.add_user(task.prompt)

    pieces: list[str] = []
    start = time.time()
    error: str | None = None
    try:
        async for ev in loop.run():
            if ev.kind == "text" and ev.data:
                pieces.append(ev.data)
            elif ev.kind == "done":
                break
    except Exception as e:
        log.exception("scheduled task %s failed", task.name)
        error = str(e)
    elapsed = time.time() - start

    body = "".join(pieces).strip() or "(agent 没有输出文本)"
    header = (
        f"# {task.name}\n\n"
        f"- 触发时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"- 耗时: {elapsed:.1f}s\n"
        f"- schedule: `{task.schedule}` · repeat: `{task.repeat}`\n"
        f"{'- 错误: ' + error if error else ''}\n\n"
        f"## Prompt\n\n```\n{task.prompt}\n```\n\n"
        f"## Agent 回复\n\n"
    )
    report_path.write_text(header + body + "\n", encoding="utf-8")
    log.info("scheduler: wrote report %s (elapsed %.1fs)", report_path.name, elapsed)
    return report_path


# ─────────────── The poll loop ───────────────

async def scheduler_loop(root: Path, *, interval: float = 60.0, stop_evt=None) -> None:
    """Background loop: every `interval` seconds, scan tasks and fire due ones.
    Cancel by calling `stop_evt.set()` (if provided) or cancelling the task."""
    from .config import load_config
    log.info("scheduler loop starting (root=%s, interval=%.0fs)", root, interval)
    while True:
        try:
            if stop_evt is not None and stop_evt.is_set():
                break
            try:
                cfg = load_config(root / "config.toml")
            except FileNotFoundError:
                cfg = None
            if cfg is not None:
                now = datetime.now()
                for task in list_tasks(root):
                    last = _last_run(root, task.name)
                    if is_due(task, now, last):
                        log.info("scheduler: firing %s", task.name)
                        try:
                            await run_once(root, task, cfg)
                        except Exception:
                            log.exception("task %s crashed", task.name)
        except Exception:
            log.exception("scheduler loop iteration crashed — sleeping and retrying")
        await asyncio.sleep(interval)


# ─────────────── List reports ───────────────

def list_reports(root: Path, *, task_name: str | None = None,
                 limit: int = 50) -> list[dict]:
    rd = reports_dir(root)
    if not rd.exists():
        return []
    paths = sorted(rd.glob("*.md"), reverse=True)
    out = []
    for p in paths:
        stem = p.stem
        parts = stem.split("_", 2)
        if len(parts) != 3:
            continue
        if task_name and parts[2] != task_name:
            continue
        out.append({
            "file": p.name,
            "task": parts[2],
            "time": f"{parts[0]} {parts[1][:2]}:{parts[1][2:]}",
            "size": p.stat().st_size,
        })
        if len(out) >= limit:
            break
    return out
