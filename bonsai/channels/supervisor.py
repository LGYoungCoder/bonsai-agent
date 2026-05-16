"""Lifecycle management for channel runners spawned from the web UI.

Runner = the long-lived process that polls the vendor for messages and
dispatches them to AgentLoop. Today only `wechat` has a real runner; this
module is generic so other kinds slot in as their runtimes land.

Model:
- One instance per (root, kind). PID persisted to `<root>/data/<kind>_runner.pid`.
- Log tailed from `<root>/logs/<kind>_runner.log`.
- start() is idempotent: if a live PID already exists, it's returned unchanged.
- stop() sends SIGTERM then, after 3s, escalates (SIGKILL on POSIX;
  Windows SIGTERM is already TerminateProcess, so escalation is a no-op).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from .._proc import DETACH_KWARGS, pid_alive, terminate_pid

log = logging.getLogger(__name__)

_SUPPORTED = {"wechat", "telegram", "qq", "feishu", "dingtalk"}


def _pid_file(root: Path, kind: str) -> Path:
    return root / "data" / f"{kind}_runner.pid"


def _log_file(root: Path, kind: str) -> Path:
    return root / "logs" / f"{kind}_runner.log"


def _read_pid(root: Path, kind: str) -> int | None:
    pf = _pid_file(root, kind)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def status(root: Path, kind: str) -> dict:
    if kind not in _SUPPORTED:
        return {"running": False, "error": f"unsupported kind: {kind}"}
    pid = _read_pid(root, kind)
    if pid is None:
        return {"running": False}
    if not pid_alive(pid):
        # Clean up stale pid file.
        _pid_file(root, kind).unlink(missing_ok=True)
        return {"running": False, "stale_pid": pid}
    lf = _log_file(root, kind)
    return {
        "running": True,
        "pid": pid,
        "log_path": str(lf),
        "log_exists": lf.exists(),
    }


def start(root: Path, kind: str, *, allow: str = "") -> dict:
    if kind not in _SUPPORTED:
        raise ValueError(f"unsupported kind: {kind}")
    st = status(root, kind)
    if st.get("running"):
        return st

    lf = _log_file(root, kind)
    lf.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(lf, "a", buffering=1, encoding="utf-8")
    log_handle.write(f"\n===== runner start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log_handle.flush()

    args = [sys.executable, "-m", "bonsai.cli.__main__", "channel-run", kind,
            "--project", str(root)]
    if allow:
        args += ["--allow", allow]

    # DETACH_KWARGS detaches from web server's process group so Ctrl+C on
    # `bonsai serve` doesn't take the runner down. POSIX → start_new_session;
    # Windows → CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS.
    proc = subprocess.Popen(
        args, stdout=log_handle, stderr=log_handle,
        cwd=str(root),
        **DETACH_KWARGS,
    )
    _pid_file(root, kind).parent.mkdir(parents=True, exist_ok=True)
    _pid_file(root, kind).write_text(str(proc.pid), encoding="utf-8")
    log.info("spawned %s runner pid=%d log=%s", kind, proc.pid, lf)
    return status(root, kind)


def stop(root: Path, kind: str, *, timeout: float = 3.0) -> dict:
    if kind not in _SUPPORTED:
        raise ValueError(f"unsupported kind: {kind}")
    pid = _read_pid(root, kind)
    if pid is None or not pid_alive(pid):
        _pid_file(root, kind).unlink(missing_ok=True)
        return {"running": False, "stopped_nothing": True}
    if not terminate_pid(pid, timeout=timeout):
        log.warning("failed to fully terminate pid %d", pid)
    _pid_file(root, kind).unlink(missing_ok=True)
    return {"running": False, "killed_pid": pid}


def _bonsai_pkg_mtime() -> float:
    """Newest mtime among all .py files in the bonsai package — proxy for
    'when did the code last change'. Walks ~150 files; ms-scale latency."""
    pkg_root = Path(__file__).resolve().parent.parent  # bonsai/
    latest = 0.0
    for p in pkg_root.rglob("*.py"):
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            pass
    return latest


def restart_stale_runners(root: Path, *, kinds: list[str] | None = None) -> list[dict]:
    """For each running runner, if its pid file is older than the newest
    bonsai source file mtime, stop+start it. Caller is `bonsai serve` startup.

    Why this exists: runners are spawned detached so they survive
    `serve` restarts. Without this sweep, `git pull` + restart serve leaves
    runners on the *previous* bytecode → confusing tracebacks where line
    numbers from old bytecode point at docstrings in current source.

    Returns one dict per kind acted on (skipped kinds omitted). Logs only.
    """
    pkg_mtime = _bonsai_pkg_mtime()
    out: list[dict] = []
    for kind in (kinds or sorted(_SUPPORTED)):
        st = status(root, kind)
        if not st.get("running"):
            continue
        pf = _pid_file(root, kind)
        try:
            runner_started = pf.stat().st_mtime
        except OSError:
            continue
        # 30s grace: if you just started the runner manually, don't immediately
        # restart it because some unrelated touched .py file is newer.
        if runner_started + 30 >= pkg_mtime:
            continue
        age_h = (pkg_mtime - runner_started) / 3600
        log.warning(
            "channel %s runner pid=%s started %.1fh before latest source change "
            "→ auto-restart so it picks up new bytecode",
            kind, st.get("pid"), age_h,
        )
        old_pid = st.get("pid")
        try:
            stop(root, kind)
            new_st = start(root, kind)
            log.info("channel %s auto-restarted: pid %s → %s",
                     kind, old_pid, new_st.get("pid"))
            out.append({"kind": kind, "old_pid": old_pid,
                        "new_pid": new_st.get("pid"), "stale_hours": round(age_h, 1)})
        except Exception as e:
            log.warning("channel %s auto-restart failed: %s", kind, e)
            out.append({"kind": kind, "old_pid": old_pid, "error": str(e)})
    return out


def log_tail(root: Path, kind: str, lines: int = 200) -> str:
    lf = _log_file(root, kind)
    if not lf.exists():
        return ""
    # Last N lines without loading the whole file.
    try:
        with lf.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError as e:
        return f"(log read failed: {e})"
