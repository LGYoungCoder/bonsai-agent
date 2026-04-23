"""Per-conversation state. History lives in the backend; this is a counter + cwd."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    cwd: Path = field(default_factory=Path.cwd)
    budget_used: int = 0
    turns: int = 0
    # backend handles full history; we just track counters here
    metadata: dict[str, Any] = field(default_factory=dict)

    def next_turn(self) -> None:
        self.turns += 1

    def artifact_dir(self, base: Path | None = None) -> Path:
        root = (base or self.cwd) / "temp" / "tool_artifacts" / self.session_id
        root.mkdir(parents=True, exist_ok=True)
        return root
