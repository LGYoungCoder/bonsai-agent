"""code_run — python / bash. Truncate + save full log to disk."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from ..core.smart_format import smart_format

TAIL_LIMIT = 8_000  # tail bytes in tool result; full log saved to disk


async def _run(cmd: list[str], *, cwd: Path, timeout: int,
               stdin: bytes | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin else None,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return (124, "", f"[timeout after {timeout}s]")
    return (proc.returncode or 0,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"))


async def code_run(code: str, *, type: str = "python", timeout: int = 60,
                   cwd: Path | None = None, interest_hint: str | None = None,
                   artifact_dir: Path | None = None) -> str:
    work = cwd or Path.cwd()
    t0 = time.time()

    if type == "python":
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            script = f.name
        try:
            rc, stdout, stderr = await _run(
                [sys.executable, script],
                cwd=work, timeout=timeout,
            )
        finally:
            Path(script).unlink(missing_ok=True)
    elif type == "bash":
        rc, stdout, stderr = await _run(
            ["bash", "-c", code],
            cwd=work, timeout=timeout,
        )
    else:
        return f"[error] unsupported type: {type}"

    dt = time.time() - t0
    combined = stdout + ("\n[stderr]\n" + stderr if stderr else "")
    full_path = None
    if artifact_dir and len(combined) > TAIL_LIMIT:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        full_path = artifact_dir / f"run_{int(t0)}.log"
        full_path.write_text(combined, encoding="utf-8")

    header = f"[exit={rc} · {dt:.2f}s" + (f" · saved={full_path}]" if full_path else "]")
    body = smart_format(combined, max_chars=TAIL_LIMIT, hint_type="log",
                        interest_hint=interest_hint)
    return header + "\n" + body
