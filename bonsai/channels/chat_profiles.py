"""Per-user chat profile — lightweight persistent info the agent can use
to personalize replies, editable by the user themselves via `/name` /
`/note` commands inside the IM.

Storage: one JSON per uid under `<root>/data/user_profiles/<safe_uid>.json`.
Preamble goes into the user's AgentLoop tail as a one-time system note on
the first message of each session (cleared when `/new` resets it).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_SAFE_UID_RE = re.compile(r"[^A-Za-z0-9_.\-]")


@dataclass
class UserProfile:
    uid: str
    nickname: str = ""
    notes: list[str] = field(default_factory=list)

    def preamble(self) -> str:
        """System-style preface to inject before the user's first turn.
        Empty string if there's nothing to say."""
        if not self.nickname and not self.notes:
            return ""
        parts = []
        if self.nickname:
            parts.append(f"对方昵称: {self.nickname}")
        if self.notes:
            bullet = "\n  - " + "\n  - ".join(self.notes)
            parts.append(f"对方偏好 / 备注:{bullet}")
        return "(系统提示 —— 仅供你参考,不要照念给用户)\n" + "\n".join(parts) + "\n---\n\n"


def _safe_uid(uid: str) -> str:
    return _SAFE_UID_RE.sub("_", uid)[:100] or "anon"


def _path(root: Path, uid: str) -> Path:
    return root / "data" / "user_profiles" / f"{_safe_uid(uid)}.json"


def load_profile(root: Path, uid: str) -> UserProfile:
    p = _path(root, uid)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return UserProfile(
                uid=d.get("uid", uid),
                nickname=d.get("nickname", "") or "",
                notes=list(d.get("notes", []) or []),
            )
        except Exception:
            pass
    return UserProfile(uid=uid)


def save_profile(root: Path, profile: UserProfile) -> None:
    p = _path(root, profile.uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
