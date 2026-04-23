"""skill_lookup — hit the SkillStore L1 keyword index."""

from __future__ import annotations

from ..stores.skill_store import SkillStore


def skill_lookup(keyword: str, *, store: SkillStore) -> str:
    hits = store.lookup(keyword)
    if not hits:
        return f"[skill_lookup] no SOP found for keyword={keyword!r}"
    lines = [f"[skill_lookup] found {len(hits)} candidate(s) for {keyword!r}:"]
    for p in hits:
        rel = p.relative_to(store.root) if p.is_relative_to(store.root) else p
        lines.append(f"  - {rel}")
    lines.append("\nUse file_read to open the full SOP.")
    return "\n".join(lines)
