"""Type-aware truncation. One goal: keep the LLM-useful bits, drop the rest.

Dispatch table:
  JSON      → keep schema + first N array items
  CSV/TSV   → header + first 20 rows + per-column stats on tail
  logs/code → head + tail + ALL lines matching ERROR|WARN|Traceback
  plain     → head+tail fold (with optional interest_hint from budget.py)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any

from .budget import truncate_tool_result

log = logging.getLogger(__name__)


_ERR_RE = re.compile(r"\b(error|ERROR|err|fail|FAIL|traceback|Traceback|WARN|warn)\b")


def smart_format(content: str, *, max_chars: int = 20_000,
                 hint_type: str | None = None,
                 interest_hint: str | None = None) -> str:
    """Route to the right formatter. hint_type can be 'json', 'csv', 'log', 'code', ..."""
    if len(content) <= max_chars:
        return content
    ctype = hint_type or _guess_type(content)
    try:
        if ctype == "json":
            return _format_json(content, max_chars)
        if ctype in ("csv", "tsv"):
            return _format_csv(content, max_chars, sep="\t" if ctype == "tsv" else ",")
        if ctype in ("log", "code"):
            return _format_log(content, max_chars)
    except Exception as e:
        log.debug("smart_format fallback due to: %s", e)
    return truncate_tool_result(content, max_chars=max_chars, interest_hint=interest_hint)


def _guess_type(content: str) -> str:
    head = content.lstrip()[:500]
    if head.startswith(("{", "[")) and _looks_like_json(content):
        return "json"
    if "\n" in content and "," in content.splitlines()[0]:
        return "csv"
    if "\n" in content and "\t" in content.splitlines()[0]:
        return "tsv"
    if _ERR_RE.search(content):
        return "log"
    return "text"


def _looks_like_json(content: str) -> bool:
    try:
        json.loads(content[:100000])  # cap to keep it cheap
        return True
    except Exception:
        return False


def _format_json(content: str, max_chars: int) -> str:
    try:
        parsed = json.loads(content)
    except Exception:
        return truncate_tool_result(content, max_chars=max_chars)

    # Render a compact shape hint + first-N items.
    shape = _json_shape(parsed, depth=0, max_items=3)
    compact = json.dumps(shape, ensure_ascii=False, indent=2)
    if len(compact) <= max_chars:
        return compact

    # Too big even as shape — fall back to head-tail.
    raw = json.dumps(parsed, ensure_ascii=False)
    return truncate_tool_result(raw, max_chars=max_chars)


def _json_shape(v: Any, *, depth: int, max_items: int, max_depth: int = 3) -> Any:
    if depth >= max_depth:
        return f"<{type(v).__name__}>"
    if isinstance(v, list):
        return ([_json_shape(x, depth=depth + 1, max_items=max_items)
                 for x in v[:max_items]]
                + (["..."] if len(v) > max_items else []))
    if isinstance(v, dict):
        return {k: _json_shape(val, depth=depth + 1, max_items=max_items)
                for k, val in list(v.items())[:20]}
    return v


def _format_csv(content: str, max_chars: int, *, sep: str = ",") -> str:
    reader = csv.reader(io.StringIO(content), delimiter=sep)
    rows = list(reader)
    if not rows:
        return content[:max_chars]
    header = rows[0]
    body = rows[1:]
    total_rows = len(body)
    sample = body[:20]

    lines = [sep.join(header)]
    for r in sample:
        lines.append(sep.join(r))
    lines.append(f"# ... {total_rows - 20} more rows (total {total_rows})"
                 if total_rows > 20 else "")

    # Column stats on a small sample.
    if total_rows > 0:
        lines.append("\n# column stats (first 500 rows):")
        for i, col in enumerate(header):
            vals = [r[i] for r in body[:500] if i < len(r)]
            distinct = len(set(vals))
            sample_vals = ", ".join(sorted(set(vals))[:5])
            lines.append(f"  {col}: {distinct} distinct | sample: {sample_vals}")
    out = "\n".join(lines)
    return out if len(out) <= max_chars else truncate_tool_result(out, max_chars=max_chars)


def _format_log(content: str, max_chars: int) -> str:
    lines = content.splitlines(keepends=True)
    if not lines:
        return content

    head_n = 30
    tail_n = 30
    head = lines[:head_n]
    tail = lines[-tail_n:]
    mid = lines[head_n:-tail_n] if len(lines) > head_n + tail_n else []
    errors = [ln for ln in mid if _ERR_RE.search(ln)]

    # Cap errors to keep total size in check.
    cap = max(1, (max_chars // 2) // max(1, int(sum(len(e) for e in errors) / max(len(errors), 1))))
    errors = errors[:cap]

    out = "".join(head)
    if errors:
        out += f"\n... [{len(mid)} mid lines; {len(errors)} errors/warns kept] ...\n"
        out += "".join(errors)
    else:
        dropped = len(mid)
        if dropped:
            out += f"\n... [{dropped} mid lines dropped, no ERR/WARN] ...\n"
    out += "".join(tail)
    if len(out) <= max_chars:
        return out
    return truncate_tool_result(out, max_chars=max_chars)
