"""code_search — structured grep across the working tree.

Prefers ripgrep (rg) when available, falls back to grep -rn. Returns
matches as `path:line: preview`, truncated to keep tool result small.
Compared to `code_run grep ...`, this is:
  - structured (no shell quoting hell)
  - bounded (max_results enforced)
  - cwd-aware (defaults to session cwd, not /opt/lg/bonsai)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _truncate_line(s: str, limit: int = 200) -> str:
    s = s.rstrip("\n")
    return s if len(s) <= limit else s[:limit] + " …[trunc]"


def code_search(query: str, *, path: str = ".", glob: str | None = None,
                case_insensitive: bool = False, max_results: int = 80,
                cwd: Path | None = None) -> str:
    if not query:
        return "[error] query required"
    base = cwd or Path.cwd()
    target = (base / path).resolve() if not Path(path).is_absolute() else Path(path)
    if not target.exists():
        return f"[error] path not found: {target}"

    rg = shutil.which("rg")
    if rg:
        args = [rg, "--line-number", "--no-heading", "--color=never",
                "--max-count=20", "--max-columns=300"]
        if case_insensitive:
            args.append("-i")
        if glob:
            args.extend(["-g", glob])
        args.extend(["--", query, str(target)])
    else:
        args = ["grep", "-rn", "--color=never"]
        if case_insensitive:
            args.append("-i")
        if glob:
            args.extend(["--include", glob])
        args.extend(["-e", query, str(target)])

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "[error] search timed out after 20s"
    except FileNotFoundError as e:
        return f"[error] {e}"

    if proc.returncode not in (0, 1):  # 1 = no matches (normal)
        err = (proc.stderr or "").strip()[:300]
        return f"[error] search failed (exit {proc.returncode}): {err}"

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return f"[no matches] query={query!r} path={path}"

    truncated = len(lines) > max_results
    shown = lines[:max_results]
    body = "\n".join(_truncate_line(ln) for ln in shown)
    head = f"[{len(lines)} matches]" + (" (truncated)" if truncated else "")
    return f"{head}\n{body}"
