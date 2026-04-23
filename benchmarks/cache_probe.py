"""Empirically measure cache hit rate per provider.

Usage:
  python benchmarks/cache_probe.py --config ./config.toml --provider glm-primary

Sends the same 2K-token prefix twice in a row, reports the cache_read_tokens
delta on the second call. A provider with working prefix caching should report
cache_read ~= prefix size on the second call.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from bonsai.adapters import build_adapter
from bonsai.config import load_config
from bonsai.core.types import DynamicTail, FrozenPrefix, Message, ToolSpec


STABLE_PREFIX = "You are Bonsai.\n" + ("This is a stable prefix.\n" * 200)
STABLE_TOOL = ToolSpec(
    name="echo",
    description="echo the input back",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


async def probe(provider_cfg: dict) -> None:
    backend = build_adapter(provider_cfg)
    prefix = FrozenPrefix(system_prompt=STABLE_PREFIX, tools=[STABLE_TOOL])
    tail = DynamicTail(messages=[Message(role="user", content="say hi")])

    print(f"== probing {backend.name} ({backend.kind} / {backend.model}) ==")
    for i in range(2):
        t0 = time.time()
        try:
            resp = await backend.chat(prefix, tail, max_tokens=32)
        except Exception as e:
            print(f"  call {i+1}: ERROR {e}")
            return
        dt = time.time() - t0
        u = resp.usage
        print(f"  call {i+1}: in={u.input_tokens} out={u.output_tokens} "
              f"cache_read={u.cache_read_tokens} cache_create={u.cache_creation_tokens} "
              f"({dt:.2f}s)")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--provider", type=str, default=None,
                        help="probe one provider by name; default: all in config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    targets = (
        [cfg.provider(args.provider)] if args.provider
        else cfg.providers
    )
    for p in targets:
        if not p.get("api_key"):
            print(f"-- skipping {p['name']}: no api_key")
            continue
        await probe(p)


if __name__ == "__main__":
    asyncio.run(main())
