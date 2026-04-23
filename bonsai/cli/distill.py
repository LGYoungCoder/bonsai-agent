"""`bonsai distill` — propose SOPs from captured evidence.

Flow:
  1. Scan skills/_meta/evidence/session_*.jsonl.
  2. Surface sessions whose trace looks SOP-worthy:
     - ≥ MIN_OK successful tool calls
     - no unresolved errors at the tail
  3. Print a ranked list; user picks one or inspects trace.
  4. User provides SOP name + body; we call SkillStore.write_sop with the
     evidence trace so gating + L1 rebuild + evidence persist happen.

Deliberately half-manual: the model shouldn't decide what's a skill without
a human check. No LLM call here — pure file scan + pick.
"""

from __future__ import annotations

import datetime as _dt
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import orjson

MIN_OK = 3                 # need at least this many successful tool calls
MAX_SUMMARY_TOOLS = 8      # preview top-N distinct tools


def _list_sessions(evidence_dir: Path) -> list[Path]:
    if not evidence_dir.exists():
        return []
    return sorted(evidence_dir.glob("session_*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _load_trace(path: Path) -> list[dict[str, Any]]:
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


def _score(trace: list[dict]) -> dict[str, Any]:
    """Rank a session by SOP-worthiness.

    A session is worth surfacing if:
      • at least MIN_OK successful tool calls
      • tail (last 3 calls) contains NO errors — means the user ended in
        a working state, so the pattern converges
    """
    ok_calls = [t for t in trace if t.get("ok")]
    tail_errs = any((not t.get("ok")) for t in trace[-3:])
    return {
        "n_total": len(trace),
        "n_ok": len(ok_calls),
        "tail_clean": not tail_errs,
        "qualifies": len(ok_calls) >= MIN_OK and not tail_errs,
        "tools": Counter(t.get("tool", "?") for t in ok_calls),
    }


def _relative_age(path: Path) -> str:
    delta = _dt.datetime.now() - _dt.datetime.fromtimestamp(path.stat().st_mtime)
    secs = int(delta.total_seconds())
    if secs < 120:
        return f"{secs}s ago"
    if secs < 7200:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def list_candidates(skills_root: Path) -> list[dict[str, Any]]:
    """Public helper — returns candidate sessions without printing.

    Each dict: {path, session_id, age, n_ok, n_total, tools_preview, qualifies}
    """
    evidence_dir = Path(skills_root) / "_meta" / "evidence"
    out: list[dict[str, Any]] = []
    for p in _list_sessions(evidence_dir):
        trace = _load_trace(p)
        s = _score(trace)
        sid = p.stem.replace("session_", "", 1)
        tools = ", ".join(f"{t}×{n}" for t, n in
                          s["tools"].most_common(MAX_SUMMARY_TOOLS))
        out.append({
            "path": p,
            "session_id": sid,
            "age": _relative_age(p),
            "n_ok": s["n_ok"],
            "n_total": s["n_total"],
            "tail_clean": s["tail_clean"],
            "qualifies": s["qualifies"],
            "tools_preview": tools,
        })
    return out


def print_candidates(skills_root: Path) -> int:
    cands = list_candidates(skills_root)
    if not cands:
        print(f"[distill] no evidence yet in {skills_root}/_meta/evidence/")
        print("  Run a few tasks via `bonsai chat` to record tool calls, "
              "then re-run `bonsai distill`.")
        return 0
    print(f"[distill] {len(cands)} session(s) under {skills_root}/_meta/evidence/")
    n_qual = 0
    for c in cands:
        tick = "✓" if c["qualifies"] else " "
        n_qual += int(c["qualifies"])
        print(f" [{tick}] session={c['session_id']}  ok={c['n_ok']:>3}/"
              f"{c['n_total']:<3}  {c['age']:>7}  {c['tools_preview']}")
    print(f"\n  {n_qual} session(s) qualify (≥ {MIN_OK} successes, clean tail).")
    print("  Next:  bonsai distill inspect <session_id>")
    print("         bonsai distill propose <session_id> <sop_name>")
    return 0


def print_inspection(skills_root: Path, session_id: str,
                      limit: int = 40) -> int:
    p = Path(skills_root) / "_meta" / "evidence" / f"session_{session_id}.jsonl"
    trace = _load_trace(p)
    if not trace:
        print(f"[distill] no trace for session {session_id!r}")
        return 1
    s = _score(trace)
    print(f"session={session_id}  total={s['n_total']}  "
          f"ok={s['n_ok']}  tail_clean={s['tail_clean']}  "
          f"qualifies={s['qualifies']}")
    print("-" * 64)
    shown = trace[-limit:] if len(trace) > limit else trace
    for i, t in enumerate(shown, start=max(1, len(trace) - limit + 1)):
        status = "ok" if t.get("ok") else "ERR"
        args = t.get("args") or {}
        preview = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in args.items())[:120]
        print(f" {i:>3}. turn={t.get('turn','?')} {status:>3} "
              f"{t.get('tool',''):<18} {preview}")
    if s["qualifies"]:
        print("\n  Next: bonsai distill propose "
              f"{session_id} <sop_name>")
    return 0


def propose_sop(skills_root: Path, session_id: str, sop_name: str,
                body_from: Path | None = None) -> int:
    """Write a new SOP using evidence from this session.

    Body text:
      • --body-from <file>: read SOP body from file (preferred, scriptable)
      • stdin (interactive): read until EOF
    """
    from ..stores.skill_store import SkillStore
    p = Path(skills_root) / "_meta" / "evidence" / f"session_{session_id}.jsonl"
    trace = _load_trace(p)
    if not trace:
        print(f"[distill] no trace for session {session_id!r}")
        return 1
    s = _score(trace)
    if not s["qualifies"]:
        print(f"[distill] session {session_id!r} does not qualify "
              f"(ok={s['n_ok']} need ≥{MIN_OK}, tail_clean={s['tail_clean']}).")
        return 1

    if body_from is not None:
        if not body_from.exists():
            print(f"[distill] body file not found: {body_from}")
            return 1
        body = body_from.read_text(encoding="utf-8")
    else:
        print(f"[distill] Enter SOP body for {sop_name!r}. Ctrl-D to finish:")
        body = sys.stdin.read()
    if not body.strip():
        print("[distill] empty body, aborting.")
        return 1

    # Compose evidence payload SkillStore expects
    evidence = {
        "session_id": session_id,
        "tool_calls": [
            {"tool": t.get("tool"), "turn": t.get("turn"),
             "is_error": not t.get("ok")}
            for t in trace
        ],
    }
    store = SkillStore(Path(skills_root))
    store.init()
    try:
        target = store.write_sop(sop_name, body, evidence)
    except ValueError as e:
        print(f"[distill] gate rejected SOP: {e}")
        return 1
    print(f"[distill] wrote {target}")
    return 0
