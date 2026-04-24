"""Dual-output logging — console (by configured level) + rotating file (DEBUG).

Why this exists: `basicConfig` gives console-only logs that vanish when the
REPL scrolls. Silent disconnects (model-side timeouts, half-closed sockets)
were impossible to diagnose after the fact. File handler captures full DEBUG
regardless of console level so postmortem is always possible.

Idempotent: safe to call multiple times; second call is a no-op. Respects
BONSAI_LOG_LEVEL / BONSAI_LOG_FILE env vars as last-resort overrides.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MARK = "_bonsai_configured"


def setup_logging(
    *,
    log_file: str | Path = "./logs/bonsai.log",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    project_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Configure root logger with console + rotating file handlers.

    Returns the resolved log file path (useful to print at startup so the
    user knows where to `tail -f`).
    """
    root = logging.getLogger()
    if getattr(root, _MARK, False) and not force:
        return getattr(root, "_bonsai_log_path", Path(log_file))

    console_level = os.environ.get("BONSAI_CONSOLE_LEVEL", console_level).upper()
    file_level = os.environ.get("BONSAI_FILE_LEVEL", file_level).upper()
    lf = os.environ.get("BONSAI_LOG_FILE", str(log_file))
    path = Path(lf)
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    path.parent.mkdir(parents=True, exist_ok=True)

    # Root at the lowest of the two so both handlers see everything they need.
    root.setLevel(min(
        logging.getLevelName(console_level) if isinstance(console_level, str) else console_level,
        logging.getLevelName(file_level) if isinstance(file_level, str) else file_level,
    ))

    # Clear any prior handlers (e.g. basicConfig in tests) so levels actually apply.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(file_level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Silence httpx's INFO per-request spam on the console; keep it in file.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")

    setattr(root, _MARK, True)
    setattr(root, "_bonsai_log_path", path)
    return path
