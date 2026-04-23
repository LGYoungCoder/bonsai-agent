"""Evidence recorder — per-session tool-call JSONL trail.

Writes one JSONL line per tool dispatch to:
  <skill_root>/_meta/evidence/session_<session_id>.jsonl

Line shape:
  {"t": 1700000000.123, "turn": 3, "tool": "file_read",
   "args": {"path": "x.py"}, "ok": true, "ms": 42}

distill reads these back to build evidence dicts for SkillStore.write_sop.
Deliberately small and append-only — no query API here, that lives in
the distill job.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import orjson

log = logging.getLogger(__name__)

_ARG_PREVIEW_CAP = 500  # truncate oversize string args in evidence


class EvidenceRecorder:
    """Append-only JSONL writer, one file per session."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = Path(root).resolve()
        self.session_id = session_id
        self.evidence_dir = self.root / "_meta" / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.evidence_dir / f"session_{session_id}.jsonl"

    def record(self, *, turn: int, tool: str, args: dict[str, Any] | None,
               ok: bool, duration_ms: int, err: str | None = None) -> None:
        entry: dict[str, Any] = {
            "t": round(time.time(), 3),
            "turn": turn,
            "tool": tool,
            "args": _clip_args(args or {}),
            "ok": ok,
            "ms": duration_ms,
        }
        if err:
            entry["err"] = err[:200]
        try:
            with self.path.open("ab") as f:
                f.write(orjson.dumps(entry) + b"\n")
        except Exception as e:
            # Evidence must never break the agent. Log and swallow.
            log.warning("evidence write failed: %s", e)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                out.append(orjson.loads(line))
            except orjson.JSONDecodeError:
                continue
        return out


def _clip_args(args: dict[str, Any]) -> dict[str, Any]:
    """Truncate oversize string values — keep schema, drop bulk."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _ARG_PREVIEW_CAP:
            out[k] = v[:_ARG_PREVIEW_CAP] + f"...[+{len(v) - _ARG_PREVIEW_CAP} chars]"
        else:
            out[k] = v
    return out


def load_session_evidence(root: Path, session_id: str) -> list[dict[str, Any]]:
    """Helper for distill: read a session's full trace without a recorder."""
    path = Path(root).resolve() / "_meta" / "evidence" / f"session_{session_id}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            out.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            continue
    return out
