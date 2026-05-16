"""Cross-platform process helpers.

POSIX 和 Windows 在三个小但关键的地方不一样,这里集中处理一次:

- `os.kill(pid, 0)` 在 POSIX 上是 liveness probe,在 Windows 上 Python
  会抛 TypeError("unsupported signal") — 必须走 ctypes / OpenProcess。
- Windows 没有 `SIGKILL`;`os.kill(pid, SIGTERM)` 在 Windows 上等价于
  TerminateProcess,本身就强杀,escalate 步骤可省。
- `start_new_session=True` 只在 POSIX 起 detach 作用;Windows 需要
  `creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`,否则
  父进程退出会把子进程一起带走。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

_IS_WIN = os.name == "nt"

if _IS_WIN:
    DETACH_KWARGS: dict = {
        "creationflags": (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    }
else:
    DETACH_KWARGS = {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    """True if pid refers to a running (non-zombie) process."""
    if pid <= 0:
        return False
    if _IS_WIN:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # Linux: zombie 在 os.kill(pid,0) 下仍报 alive,/proc 里 State 才真。
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        if "Z" in line.split(":", 1)[1]:
                            try:
                                os.waitpid(pid, os.WNOHANG)
                            except OSError:
                                pass
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return True


def terminate_pid(pid: int, *, timeout: float = 3.0) -> bool:
    """SIGTERM → 等 `timeout` 秒 → POSIX 上升级 SIGKILL。返回是否已死。"""
    if not pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        sigkill = getattr(signal, "SIGKILL", None)
        if sigkill is not None:
            try:
                os.kill(pid, sigkill)
            except OSError:
                pass
    return not pid_alive(pid)
