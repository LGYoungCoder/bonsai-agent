"""Periodic housekeeping for long-running bonsai processes.

Spawned by every long-lived entrypoint (`bonsai serve`, each
`bonsai channel-run *` runner) so the user never has to set up cron.

Currently runs:
  - gc: delete session/evidence JSONL older than `cfg.maintenance.gc_retention_days`

Daemon thread → dies with the process. Failures log + continue (we never let
maintenance crash the agent).

Toggle in config.toml:
  [maintenance]
  gc_enabled = true        # default
  gc_retention_days = 15
  gc_interval_hours = 24
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STARTED: set[str] = set()  # idempotency: re-call with same root is no-op


def start_maintenance(root: Path, cfg: Any) -> threading.Thread | None:
    """Spawn the maintenance daemon thread for this root. Idempotent.

    Returns the thread (or None if disabled / already running for this root).
    """
    m = getattr(cfg, "maintenance", None)
    if m is None or not getattr(m, "gc_enabled", True):
        log.info("maintenance: disabled by config")
        return None

    key = str(root.resolve())
    if key in _STARTED:
        log.debug("maintenance: already running for %s", key)
        return None
    _STARTED.add(key)

    t = threading.Thread(
        target=_loop,
        args=(root, cfg),
        name=f"bonsai-maintenance-{root.name}",
        daemon=True,
    )
    t.start()
    log.info("maintenance: started (gc every %dh, retention %dd)",
             m.gc_interval_hours, m.gc_retention_days)
    return t


def _loop(root: Path, cfg: Any) -> None:
    interval = max(60, cfg.maintenance.gc_interval_hours * 3600)
    # First run after ~5 min so a freshly-launched bot doesn't churn during
    # boot. Then settle into the configured interval.
    time.sleep(300)
    while True:
        try:
            _run_once(root, cfg)
        except Exception:
            log.exception("maintenance tick failed")
        time.sleep(interval)


def _run_once(root: Path, cfg: Any) -> None:
    from .cli.gc import run_gc
    skill_root = (root / cfg.memory.skill_dir.lstrip("./")).resolve()
    report = run_gc(
        root,
        retention_days=cfg.maintenance.gc_retention_days,
        skill_root=skill_root,
        skip_busy_probe=False,
    )
    # Single-line log for ops visibility; render() is multi-line.
    flat = report.render().replace("\n", " | ")
    log.info("maintenance gc: %s", flat)
