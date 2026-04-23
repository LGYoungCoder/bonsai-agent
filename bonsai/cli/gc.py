"""`bonsai gc` — delete old, already-archived session / evidence JSONL files.

Permanent bot processes accumulate these forever; 15-day default retention
reclaims disk without affecting MemoryStore (which holds the distilled
knowledge, not the raw turn logs).

Algorithm:
  1. Scan logs/sessions/*.jsonl — drop any whose mtime is older than N days.
  2. Scan skills/_meta/evidence/*.jsonl — same.
  3. Report what was kept / dropped.

Skips files actively being written (size change within 5s) for safety.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path  # noqa: F401  (Path is used as type via signature & dataclass field)

DEFAULT_RETENTION_DAYS = 15


@dataclass
class GcReport:
    sessions_kept: int = 0
    sessions_removed: int = 0
    evidence_kept: int = 0
    evidence_removed: int = 0
    bytes_freed: int = 0
    dry_run: bool = False

    def render(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        return (
            f"{prefix}sessions: {self.sessions_removed} removed, "
            f"{self.sessions_kept} kept\n"
            f"{prefix}evidence: {self.evidence_removed} removed, "
            f"{self.evidence_kept} kept\n"
            f"{prefix}freed: {self.bytes_freed / 1024 / 1024:.1f} MB"
        )


def _is_currently_writing(p: Path, probe_delay: float = 5.0) -> bool:
    """Cheap safety check: size changes between two reads → in use."""
    try:
        s0 = p.stat().st_size
    except OSError:
        return True  # assume busy rather than risk a race
    time.sleep(probe_delay)
    try:
        s1 = p.stat().st_size
    except OSError:
        return True
    return s0 != s1


def run_gc(root: Path, *, retention_days: int = DEFAULT_RETENTION_DAYS,
           skill_root: Path | None = None,
           dry_run: bool = False,
           skip_busy_probe: bool = False) -> GcReport:
    """Delete logs older than retention_days. Returns a report; never raises
    on a single-file failure (best-effort cleanup)."""
    report = GcReport(dry_run=dry_run)
    cutoff = time.time() - retention_days * 86400

    def _maybe_delete(p: Path, ks: str) -> None:
        try:
            mtime = p.stat().st_mtime
            size = p.stat().st_size
        except OSError:
            return
        if mtime >= cutoff:
            if ks == "sessions":
                report.sessions_kept += 1
            else:
                report.evidence_kept += 1
            return
        if not skip_busy_probe and _is_currently_writing(p, probe_delay=0.2):
            # Shouldn't happen at this age, but be paranoid — never delete a
            # file that's still being appended to.
            if ks == "sessions":
                report.sessions_kept += 1
            else:
                report.evidence_kept += 1
            return
        if not dry_run:
            try:
                p.unlink()
            except OSError:
                return
        report.bytes_freed += size
        if ks == "sessions":
            report.sessions_removed += 1
        else:
            report.evidence_removed += 1

    sessions_dir = root / "logs" / "sessions"
    if sessions_dir.exists():
        for p in sessions_dir.glob("*.jsonl"):
            _maybe_delete(p, "sessions")

    evidence_dir = (skill_root or (root / "skills")) / "_meta" / "evidence"
    if evidence_dir.exists():
        for p in evidence_dir.glob("session_*.jsonl"):
            _maybe_delete(p, "evidence")

    return report
