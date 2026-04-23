"""file_read — read file with line numbers, keyword localization, size cap."""

from __future__ import annotations

from pathlib import Path

from ..core.smart_format import smart_format

MAX_CHARS_DEFAULT = 20_000


_SUFFIX_TO_TYPE = {
    ".json": "json", ".jsonl": "json",
    ".csv": "csv", ".tsv": "tsv",
    ".log": "log",
    ".py": "code", ".js": "code", ".ts": "code", ".go": "code",
    ".rs": "code", ".c": "code", ".cpp": "code",
}


def file_read(path: str, *, start: int = 1, count: int = 200,
              keyword: str | None = None, cwd: Path | None = None) -> str:
    base = cwd or Path.cwd()
    p = (base / path).resolve() if not Path(path).is_absolute() else Path(path)
    if not p.exists():
        return f"[error] file not found: {p}"
    if not p.is_file():
        return f"[error] not a file: {p}"
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[error] read failed: {e}"

    lines = raw.splitlines()
    total = len(lines)

    if keyword:
        kw_lower = keyword.lower()
        match_idx = next((i for i, ln in enumerate(lines) if kw_lower in ln.lower()), None)
        if match_idx is None:
            return f"[error] keyword not found: {keyword}"
        start = max(1, match_idx + 1 - count // 4)

    start = max(1, start)
    end = min(total, start + count - 1)
    chunk = lines[start - 1:end]
    header = f"[{p} lines {start}-{end} of {total}]\n"
    body = "\n".join(f"{start + i:>6}  {ln}" for i, ln in enumerate(chunk))
    out = header + body
    hint_type = _SUFFIX_TO_TYPE.get(p.suffix.lower())
    # For structured types we prefer smart_format over line-numbered view when file is large.
    if hint_type and total > count * 2:
        return smart_format(raw, max_chars=MAX_CHARS_DEFAULT,
                            hint_type=hint_type, interest_hint=keyword)
    return smart_format(out, max_chars=MAX_CHARS_DEFAULT, hint_type=hint_type,
                        interest_hint=keyword)
