"""Wake-up: build L0 identity + L1 essentials to inject into the frozen prefix.

Hard cap: ≤ ~1K tokens. Anything bigger breaks the cache economics.

L0 (identity):  user profile, preferred tone, non-negotiable conventions.
                Tiny (~150 tokens), hand-crafted, read from skills/L0.md.
L1 (essentials): compact recent-memory overview + SkillStore L1 index summary.
                Machine-generated each session, but byte-stable *within* a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..stores.memory_store import MemoryStore
from ..stores.skill_store import SkillStore
from .budget import estimate

HARD_CAP_TOKENS = 1000


@dataclass
class Wakeup:
    identity: str
    essentials: str

    def render(self) -> str:
        out = ""
        if self.identity:
            out += f"## L0 Identity\n{self.identity.strip()}\n"
        if self.essentials:
            out += f"\n## L1 Essentials\n{self.essentials.strip()}\n"
        return out.strip()


def build_wakeup(skill_store: SkillStore, memory_store: MemoryStore | None,
                 *, identity_file: Path | None = None) -> Wakeup:
    identity = _load_identity(identity_file, skill_store)
    essentials = _compose_essentials(skill_store, memory_store)

    # Enforce the hard cap — trim essentials first (identity is tiny + curated).
    current = identity + "\n" + essentials
    while estimate(current) > HARD_CAP_TOKENS and len(essentials.splitlines()) > 3:
        essentials = "\n".join(essentials.splitlines()[:-1])
        current = identity + "\n" + essentials
    return Wakeup(identity=identity, essentials=essentials)


def _load_identity(path: Path | None, skill_store: SkillStore) -> str:
    # Prefer explicit file, else skills/L0.md
    if path is None:
        path = skill_store.root / "L0.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    # Minimal default — users should edit this.
    return (
        "用户(主人)的专属 agent。\n"
        "- 回复简洁,不赘述\n"
        "- 用中文优先,代码/命令保留原样\n"
        "- 每次回合先查 skill 再动手"
    )


def _compose_essentials(skill_store: SkillStore, memory_store: MemoryStore | None) -> str:
    parts: list[str] = []

    l2 = skill_store.l2_text(max_chars=400)
    if l2.strip() and not l2.strip().startswith("#"):
        parts.append(f"### L2 Facts\n{l2.strip()}")

    l1 = skill_store.l1_text(max_chars=600)
    if l1 and any(":" in ln for ln in l1.splitlines() if not ln.startswith("#")):
        parts.append(f"### Skill Index (keyword → path)\n{l1.strip()}")

    if memory_store is not None:
        recent = memory_store.wake_up_l1(max_items=8)
        if recent:
            parts.append(f"### Recent Memory Scopes\n{recent.strip()}")

    return "\n\n".join(parts)
