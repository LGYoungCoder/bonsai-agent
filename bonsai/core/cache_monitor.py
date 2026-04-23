"""Per-provider cache hit-rate monitoring. Append-only JSONL + in-memory aggregate."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import orjson


@dataclass
class CacheStats:
    provider: str
    requests: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        total_prefix = self.cache_read_tokens + self.cache_creation_tokens
        if total_prefix == 0:
            return 0.0
        return self.cache_read_tokens / total_prefix


@dataclass
class CacheMonitor:
    log_path: Path | None = None
    stats: dict[str, CacheStats] = field(default_factory=lambda: defaultdict(lambda: None))
    warn_threshold: float = 0.70

    def record(self, provider: str, *, cache_read: int, cache_creation: int,
               input_tokens: int, output_tokens: int,
               model: str | None = None) -> None:
        s = self.stats.get(provider)
        if s is None:
            s = CacheStats(provider=provider)
            self.stats[provider] = s
        s.requests += 1
        s.cache_read_tokens += cache_read
        s.cache_creation_tokens += cache_creation
        s.input_tokens += input_tokens
        s.output_tokens += output_tokens

        if self.log_path is not None:
            entry = {
                "t": time.time(),
                "provider": provider,
                "model": model,
                "cache_read": cache_read,
                "cache_creation": cache_creation,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "hit_rate_cumulative": round(s.hit_rate, 3),
            }
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("ab") as f:
                f.write(orjson.dumps(entry) + b"\n")

    def summary(self) -> str:
        if not self.stats:
            return "(no cache stats yet)"
        rows = []
        for name, s in sorted(self.stats.items()):
            rows.append(
                f"  {name:<20} req={s.requests:<5} "
                f"hit={s.hit_rate:.0%} "
                f"read={s.cache_read_tokens} "
                f"create={s.cache_creation_tokens} "
                f"in={s.input_tokens} out={s.output_tokens}"
            )
        return "cache stats:\n" + "\n".join(rows)

    def alert(self, provider: str) -> str | None:
        s = self.stats.get(provider)
        if not s or s.requests < 5:
            return None
        if s.hit_rate < self.warn_threshold:
            return (f"[warn] {provider} cache hit rate {s.hit_rate:.0%} "
                    f"< {self.warn_threshold:.0%} after {s.requests} reqs")
        return None
