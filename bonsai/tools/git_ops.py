"""git_ops — structured git wrappers for the harness loop.

Why: agent doing `code_run git ...` is fine until commit messages get
multi-line / contain shell-special chars. This wrapper:
  - quotes message via subprocess argv (no shell)
  - returns structured short summary
  - clamps diff output to keep tool result bounded

Supported actions: status / diff / commit / log / branch
"""

from __future__ import annotations

import subprocess
from pathlib import Path


_MAX_DIFF_BYTES = 12_000
_MAX_LOG_ENTRIES = 20


def _git(args: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            cwd=str(cwd), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "[git timed out]"
    except FileNotFoundError:
        return 127, "", "[git not installed]"
    return proc.returncode, proc.stdout, proc.stderr


def _is_git_repo(cwd: Path) -> bool:
    code, _, _ = _git(["rev-parse", "--git-dir"], cwd)
    return code == 0


def git_ops(action: str, *, message: str | None = None,
            files: list[str] | None = None,
            paths: list[str] | None = None,
            staged: bool = False,
            limit: int = 10,
            cwd: Path | None = None) -> str:
    base = cwd or Path.cwd()
    action = (action or "").lower()

    if not _is_git_repo(base):
        return f"[error] not a git repo: {base}"

    if action == "status":
        code, out, err = _git(["status", "--porcelain=v1", "--branch"], base)
        if code != 0:
            return f"[error] git status failed: {err.strip()}"
        return f"[git status]\n{out.strip() or '(clean)'}"

    if action == "diff":
        args = ["diff"]
        if staged:
            args.append("--staged")
        if paths:
            args.append("--")
            args.extend(paths)
        code, out, err = _git(args, base)
        if code != 0:
            return f"[error] git diff failed: {err.strip()}"
        if not out.strip():
            return "[git diff] (no changes)"
        truncated = len(out.encode()) > _MAX_DIFF_BYTES
        body = out[:_MAX_DIFF_BYTES] + ("\n…[truncated]" if truncated else "")
        return f"[git diff{' --staged' if staged else ''}]\n{body}"

    if action == "log":
        args = ["log", f"--oneline", f"-n{min(limit, _MAX_LOG_ENTRIES)}"]
        code, out, err = _git(args, base)
        if code != 0:
            return f"[error] git log failed: {err.strip()}"
        return f"[git log]\n{out.strip() or '(no commits)'}"

    if action == "commit":
        if not message:
            return "[error] commit requires message"
        if files:
            add_code, _, add_err = _git(["add", "--", *files], base)
            if add_code != 0:
                return f"[error] git add failed: {add_err.strip()}"
        # check there's something staged
        code, out, _ = _git(["diff", "--cached", "--name-only"], base)
        if code == 0 and not out.strip():
            return "[error] nothing staged to commit (pass files=[...] or stage first)"
        code, out, err = _git(["commit", "-m", message], base)
        if code != 0:
            return f"[error] git commit failed: {err.strip() or out.strip()}"
        # find new HEAD
        _, sha, _ = _git(["rev-parse", "--short", "HEAD"], base)
        return f"[ok] committed {sha.strip()}\n{out.strip()}"

    if action == "branch":
        code, out, err = _git(["rev-parse", "--abbrev-ref", "HEAD"], base)
        if code != 0:
            return f"[error] {err.strip()}"
        return f"[git branch] {out.strip()}"

    return f"[error] unknown action: {action!r} (status/diff/commit/log/branch)"
