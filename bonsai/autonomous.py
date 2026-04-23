"""Autonomous workspace — lets the agent self-direct on a user-maintained
todo list while the user is offline.

Pure file operations — agent reads / writes via normal file_* tools.
No new tools, no daemon;ride the existing scheduler for periodic triggers.

Layout (under `<root>/data/autonomous/`):

    todo.md          — user-authored checklist; agent only toggles [ ]→[x]
    history.txt      — one-line-per-run, newest first
    reports/R##_<slug>.md  — auto-numbered reports (R01, R02, ...)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slug(title: str) -> str:
    s = _SLUG_RE.sub("_", title.strip()).strip("_")
    return (s[:40] or "untitled").lower()


@dataclass
class AutonomousWorkspace:
    root: Path

    @property
    def dir(self) -> Path:
        return self.root / "data" / "autonomous"

    @property
    def todo_path(self) -> Path:
        return self.dir / "todo.md"

    @property
    def history_path(self) -> Path:
        return self.dir / "history.txt"

    @property
    def reports_dir(self) -> Path:
        return self.dir / "reports"

    # ─────────── init ───────────

    def init(self, overwrite: bool = False) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if overwrite or not self.todo_path.exists():
            self.todo_path.write_text(_TODO_TEMPLATE, encoding="utf-8")
        if not self.history_path.exists():
            self.history_path.write_text("", encoding="utf-8")

    @property
    def initialized(self) -> bool:
        return self.todo_path.exists() and self.history_path.exists()

    # ─────────── todo ───────────

    def get_todo(self) -> str:
        return self.todo_path.read_text(encoding="utf-8") if self.todo_path.exists() else ""

    def set_todo(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.todo_path.write_text(text, encoding="utf-8")

    def mark_item_done(self, item_prefix: str) -> bool:
        """Change `- [ ] <prefix>...` → `- [x] <prefix>...` (first match)."""
        if not self.todo_path.exists():
            return False
        text = self.todo_path.read_text(encoding="utf-8")
        needle_re = re.compile(
            r"^(\s*-\s*\[)\s(\]\s*" + re.escape(item_prefix.strip()[:30]) + ".*)$",
            re.MULTILINE,
        )
        new, n = needle_re.subn(r"\1x\2", text, count=1)
        if n == 0:
            return False
        self.todo_path.write_text(new, encoding="utf-8")
        return True

    # ─────────── history ───────────

    def get_history(self, limit: int = 20) -> list[str]:
        if not self.history_path.exists():
            return []
        lines = [ln for ln in self.history_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return lines[:limit]

    def append_history(self, line: str) -> None:
        """Prepend one line (newest first)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        prev = self.history_path.read_text(encoding="utf-8") if self.history_path.exists() else ""
        self.history_path.write_text(line.rstrip() + "\n" + prev, encoding="utf-8")

    # ─────────── reports ───────────

    def next_report_path(self, title: str) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        for p in self.reports_dir.glob("R[0-9][0-9]*_*.md"):
            try:
                num = int(p.name[1:3])
                if num >= n:
                    n = num + 1
            except ValueError:
                continue
        return self.reports_dir / f"R{n:02d}_{_slug(title)}.md"

    def list_reports(self, limit: int = 50) -> list[dict]:
        if not self.reports_dir.exists():
            return []
        out = []
        for p in sorted(self.reports_dir.glob("R[0-9][0-9]*_*.md"), reverse=True):
            out.append({
                "file": p.name,
                "num": p.name[:3],
                "title": p.stem[4:],
                "size": p.stat().st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            })
            if len(out) >= limit:
                break
        return out

    def read_report(self, fname: str) -> str:
        # defend against path traversal
        target = (self.reports_dir / fname).resolve()
        if not str(target).startswith(str(self.reports_dir.resolve())):
            raise ValueError("报告路径越界")
        if not target.exists():
            raise FileNotFoundError(fname)
        return target.read_text(encoding="utf-8")


_TODO_TEMPLATE = """# 自主任务 TODO

> agent 下线时会按下面的清单挑一条做。
> 格式:`- [ ] 描述`。`[x]` 是已完成的(不会再选)。
> 一条描述两行以内,关键是"目标清晰"和"可小步验证"。

## 日常维护

- [ ] 扫一遍 `skills/L3/` 看有没有 verified_on 超过 30 天的 SOP,标注一下
- [ ] 搜 MemoryStore 最近 50 条,看有没有能抽成 L3 skill 的重复模式

## 项目(示例 — 编辑掉改成你自己的)

- [ ] 把 ~/Documents/notes/ 里的 md 笔记批量 `bonsai mine` 进记忆库
- [ ] 看看 README 有哪些说法跟实际代码对不上,写份修正建议报告

## 本周要做但没排期的(占位)

- [ ]
"""
