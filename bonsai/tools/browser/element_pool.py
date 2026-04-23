"""Short-ID pool: AX node IDs → a1, a2, ... for the LLM.

The LLM sees 'a3 button "Apply"'; we translate 'a3' back to a real CDP
backendNodeId / AX node id at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ElementPool:
    # short_id -> AX node details we need later
    entries: dict[str, dict] = field(default_factory=dict)
    _counter: int = 0

    def reset(self) -> None:
        self.entries.clear()
        self._counter = 0

    def assign(self, *, ax_node_id: str, backend_node_id: int | None,
               role: str, name: str) -> str:
        self._counter += 1
        sid = f"a{self._counter}"
        self.entries[sid] = {
            "ax_node_id": ax_node_id,
            "backend_node_id": backend_node_id,
            "role": role,
            "name": name,
        }
        return sid

    def resolve(self, short_id: str) -> dict | None:
        return self.entries.get(short_id)
