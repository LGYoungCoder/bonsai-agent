"""Guard: the frozen prefix must be byte-stable across builds.

We snapshot (sys_prompt ⊕ tools_schema) → SHA256 and compare against a
baseline in .prefix_baseline. If they drift, PR fails unless the baseline
is updated in the same PR (force opt-in, via `--update`).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import orjson


def compute_hash(root: Path) -> str:
    sys_prompt = (root / "prompts" / "system.txt").read_bytes()
    schema = (root / "tools" / "schema.json").read_bytes()
    # Normalize JSON to order-insensitive form.
    normalized = orjson.dumps(orjson.loads(schema), option=orjson.OPT_SORT_KEYS)
    h = hashlib.sha256()
    h.update(sys_prompt)
    h.update(b"\0")
    h.update(normalized)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--update", action="store_true", help="write new baseline")
    args = ap.parse_args()

    baseline_path = args.root / ".prefix_baseline"
    current = compute_hash(args.root)

    if args.update or not baseline_path.exists():
        baseline_path.write_text(current + "\n", encoding="utf-8")
        print(f"[prefix-audit] wrote baseline: {current}")
        return 0

    saved = baseline_path.read_text(encoding="utf-8").strip()
    if saved == current:
        print(f"[prefix-audit] OK · {current[:16]}...")
        return 0

    print(f"[prefix-audit] FAIL")
    print(f"  baseline: {saved}")
    print(f"  current:  {current}")
    print(f"  The frozen prefix changed. This invalidates prompt cache for every user.")
    print(f"  If intentional, run: python benchmarks/prefix_audit.py --update")
    return 1


if __name__ == "__main__":
    sys.exit(main())
