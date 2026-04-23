"""Tiny header/value sanitizer shared by provider adapters.

Users paste API keys from all kinds of sources — email clients, admin
consoles, docs — that silently insert BOMs, zero-width spaces, or
curly/full-width quotes. httpx then refuses the request with an
unfriendly 'ascii' codec error. We clean the common junk and raise a
clear error if anything visibly non-ASCII survives.
"""
from __future__ import annotations

# Invisible Unicode chars commonly pasted alongside real values.
_INVISIBLE = "".join([
    "﻿",  # BOM / ZWNBSP
    "​", "‌", "‍",  # zero-width space / non-joiner / joiner
    " ", " ",             # line / paragraph separator
    " ",                       # no-break space
])


def sanitize_header_value(name: str, value: str) -> str:
    """Strip invisible junk and verify the remainder is HTTP-header safe.

    Raises ValueError with a clear Chinese message on visible non-ASCII.
    """
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    for ch in _INVISIBLE:
        cleaned = cleaned.replace(ch, "")
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError as e:
        snippet = cleaned[:40]
        raise ValueError(
            f"{name} 含有非 ASCII 字符(位置 {e.start})。"
            f"大概率是粘贴时带了全角引号 / 中文字符 / BOM。"
            f"清理空白后仍有:{snippet!r}"
        ) from e
    return cleaned
