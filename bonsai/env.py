"""Ground-truth environment snapshot — first thing the agent reads.

Without this the LLM defaults to its training-distribution mode (POSIX +
sudo + apt). On Windows that turns into "sudo apt install …" which just
errors. ~80 tokens of cheap, observable facts disarm 95% of that.

Snapshot is computed once per cwd (lru_cache) so repeated calls within a
session produce byte-identical output → Anthropic prompt cache stays warm.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from functools import lru_cache
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def _detect_os() -> str:
    sysname = platform.system()
    if sysname == "Windows":
        return f"Windows {platform.release()} (build {platform.version()})"
    if sysname == "Darwin":
        ver = platform.mac_ver()[0] or platform.release()
        return f"macOS {ver}"
    if sysname == "Linux":
        try:
            info: dict[str, str] = {}
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        info[k] = v.strip('"')
            return info.get("PRETTY_NAME") or info.get("NAME", f"Linux {platform.release()}")
        except OSError:
            return f"Linux {platform.release()}"
    return f"{sysname} {platform.release()}"


def _detect_shell_line() -> str:
    """User's terminal shell + whether code_run type=bash will work here.

    Reports honestly because code_run uses ["bash", "-c", code] — on Windows
    without WSL/Git Bash on PATH, that fails outright. Knowing this saves
    the model from drafting a bash script and watching it crash.
    """
    bash_ok = shutil.which("bash") is not None
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "")
        sh = Path(comspec).name if comspec else "cmd.exe"
        suffix = "code_run bash OK (检测到 bash)" if bash_ok else "code_run bash 不可用"
        return f"{sh}; {suffix}"
    sh = Path(os.environ.get("SHELL", "/bin/sh")).name
    suffix = "code_run bash OK" if bash_ok else "code_run bash 不可用 (PATH 上没 bash)"
    return f"{sh}; {suffix}"


def _detect_pkg_mgr() -> str:
    """One-line guidance on package managers — the actual sudo trap.

    The LLM's reflex is `sudo apt install`. Stating "winget / NEVER sudo"
    makes the right verb lexically present in context, which beats any
    amount of negative instruction.
    """
    if os.name == "nt":
        wins = [n for n in ("winget", "scoop", "choco") if shutil.which(n)]
        avail = " / ".join(wins) if wins else "winget / scoop / choco"
        return f"{avail} + pip (NEVER sudo / apt / brew — 这是 Windows)"
    if sys.platform == "darwin":
        brew = "brew" if shutil.which("brew") else "(brew 未装)"
        return f"{brew} + pip (不要 sudo brew — 这是 macOS)"
    # Linux: detect actual pkg mgr instead of guessing apt
    for cand in ("apt-get", "dnf", "yum", "pacman", "apk", "zypper"):
        if shutil.which(cand):
            need_sudo = os.geteuid() != 0 if hasattr(os, "geteuid") else True
            return f"{cand}{' (需要 sudo)' if need_sudo else ' (root,无需 sudo)'} + pip"
    return "pip (未识别系统包管理器)"


def _detect_project(cwd: Path) -> str:
    """Best-effort project identity. First hit wins; no concatenation —
    歧义比没有更糟。"""
    pp = cwd / "pyproject.toml"
    if pp.exists():
        try:
            with pp.open("rb") as f:
                data = tomllib.load(f)
            proj = data.get("project") or {}
            name = proj.get("name")
            if name:
                ver = proj.get("version", "")
                return f"{name}{(' ' + ver) if ver else ''} (pyproject)"
        except Exception:
            pass
    pj = cwd / "package.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            if data.get("name"):
                ver = data.get("version", "")
                return f"{data['name']}{(' ' + ver) if ver else ''} (npm)"
        except Exception:
            pass
    ct = cwd / "Cargo.toml"
    if ct.exists():
        try:
            with ct.open("rb") as f:
                data = tomllib.load(f)
            pkg = data.get("package") or {}
            name = pkg.get("name")
            if name:
                ver = pkg.get("version", "")
                return f"{name}{(' ' + ver) if ver else ''} (cargo)"
        except Exception:
            pass
    gm = cwd / "go.mod"
    if gm.exists():
        try:
            for line in gm.read_text(encoding="utf-8").splitlines():
                if line.startswith("module "):
                    return f"{line.split(maxsplit=1)[1].strip()} (go)"
        except OSError:
            pass
    gc = cwd / ".git" / "config"
    if gc.exists():
        try:
            for line in gc.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("url ="):
                    seg = line.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
                    if seg.endswith(".git"):
                        seg = seg[:-4]
                    if seg:
                        return f"{seg} (git remote)"
        except OSError:
            pass
    return f"{cwd.name or '(unnamed)'} (cwd basename)"


@lru_cache(maxsize=8)
def _snapshot_cached(cwd_str: str) -> str:
    cwd = Path(cwd_str)
    py = f"{platform.python_version()} ({platform.python_implementation()})"
    lines = [
        f"os       = {_detect_os()}",
        f"shell    = {_detect_shell_line()}",
        f"cwd      = {cwd}",
        f"project  = {_detect_project(cwd)}",
        f"python   = {py}",
        f"pkg_mgr  = {_detect_pkg_mgr()}",
    ]
    return "\n".join(lines)


def snapshot(cwd: Path | None = None) -> str:
    """Return cached, byte-stable env snapshot for the given cwd."""
    return _snapshot_cached(str((cwd or Path.cwd()).resolve()))


def render_block(cwd: Path | None = None) -> str:
    """Wrap snapshot in the markdown header used inside the system prompt."""
    return f"## Environment\n{snapshot(cwd)}"
